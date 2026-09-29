"""Audio capture, playback and device discovery with GStreamer (PipeWire / PulseAudio)."""

from __future__ import annotations

import array
import itertools
import logging
import math
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from .echo import EchoDelayEstimator, webrtc_finds_delay  # noqa: E402
from .feedback import FeedbackDetector  # noqa: E402
from .intercom import BYTES_PER_SECOND, CHANNELS, SAMPLE_RATE  # noqa: E402

Gst.init(None)

log = logging.getLogger(__name__)

RAW_CAPS = f"audio/x-raw,format=S16LE,layout=interleaved,rate={SAMPLE_RATE},channels={CHANNELS}"

PLAYOUT_DELAY = 80 * Gst.MSECOND   # jitter buffer: how far behind arrival audio is played
MAX_AHEAD = 600 * Gst.MSECOND      # if playback falls further behind than this, drop audio


@dataclass(frozen=True)
class AudioDevice:
    id: str      # stable identifier stored in the settings; "" = system default
    name: str    # human-readable


DEFAULT_DEVICE = AudioDevice("", "System default")

# Providers are tried in order. The PulseAudio one talks to PipeWire through
# pipewire-pulse on Ubuntu and is the most mature; PipeWire's own is the fallback.
_PROVIDERS = ("pulsedeviceprovider", "pipewiredeviceprovider")
_FALLBACK_ELEMENTS = {
    "source": ("pulsesrc", "pipewiresrc", "autoaudiosrc"),
    "sink": ("pulsesink", "pipewiresink", "autoaudiosink"),
}


def _make(factory: str) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, None)
    if element is None:
        raise RuntimeError(f"GStreamer element “{factory}” is missing (is gstreamer1.0-plugins-good installed?)")
    return element


def _link_all(pipeline: Gst.Pipeline, elements: list[Gst.Element]) -> None:
    for element in elements:
        pipeline.add(element)
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise RuntimeError(f"Could not link {a.get_name()} to {b.get_name()}")


def _device_id(device: Gst.Device) -> str:
    try:
        name = device.get_property("internal-name")  # GstPulseDevice
        if name:
            return str(name)
    except TypeError:
        pass
    props = device.get_properties()
    if props:
        for key in ("node.name", "object.path", "device.path"):
            value = props.get_string(key)
            if value:
                return value
    return device.get_display_name()


def _is_monitor(device: Gst.Device) -> bool:
    props = device.get_properties()
    return bool(props and props.get_string("device.class") == "monitor")


class DeviceRegistry:
    """Lists input/output devices and creates GStreamer elements for them."""

    def __init__(self):
        self._lock = threading.Lock()
        self._devices: dict[tuple[str, str], Gst.Device] = {}

    @staticmethod
    def _probe() -> list[Gst.Device]:
        for name in _PROVIDERS:
            provider = Gst.DeviceProviderFactory.get_by_name(name)
            if provider is None:
                continue
            devices = provider.get_devices()
            if devices:
                return list(devices)
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Audio/Source", None)
        monitor.add_filter("Audio/Sink", None)
        return list(monitor.get_devices() or [])

    def refresh(self) -> tuple[list[AudioDevice], list[AudioDevice]]:
        """Return (inputs, outputs); each list starts with DEFAULT_DEVICE."""
        inputs, outputs = [DEFAULT_DEVICE], [DEFAULT_DEVICE]
        found: dict[tuple[str, str], Gst.Device] = {}
        for device in self._probe():
            if device.has_classes("Audio/Source"):
                if _is_monitor(device):
                    continue  # "Monitor of ..." loopback sources are not microphones
                kind, target = "source", inputs
            elif device.has_classes("Audio/Sink"):
                kind, target = "sink", outputs
            else:
                continue
            dev_id = _device_id(device)
            if (kind, dev_id) not in found:
                found[(kind, dev_id)] = device
                target.append(AudioDevice(dev_id, device.get_display_name()))
        with self._lock:
            self._devices = found
        return inputs, outputs

    def make_element(self, kind: str, device_id: str) -> Gst.Element:
        """Create a source ("source") or sink ("sink") element for a device id ("" = default)."""
        device = None
        if device_id:
            with self._lock:
                device = self._devices.get((kind, device_id))
            if device is None:
                self.refresh()
                with self._lock:
                    device = self._devices.get((kind, device_id))
        if device is not None:
            element = device.create_element(None)
            if element is not None:
                return element
        if device_id:
            log.warning("Audio device %r is not available; using the system default", device_id)
        for factory in _FALLBACK_ELEMENTS[kind]:
            element = Gst.ElementFactory.make(factory, None)
            if element is not None:
                return element
        raise RuntimeError("No audio output is available" if kind == "sink" else "No microphone is available")

    def make_monitor(self, sink_id: str) -> Gst.Element:
        """A source recording what an output device ("" = default) plays: its PulseAudio or
        pipewire-pulse monitor. Used as the reference for echo cancellation."""
        source = Gst.ElementFactory.make("pulsesrc", None)
        if source is None:
            raise RuntimeError("GStreamer's pulsesrc is missing")
        with self._lock:
            known = ("sink", sink_id) in self._devices
        # An unplugged device plays through the default output instead (see make_element).
        source.set_property("device", f"{sink_id}.monitor" if sink_id and known else "@DEFAULT_MONITOR@")
        return source


