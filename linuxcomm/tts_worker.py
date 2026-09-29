"""Text-to-speech worker process, run in LinuxComm's private Python environment.

    python3 -m linuxcomm.tts_worker coqui --models DIR   Coqui TTS: natural voices (needs PyTorch)
    python3 -m linuxcomm.tts_worker pyttsx3              eSpeak NG voices through pyttsx3
    ... --prepare                                        load (Coqui: download the model), list, exit

Running the engines in their own process keeps PyTorch's memory and any crash out of the app.
Protocol, one JSON object per line:

    worker -> app, once:  {"ready": true, "voices": [{"id": "p225", "name": "..."}, ...]}
                          or {"ready": false, "error": "..."} (and the worker exits)
    app -> worker:        {"say": "Dinner is ready.", "voice": "p225"}
    worker -> app:        {"ok": true, "rate": 22050, "bytes": N} and then N bytes of 16-bit
                          little-endian mono PCM, or {"ok": false, "error": "..."}

Whatever the libraries print goes to stderr, so it can't get mixed into the protocol.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import wave

COQUI_MODEL = "tts_models/en/vctk/vits"   # English, 109 voices, ~150 MB, fast enough for a Pi 5


class Coqui:
    def __init__(self, models: str):
        os.environ.setdefault("TTS_HOME", os.path.join(models, "coqui"))
        import numpy  # noqa: F401  (fail here, before the slow part, if it is missing)
        import torch
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        from TTS.api import TTS
        folder = os.path.join(os.environ["TTS_HOME"], "tts", COQUI_MODEL.replace("/", "--"))
        model, config = os.path.join(folder, "model.pth"), os.path.join(folder, "config.json")
        if os.path.exists(model) and os.path.exists(config):
            # Load the files directly: Coqui's model manager rewrites config.json on every load,
            # which fails for users, since the installer (root) owns it.
            self._tts = TTS(model_path=model, config_path=config, progress_bar=False)
        else:
            self._tts = TTS(COQUI_MODEL, progress_bar=False)  # downloads it (the installer does this)
        self._rate = self._tts.synthesizer.output_sample_rate
        # Speaker names can carry stray whitespace ("ED\n"); the model wants them exactly.
        self._speakers = {s.strip(): s for s in (self._tts.speakers or []) if s.strip()}

    def voices(self) -> list[dict]:
        return [{"id": s, "name": f"Natural voice {s}"} for s in self._speakers]

    def say(self, text: str, voice: str | None) -> tuple[int, bytes]:
        import numpy as np
        speaker = self._speakers.get(voice or "") or next(iter(self._speakers.values()), None)
        wav = np.asarray(self._tts.tts(text=text, speaker=speaker), dtype=np.float32)
        return self._rate, (np.clip(wav, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class Pyttsx3:
    def __init__(self):
        import pyttsx3
        self._engine = pyttsx3.init()
        self._voices = {v.id: v.name for v in self._engine.getProperty("voices")}

    def voices(self) -> list[dict]:
        return [{"id": vid, "name": name} for vid, name in self._voices.items()]

    def say(self, text: str, voice: str | None) -> tuple[int, bytes]:
        if voice in self._voices:
            self._engine.setProperty("voice", voice)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "say.wav")
            self._engine.save_to_file(text, path)
            self._engine.runAndWait()
            with wave.open(path) as w:
                if w.getsampwidth() != 2:
                    raise ValueError(f"unexpected {w.getsampwidth() * 8}-bit audio")
                frames = w.readframes(w.getnframes())
                if w.getnchannels() == 2:  # keep the left channel
                    frames = b"".join(frames[i:i + 2] for i in range(0, len(frames), 4))
                return w.getframerate(), frames


def main(argv: list[str]) -> int:
    # The protocol goes to the real stdout; everything else printed goes to stderr.
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def send(message: dict, data: bytes = b"") -> None:
        out.write(json.dumps(message).encode() + b"\n" + data)

    name = argv[1] if len(argv) > 1 else ""
    try:
        if name == "coqui":
            models = argv[argv.index("--models") + 1] if "--models" in argv else "/opt/linuxcomm/models"
            engine = Coqui(models)
        elif name == "pyttsx3":
            engine = Pyttsx3()
        else:
            raise ValueError(f"unknown engine {name!r}")
    except Exception as e:  # not installed, model missing, ...
        send({"ready": False, "error": f"{e.__class__.__name__}: {e}"})
        return 1
    send({"ready": True, "voices": engine.voices()})
    if "--prepare" in argv:
        return 0
    for line in sys.stdin:
        try:
            request = json.loads(line)
            rate, pcm = engine.say(str(request["say"]), request.get("voice"))
        except Exception as e:
            send({"ok": False, "error": f"{e.__class__.__name__}: {e}"})
        else:
            send({"ok": True, "rate": rate, "bytes": len(pcm)}, pcm)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