def pcm_level(pcm: bytes) -> float:
    """Loudness of an S16LE chunk mapped to 0..1 on a -60..0 dBFS scale."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    if sys.byteorder == "big":
        samples.byteswap()
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    if rms < 1:
        return 0.0
    return max(0.0, min(1.0, (20 * math.log10(rms / 32768) + 60) / 60))


def resample(pcm: bytes, rate: int) -> bytes:
    """16-bit mono PCM at `rate` Hz in LinuxComm's 16 kHz (e.g. a synthetic voice at 22050 Hz)."""
    pcm = pcm[: len(pcm) // 2 * 2]
    if rate == SAMPLE_RATE or not pcm:
        return pcm
    pipeline = Gst.parse_launch(
        f"appsrc name=src format=time caps=audio/x-raw,format=S16LE,layout=interleaved,rate={rate},channels=1 "
        f"! audioconvert ! audioresample ! {RAW_CAPS} ! appsink name=sink sync=false")
    src, sink = pipeline.get_by_name("src"), pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    buf = Gst.Buffer.new_wrapped(pcm)
    buf.pts, buf.duration = 0, len(pcm) // 2 * Gst.SECOND // rate
    src.emit("push-buffer", buf)
    src.emit("end-of-stream")
    out = bytearray()
    try:
        while (sample := sink.emit("pull-sample")) is not None:  # None at the end
            b = sample.get_buffer()
            out += b.extract_dup(0, b.get_size())
    finally:
        pipeline.set_state(Gst.State.NULL)
    return bytes(out)


# -- acoustic feedback suppression --------------------------------------------------

_MAGNITUDES = re.compile(r"magnitude=\(float\)\{([^}]*)\}")


def spectrum_magnitudes(structure: Gst.Structure) -> list[float]:
    """The magnitudes of a "spectrum" message (PyGObject can't read its GstValueList directly)."""
    match = _MAGNITUDES.search(structure.to_string())
    return [float(v) for v in match.group(1).split(",")] if match else []


class FeedbackSuppressor:
    """Acoustic feedback suppression for the microphone: notch filters that follow howling.

    A spectrum element watches the outgoing audio; when FeedbackDetector finds howling, one
    band of an equalizer becomes a narrow notch at that frequency (deeper each time the same
    howl comes back), up to NOTCHES at once, the oldest being reused. Speech passes unchanged
    until then. Both elements come with gstreamer1.0-plugins-good.
    """

    NOTCHES = 8
    BANDS = 256                        # spectrum resolution: 8000 Hz / 256 = 31 Hz at 16 kHz
    INTERVAL = 50 * Gst.MSECOND
    FIRST_GAIN, STEP, MIN_GAIN = -15.0, -6.0, -24.0   # dB

    def __init__(self, enabled: bool = True, on_notch: Callable[[float, float], None] | None = None):
        self.equalizer = _make("equalizer-nbands")
        self.spectrum = _make("spectrum")
        self.equalizer.set_property("num-bands", self.NOTCHES)
        self._bands = [self.equalizer.get_child_by_index(i) for i in range(self.NOTCHES)]
        for band in self._bands:
            band.set_property("type", 0)  # a peak filter: the first and last bands are shelves by default
            band.set_property("gain", 0.0)
        for name, value in (("bands", self.BANDS), ("interval", self.INTERVAL), ("threshold", -90),
                            ("post-messages", True), ("message-magnitude", True), ("message-phase", False)):
            self.spectrum.set_property(name, value)
        self._detector = FeedbackDetector(SAMPLE_RATE / 2 / self.BANDS)
        self._slots: list[tuple[float, float] | None] = [None] * self.NOTCHES   # (freq, gain) per band
        self._next = 0
        self._on_notch = on_notch
        self.enabled = enabled

    @property
    def elements(self) -> list[Gst.Element]:
        return [self.equalizer, self.spectrum]

    @property
    def notches(self) -> list[tuple[float, float]]:
        """The active notches as (frequency in Hz, gain in dB)."""
        return [slot for slot in self._slots if slot]

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:  # let everything through again
            self._slots = [None] * self.NOTCHES
            for band in self._bands:
                band.set_property("gain", 0.0)
        self._detector.reset()

    def on_spectrum(self, magnitudes: list[float]) -> None:
        if not self.enabled or not magnitudes:
            return
        freq = self._detector.feed(magnitudes)
        if freq is not None:
            self._notch(freq)

    def _notch(self, freq: float) -> None:
        same = next((i for i, slot in enumerate(self._slots)
                     if slot and abs(slot[0] - freq) <= 1.5 * self._detector.band_hz), None)
        if same is not None:  # the same howl again: cut deeper
            index, gain = same, max(self.MIN_GAIN, self._slots[same][1] + self.STEP)
            freq = self._slots[same][0]
        else:
            index = next((i for i, slot in enumerate(self._slots) if slot is None), self._next)
            self._next = (index + 1) % self.NOTCHES
            gain = self.FIRST_GAIN
        self._slots[index] = (freq, gain)
        band = self._bands[index]
        band.set_property("freq", freq)
        band.set_property("bandwidth", max(40.0, freq * 0.05))  # about 1/14 octave
        band.set_property("gain", gain)
        log.info("Acoustic feedback at %d Hz: notch filter at %d dB", round(freq), round(gain))
        if self._on_notch:
            self._on_notch(freq, gain)


# -- acoustic echo cancellation -------------------------------------------------------

class EchoCanceller:
    """Acoustic echo cancellation for the microphone, with WebRTC audio processing.

    What the speaker plays is recorded from the output device's monitor (so it covers other
    stations, the chime, and other apps' sound) into a webrtcechoprobe, the "reference". The
    webrtcdsp in the microphone's pipeline removes that sound from the microphone. The two
    pipelines share one clock and one base time, so the dsp can line their timestamps up.

    The real delay (the output's buffer, the room, the microphone) depends on the hardware.
    Current WebRTC audio processing (1.x, AEC3) finds it by itself. The old 0.3 library of
    Debian 12 / Raspberry Pi OS Bookworm only finds echo within about -20..+40 ms of where it is
    told to look, so with it an EchoDelayEstimator measures the delay and the reference
    pipeline's latency is set to match: the probe treats that latency as the time between
    recording the reference and hearing it. (Doing that with AEC3 stops it cancelling.)

    Needs the webrtcdsp and webrtcechoprobe elements from gstreamer1.0-plugins-bad (Debian 12,
    Raspberry Pi OS, Ubuntu 25.10+, Arch); Ubuntu 24.04's package leaves them out.
    start() raises RuntimeError if the reference can't be recorded.
    """

    WINDOW_BELOW, WINDOW_ABOVE = 20, 40   # ms: echo delays the dsp handles around the latency
    CHECK_EVERY_NS = Gst.SECOND

    _serial = itertools.count(1)

    @staticmethod
    def available() -> bool:
        return all(Gst.ElementFactory.find(name) for name in ("webrtcdsp", "webrtcechoprobe"))

    def __init__(self, reference_source: Gst.Element, label: str = "the speaker"):
        self.name = f"linuxcomm-echo-reference-{next(self._serial)}"
        self.label = label
        self._clock = Gst.SystemClock.obtain()
        self._base_time = self._clock.get_time()
        self.reference = Gst.Pipeline.new(None)
        caps = _make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(RAW_CAPS))  # the dsp needs the probe at its own rate
        probe = Gst.ElementFactory.make("webrtcechoprobe", self.name)
        self.dsp = Gst.ElementFactory.make("webrtcdsp", None)
        if probe is None or self.dsp is None:
            raise RuntimeError("GStreamer's webrtcdsp is missing (install gstreamer1.0-plugins-bad)")
        sink = _make("appsink")  # renders in time, like a speaker, and lets us measure the delay
        sink.set_property("sync", True)
        sink.set_property("emit-signals", True)
        sink.set_property("max-buffers", 50)
        sink.set_property("drop", True)
        sink.connect("new-sample", self._on_reference)
        _link_all(self.reference, [reference_source, _make("audioconvert"), _make("audioresample"), caps, probe, sink])
        self.share_clock(self.reference)
        self.dsp.set_property("probe", self.name)
        # Only echo cancellation: no noise suppression or automatic gain, which would change the voice.
        for name, value in (("echo-cancel", True), ("noise-suppression", False), ("gain-control", False),
                            ("delay-agnostic", True), ("extended-filter", True)):
            if self.dsp.find_property(name) is not None:
                self.dsp.set_property(name, value)
        self.delay = EchoDelayEstimator()
        self.latency_ms = 0            # where the dsp looks for echo
        self.echo_delay_ms: int | None = None
        self._next_check = 0
        try:  # the dsp's library is loaded now that the element exists
            maps = Path("/proc/self/maps").read_text()
        except OSError:
            maps = ""
        self.measures_delay = not webrtc_finds_delay(maps)

    def _on_reference(self, appsink) -> Gst.FlowReturn:
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        if buf.pts != Gst.CLOCK_TIME_NONE:
            self.delay.add("speaker", buf.pts, buf.extract_dup(0, buf.get_size()))
        return Gst.FlowReturn.OK

    def on_microphone(self, pts: int, pcm: bytes) -> None:
        """The microphone after echo cancellation (from the capture's streaming thread)."""
        if not self.measures_delay or pts == Gst.CLOCK_TIME_NONE:
            return
        self.delay.add("microphone", pts, pcm)
        if pts < self._next_check:
            return
        self._next_check = pts + self.CHECK_EVERY_NS
        found = self.delay.estimate()
        if found is None:
            return
        delay = found[0]
        # Clear echo left in the microphone means the dsp looks in the wrong place.
        if not self.latency_ms - self.WINDOW_BELOW <= delay <= self.latency_ms + self.WINDOW_ABOVE:
            self.echo_delay_ms = delay
            GLib.idle_add(self._set_latency, max(0, delay - 10))

    def _set_latency(self, ms: int) -> bool:
        self.latency_ms = ms
        self.reference.set_latency(ms * Gst.MSECOND)
        log.info("Echo cancellation: sound from %s comes back into the microphone after about %d ms",
                 self.label, self.echo_delay_ms)
        return GLib.SOURCE_REMOVE

    def share_clock(self, pipeline: Gst.Pipeline) -> None:
        """Run `pipeline` on the reference's clock and base time."""
        pipeline.use_clock(self._clock)
        pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
        pipeline.set_base_time(self._base_time)

    def start(self) -> None:
        """Start recording the reference; the probe must exist before the microphone starts."""
        if self.reference.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            msg = self.reference.get_bus().pop_filtered(Gst.MessageType.ERROR)
            self.stop()
            raise RuntimeError(msg.parse_error()[0].message if msg else "Could not record the speaker's sound")

    def stop(self) -> None:
        self.reference.set_state(Gst.State.NULL)


class Capture:
    """Records an input device as 16 kHz mono S16LE.

    on_data(bytes) and on_level(0..1) run on a GStreamer streaming thread;
    on_error(message) runs on the GLib main loop. With an EchoCanceller (already started)
    and/or a FeedbackSuppressor, the audio passes through their filters, echo cancellation
    first; the suppressor is adjusted on the GLib main loop. stop() also stops the canceller.
    """

    def __init__(self, source: Gst.Element,
                 on_data: Callable[[bytes], None] | None = None,
                 on_level: Callable[[float], None] | None = None,
                 on_error: Callable[[str], None] | None = None,
                 feedback: FeedbackSuppressor | None = None,
                 echo: EchoCanceller | None = None):
        self._on_data = on_data
        self._on_level = on_level
        self._on_error = on_error
        self.feedback = feedback
        self.echo = echo
        self._pipeline = Gst.Pipeline.new(None)
        caps = _make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(RAW_CAPS))
        sink = _make("appsink")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("max-buffers", 100)
        sink.set_property("drop", True)
        sink.connect("new-sample", self._on_new_sample)
        filters = ([echo.dsp] if echo else []) + (feedback.elements if feedback else [])
        _link_all(self._pipeline, [source, _make("audioconvert"), _make("audioresample"), caps, *filters, sink])
        if echo:
            echo.share_clock(self._pipeline)
        self._bus = self._pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus_handlers = [self._bus.connect("message::error", self._on_bus_error)]
        if feedback:
            self._bus_handlers.append(self._bus.connect("message::element", self._on_element))

    def start(self) -> None:
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            msg = self._bus.pop_filtered(Gst.MessageType.ERROR)
            self.stop()
            raise RuntimeError(msg.parse_error()[0].message if msg else "Could not open the microphone")

    def stop(self) -> None:
        self._pipeline.set_state(Gst.State.NULL)
        if self.echo:
            self.echo.stop()
        if self._bus_handlers:
            for handler in self._bus_handlers:
                self._bus.disconnect(handler)
            self._bus.remove_signal_watch()
            self._bus_handlers = []

    def _on_element(self, _bus, message) -> None:
        structure = message.get_structure()
        if self.feedback and structure is not None and structure.get_name() == "spectrum":
            self.feedback.on_spectrum(spectrum_magnitudes(structure))

    def _on_new_sample(self, appsink) -> Gst.FlowReturn:
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        pcm = buf.extract_dup(0, buf.get_size())
        try:
            if self.echo:
                self.echo.on_microphone(buf.pts, pcm)
            if self._on_data:
                self._on_data(pcm)
            if self._on_level:
                self._on_level(pcm_level(pcm))
        except Exception:
            log.exception("Capture callback failed")
        return Gst.FlowReturn.OK

    def _on_bus_error(self, _bus, message) -> None:
        err, debug = message.parse_error()
        log.error("Capture error: %s (%s)", err.message, debug)
        if self._on_error:
            self._on_error(err.message)


class Player:
    """Plays a stream of 16 kHz mono S16LE PCM on an output device.

    Buffers are timestamped against the system clock with a fixed playout delay,
    which acts as a small jitter buffer for audio arriving over the network.
    With live=True, audio that would play more than MAX_AHEAD late is dropped to
    keep latency bounded; use live=False for clips written all at once.
    write() and close() may be called from any single thread.
    """

    def __init__(self, sink: Gst.Element, volume: float = 1.0, live: bool = True):
        self._live = live
        self._pipeline = Gst.Pipeline.new(None)
        self._clock = Gst.SystemClock.obtain()
        self._pipeline.use_clock(self._clock)
        self._src = _make("appsrc")
        self._src.set_property("caps", Gst.Caps.from_string(RAW_CAPS))
        self._src.set_property("format", Gst.Format.TIME)
        self._src.set_property("is-live", True)
        vol = _make("volume")
        vol.set_property("volume", max(0.0, min(float(volume), 2.0)))
        _link_all(self._pipeline, [self._src, _make("queue"), _make("audioconvert"),
                                   _make("audioresample"), vol, sink])
        self._pending = b""
        self._base_pts = 0
        self._samples = -1          # -1 = not started (or recovering from an underrun)
        self._closed = False
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Could not open the audio output")

    def _running_time(self) -> int:
        return self._clock.get_time() - self._pipeline.get_base_time()

    def _pts(self, samples: int) -> int:
        return self._base_pts + samples * Gst.SECOND // SAMPLE_RATE

    def write(self, pcm: bytes) -> None:
        if self._closed:
            return
        if self._pending:
            pcm, self._pending = self._pending + pcm, b""
        if len(pcm) % 2:
            pcm, self._pending = pcm[:-1], pcm[-1:]
        if not pcm:
            return
        n = len(pcm) // (2 * CHANNELS)
        now = self._running_time()
        discont = False
        if self._samples < 0 or self._pts(self._samples) < now:
            # First audio, or the network stalled and we ran dry: restart the timeline.
            self._base_pts, self._samples, discont = now + PLAYOUT_DELAY, 0, True
        elif self._live and self._pts(self._samples) - now > MAX_AHEAD:
            return  # far behind real time (sender clock faster than ours): drop to catch up
        buf = Gst.Buffer.new_wrapped(pcm)
        buf.pts = self._pts(self._samples)
        buf.duration = self._pts(self._samples + n) - buf.pts
        if discont:
            buf.set_flags(Gst.BufferFlags.DISCONT)
        self._samples += n
        self._src.emit("push-buffer", buf)

    def close(self) -> None:
        """Play out what is queued, then release the device."""
        if self._closed:
            return
        self._closed = True
        ahead = max(0, self._pts(self._samples) - self._running_time()) if self._samples >= 0 else 0
        self._src.emit("end-of-stream")
        self._pipeline.get_bus().timed_pop_filtered(ahead + Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        self._pipeline.set_state(Gst.State.NULL)


def _make_chime() -> bytes:
    """A soft two-tone "ding-dong" announcing incoming audio."""
    out = array.array("h")
    for freq, dur in ((1318.5, 0.18), (1046.5, 0.40)):  # E6, C6
        n = int(SAMPLE_RATE * dur)
        release = SAMPLE_RATE * 0.02
        for i in range(n):
            t = i / SAMPLE_RATE
            env = min(1.0, t / 0.004, (n - i) / release) * math.exp(-t * 6)
            v = math.sin(2 * math.pi * freq * t) + 0.2 * math.sin(4 * math.pi * freq * t)
            out.append(int(9000 * env * v))
    out.extend([0] * int(SAMPLE_RATE * 0.05))
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


CHIME = _make_chime()


def _make_alarm_tone() -> bytes:
    """One cycle of the alarm: four short beeps, then a pause (about 1.3 s)."""
    out = array.array("h")
    beep, gap, pause = int(SAMPLE_RATE * 0.11), int(SAMPLE_RATE * 0.07), int(SAMPLE_RATE * 0.55)
    ramp = SAMPLE_RATE * 0.005
    for _ in range(4):
        for i in range(beep):
            env = min(1.0, i / ramp, (beep - i) / ramp)
            t = i / SAMPLE_RATE
            out.append(int(12000 * env * (math.sin(2 * math.pi * 1000 * t) + 0.3 * math.sin(2 * math.pi * 3000 * t))))
        out.extend([0] * gap)
    out.extend([0] * pause)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


ALARM_TONE = _make_alarm_tone()


class AlarmSound:
    """Plays the alarm tone over and over until stop()."""

    def __init__(self, make_sink: Callable[[], Gst.Element], volume: float = 1.0):
        self._make_sink = make_sink
        self._volume = volume
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="alarm-sound", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            player = Player(self._make_sink(), self._volume)
        except Exception:
            log.exception("Could not play the alarm")
            return
        chunk = BYTES_PER_SECOND // 10
        started, sent = time.monotonic(), 0
        while not self._stop.is_set():
            for i in range(0, len(ALARM_TONE), chunk):
                if self._stop.is_set():
                    break
                data = ALARM_TONE[i:i + chunk]
                player.write(data)
                sent += len(data)
                # Stay about 0.2 s ahead of playback, so the tone is seamless but stops quickly.
                ahead = started + sent / BYTES_PER_SECOND - time.monotonic() - 0.2
                if ahead > 0:
                    self._stop.wait(ahead)
        player.close()


def play_pcm_async(sink: Gst.Element, pcm: bytes, volume: float = 1.0) -> None:
    """Play a short PCM clip (e.g. the chime) on a background thread."""

    def run():
        try:
            player = Player(sink, volume, live=False)
            chunk = BYTES_PER_SECOND // 10
            for i in range(0, len(pcm), chunk):
                player.write(pcm[i:i + chunk])
            player.close()
        except Exception:
            log.exception("Could not play sound")

    threading.Thread(target=run, name="play-clip", daemon=True).start()
