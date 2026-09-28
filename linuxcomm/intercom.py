"""LAN intercom over plain HTTP.

Every LinuxComm station runs a small HTTP server (port 80 by default):

    GET  /linuxcomm/             human-readable status page (handy for testing from a browser)
    GET  /linuxcomm/api/status   JSON describing the station: name, do-not-disturb, ...
    POST /linuxcomm/api/ring     "may I talk to you?" – checks the network key and do-not-disturb
    POST /linuxcomm/api/talk     live audio: a chunked request body of raw PCM, played as it arrives

The /linuxcomm prefix lets a station sit behind a reverse proxy (see README.md for nginx).
Stations before version 0.0.6 used the same paths without the prefix; those are still
accepted so older stations can call this one.

Audio is signed 16-bit little-endian mono PCM at 16 kHz (32 KB/s per stream).
When a network key is configured, requests carry an HMAC-SHA256 of a timestamp and
the caller's name, so the key itself never crosses the network.

This module has no GTK/GStreamer dependency so it can be tested on its own.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import html
import http.client
import json
import logging
import os
import queue
import re
import socket
import socketserver
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterator, Protocol
from urllib.parse import quote, unquote, urlsplit

from . import APP_NAME, __version__

log = logging.getLogger(__name__)

PORT = int(os.environ.get("LINUXCOMM_PORT", "80"))
# Every station serves its API under this path, the same everywhere, so a station can also
# sit behind a reverse proxy: http://example.com/linuxcomm -> http://station/linuxcomm.
BASE_PATH = "/linuxcomm"
PROTOCOL_VERSION = 2

SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2
AUDIO_CONTENT_TYPE = f"audio/x-linuxcomm; format=S16LE; rate={SAMPLE_RATE}; channels={CHANNELS}"

H_STATION = "X-LinuxComm-Station"
H_AUTH = "X-LinuxComm-Auth"
H_BROADCAST = "X-LinuxComm-Broadcast"
USER_AGENT = f"{APP_NAME}/{__version__}"

AUTH_WINDOW_S = 300          # tolerated clock difference between stations
CONNECT_TIMEOUT_S = 3.0
STREAM_TIMEOUT_S = 10.0      # silence on a live stream before it is considered dead
MAX_CHUNK = 256 * 1024
SEND_QUEUE_CHUNKS = 200      # ~2 s of audio per peer before the oldest is dropped

# Peer states reported by PeerMonitor
ONLINE, DND, OFFLINE, FOREIGN, SELF, UNKNOWN = "online", "dnd", "offline", "foreign", "self", "unknown"
# Per-peer states reported while talking
CONNECTING, LIVE, REFUSED, FAILED, ENDED = "connecting", "live", "refused", "failed", "ended"


# -- helpers ----------------------------------------------------------------------

def parse_address(address: str, default_port: int = PORT) -> tuple[str, int]:
    """Split a station address into (host, port).

    Accepts "host", "host:port", "[v6]:port", or a URL such as "http://example.com/linuxcomm"
    (e.g. a station behind a reverse proxy). The path is always BASE_PATH, so a URL may
    contain that path or none. ValueError messages are meant to be shown to the user.
    """
    a = address.strip()
    if a.lower().startswith("https://"):
        raise ValueError("Use http://; LinuxComm connects over plain HTTP")
    if a.lower().startswith("http://"):
        a = a[len("http://"):]
    a, slash, path = a.partition("/")
    if slash and ("/" + path).rstrip("/") not in ("", BASE_PATH):
        raise ValueError(f"The path must be {BASE_PATH} (or left out)")
    if not a:
        raise ValueError("Enter an IP address or host name")
    port_text = ""
    if a.startswith("["):
        host, _, rest = a[1:].partition("]")
        if rest.startswith(":"):
            port_text = rest[1:]
        elif rest:
            raise ValueError("Not a valid IPv6 address")
    elif a.count(":") == 1:
        host, port_text = a.split(":")
    else:
        host = a  # plain host name, IPv4 or bare IPv6 address
    if not host or any(c.isspace() for c in host):
        raise ValueError("Not a valid IP address or host name")
    if not port_text:
        return host, default_port
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise ValueError("The port must be a number from 1 to 65535")
    return host, int(port_text)


def make_auth(key: str, station: str, now: float | None = None) -> str:
    ts = str(int(time.time() if now is None else now))
    mac = hmac.new(key.encode(), f"{ts}\n{station}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}:{mac}"


def check_auth(key: str, station: str, header: str | None) -> str | None:
    """Return None if the request is authorised, otherwise a reason to show the caller."""
    if not key:
        return None
    if not header:
        return "This station requires a network key"
    ts, _, mac = header.partition(":")
    try:
        ts_value = int(ts)
    except ValueError:
        return "Malformed authentication"
    expected = hmac.new(key.encode(), f"{ts}\n{station}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, expected):
        return "Wrong network key"
    if abs(time.time() - ts_value) > AUTH_WINDOW_S:
        return "The clocks of the two stations differ too much"
    return None


def describe_error(exc: BaseException) -> str:
    """Turn a network exception into something a person can act on."""
    if isinstance(exc, socket.gaierror):
        return "Unknown host name"
    if isinstance(exc, ConnectionRefusedError):
        return "LinuxComm is not running there"
    if isinstance(exc, TimeoutError):
        return "Not responding"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
        return "Connection lost"
    if isinstance(exc, OSError) and exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "Unreachable"
    if isinstance(exc, http.client.HTTPException):
        return "Unexpected reply"
    return str(exc) or exc.__class__.__name__


def _read_json(resp: http.client.HTTPResponse) -> dict:
    try:
        data = json.loads(resp.read(65536) or b"null")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# -- server -------------------------------------------------------------------------

@dataclass
class IncomingCall:
    id: str
    caller: str         # the caller's station name
    address: str        # the caller's IP address
    broadcast: bool     # sent to every station rather than just this one
    started: float


class AudioSink(Protocol):
    def write(self, pcm: bytes) -> None: ...
    def close(self) -> None: ...


class StationDelegate(Protocol):
    """What the server needs from the application. Methods run on server threads."""

    instance_id: str

    def station_name(self) -> str: ...
    def network_key(self) -> str: ...
    def do_not_disturb(self) -> bool: ...
    def incoming_started(self, call: IncomingCall) -> AudioSink: ...
    def incoming_finished(self, call: IncomingCall) -> None: ...
    # Alarms, for other stations to read and set (each returns all alarms as JSON; ValueError = bad request)
    def alarms(self) -> dict: ...
    def set_alarm(self, slot: int, time: str, enabled: bool, caller: str) -> dict: ...
    def delete_alarm(self, slot: int, caller: str) -> dict: ...


class _Handler(BaseHTTPRequestHandler):
    server_version = USER_AGENT
    protocol_version = "HTTP/1.1"
    timeout = STREAM_TIMEOUT_S

    server: _HTTPServer

    def log_message(self, fmt, *args):
        log.debug("%s %s", self._client_ip(), fmt % args)

    def _client_ip(self) -> str:
        """The caller's IP address, as reported by a reverse proxy (nginx) if there is one."""
        headers = getattr(self, "headers", None)  # not parsed yet when a malformed request is logged
        forwarded = ""
        if headers is not None:
            forwarded = headers.get("X-Real-IP") or headers.get("X-Forwarded-For", "").split(",")[0]
        ip = forwarded.strip() or self.client_address[0]
        return ip.removeprefix("::ffff:")

    def _route(self) -> str:
        """The request path below BASE_PATH ("/", "/api/status", ...), or "" if it isn't ours."""
        path = urlsplit(self.path).path
        if path == BASE_PATH or path.startswith(BASE_PATH + "/"):
            return path[len(BASE_PATH):] or "/"
        if path == "/" or path.startswith("/api/"):  # stations before 0.0.6 used no prefix
            return path
        return ""

    def _status(self) -> dict:
        d = self.server.delegate
        return {
            "app": APP_NAME,
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "instance": d.instance_id,
            "name": d.station_name(),
            "hostname": socket.gethostname(),
            "dnd": bool(d.do_not_disturb()),
            "key_required": bool(d.network_key()),
        }

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = self._route()
        if route.startswith("/api/alarms"):
            self._alarms_request(route)
        elif route == "/api/status":
            self._send_json(200, self._status())
        elif route == "/":
            s = self._status()
            page = (
                "<!doctype html><meta charset=utf-8><title>{app}</title>"
                "<body style='font-family:sans-serif;margin:3em'>"
                "<h1>{name}</h1><p>{app} {version} intercom station is running on {host}.</p>"
                "<p>Do not disturb: {dnd}</p>"
            ).format(app=APP_NAME, version=__version__, name=html.escape(s["name"]),
                     host=html.escape(s["hostname"]), dnd="on" if s["dnd"] else "off")
            self._send(200, page.encode(), "text/html; charset=utf-8")
        else:
            self._send_json(404, {"ok": False, "reason": "Not found"})

    def do_PUT(self):
        self._alarms_request(self._route())

    def do_DELETE(self):
        self._alarms_request(self._route())

    def _alarms_request(self, route: str) -> None:
        """GET /api/alarms, PUT /api/alarms/<1-3> {"time": "07:30", "enabled": true}, DELETE /api/alarms/<1-3>.

        Always answers with every alarm: {"ok": true, "alarms": {"1": {...} | null, "2": ..., "3": ...}}.
        """
        delegate = self.server.delegate
        match = re.fullmatch(r"/api/alarms(?:/(\d+))?", route)
        method = self.command
        if not match or (method == "GET") != (match.group(1) is None):
            self.close_connection = True
            self._send_json(404 if not match else 405, {"ok": False, "reason": "Not found"})
            return
        caller = unquote(self.headers.get(H_STATION, "")).strip()[:64] or self._client_ip()
        reason = check_auth(delegate.network_key(), caller, self.headers.get(H_AUTH))
        if reason:
            self.close_connection = True
            self._send_json(403, {"ok": False, "reason": reason})
            return
        try:
            if method == "GET":
                alarms = delegate.alarms()
            elif method == "PUT":
                body = b"".join(self._iter_body())
                data = json.loads(body[:4096] or b"null")
                if not isinstance(data, dict):
                    raise ValueError("Send the alarm as JSON")
                alarms = delegate.set_alarm(int(match.group(1)), data.get("time"),
                                            bool(data.get("enabled", True)), caller)
            else:
                alarms = delegate.delete_alarm(int(match.group(1)), caller)
        except ValueError as e:  # includes bad JSON
            self._send_json(400, {"ok": False, "reason": str(e)})
        except OSError:
            log.exception("Could not change alarms")
            self._send_json(500, {"ok": False, "reason": "That station could not save its alarms"})
        else:
            self._send_json(200, {"ok": True, "alarms": alarms})

    def do_POST(self):
        route = self._route()
        if route not in ("/api/ring", "/api/talk"):
            self.close_connection = True
            self._send_json(404, {"ok": False, "reason": "Not found"})
            return
        delegate = self.server.delegate
        caller = unquote(self.headers.get(H_STATION, "")).strip()[:64] or self._client_ip()
        reason = check_auth(delegate.network_key(), caller, self.headers.get(H_AUTH))
        status = 403
        if reason is None and delegate.do_not_disturb():
            reason, status = "Do not disturb is on", 409
        if reason:
            self.close_connection = True
            self._send_json(status, {"ok": False, "reason": reason})
            return
        if route == "/api/ring":
            for _ in self._iter_body():
                pass
            self._send_json(200, {"ok": True, **self._status()})
        else:
            self._receive_audio(caller)

    def _receive_audio(self, caller: str) -> None:
        delegate = self.server.delegate
        call = IncomingCall(
            id=uuid.uuid4().hex,
            caller=caller,
            address=self._client_ip(),
            broadcast=self.headers.get(H_BROADCAST) == "1",
            started=time.time(),
        )
        try:
            sink = delegate.incoming_started(call)
        except Exception:
            log.exception("Could not start playback for %s", caller)
            self.close_connection = True
            self._send_json(500, {"ok": False, "reason": "Audio playback failed on the receiving station"})
            return
        log.info("Receiving audio from %s (%s)%s", caller, call.address, " [all stations]" if call.broadcast else "")
        completed = False
        try:
            for chunk in self._iter_body():
                sink.write(chunk)
            completed = True
        except (OSError, ValueError) as e:
            log.info("Stream from %s ended abnormally: %s", caller, e)
        finally:
            try:
                sink.close()
            except Exception:
                log.exception("Error closing playback")
            delegate.incoming_finished(call)
        if completed:
            self._send_json(200, {"ok": True})
        else:
            self.close_connection = True

    def _iter_body(self) -> Iterator[bytes]:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            while True:
                line = self.rfile.readline(1024)
                if not line:
                    raise ConnectionError("connection closed mid-stream")
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass  # skip trailers
                    return
                if size > MAX_CHUNK:
                    raise ValueError("chunk too large")
                data = self.rfile.read(size)
                if len(data) < size:
                    raise ConnectionError("connection closed mid-chunk")
                self.rfile.readline(1024)  # CRLF after the chunk
                yield data
        else:
            remaining = int(self.headers.get("Content-Length") or 0)
            while remaining > 0:
                data = self.rfile.read(min(remaining, 8192))
                if not data:
                    raise ConnectionError("connection closed mid-body")
                remaining -= len(data)
                yield data


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address, family: int, delegate: StationDelegate):
        self.address_family = family
        self.delegate = delegate
        super().__init__(address, _Handler)

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            with contextlib.suppress(OSError, AttributeError):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        # Skip HTTPServer.server_bind(): its getfqdn() can stall on a reverse DNS lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name = socket.gethostname()
        self.server_port = self.server_address[1]


class IntercomServer:
    """Listens for other stations. start() raises OSError if the port can't be bound."""

    def __init__(self, delegate: StationDelegate, port: int = PORT):
        self.delegate = delegate
        self.port = port
        self._httpd: _HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        last_error: OSError | None = None
        for family, host in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
            if family == socket.AF_INET6 and not socket.has_ipv6:
                continue
            try:
                self._httpd = _HTTPServer((host, self.port), family, self.delegate)
                break
            except OSError as e:
                # Permission and "address in use" errors would repeat on IPv4 too.
                if isinstance(e, PermissionError) or e.errno == errno.EADDRINUSE:
                    raise
                last_error = e
        else:
            raise last_error or OSError("could not create a listening socket")
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="intercom-server", daemon=True)
        self._thread.start()
        log.info("Intercom listening on port %d", self.port)

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# -- peer presence ------------------------------------------------------------------

@dataclass
class PeerStatus:
    state: str
    detail: str = ""
    name: str = ""      # the station name the peer reports
    ip: str = ""        # the IP address the peer's host name resolved to


def describe_http_status(status: int, server: str = "") -> str:
    """Explain an HTTP error from a station or a reverse proxy in front of it."""
    if status == 404 and server.startswith(APP_NAME + "/"):
        return "Runs an older LinuxComm; update it"
    if status == 404:
        return f"No LinuxComm station at {BASE_PATH} there"
    if status in (502, 503, 504):
        return "The proxy there can't reach the station"
    if status in (301, 302, 307, 308):
        return f"Redirected (to HTTPS?); {BASE_PATH} must be served over plain HTTP"
    if status == 413:
        return "The proxy there limits upload size (see client_max_body_size in the README)"
    return f"Unexpected reply (HTTP {status})"


class HTTPStatusError(Exception):
    def __init__(self, status: int, server: str = ""):
        super().__init__(describe_http_status(status, server))
        self.status = status


def fetch_status(address: str, timeout: float = CONNECT_TIMEOUT_S) -> tuple[dict, str]:
    host, port = parse_address(address)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("GET", BASE_PATH + "/api/status",
                     headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        ip = ""
        with contextlib.suppress(OSError, AttributeError, IndexError):
            ip = conn.sock.getpeername()[0]
        resp = conn.getresponse()
        body = resp.read(65536)
        if resp.status != 200:
            raise HTTPStatusError(resp.status, resp.getheader("Server", ""))
        info = json.loads(body or b"null")
    finally:
        conn.close()
    if not isinstance(info, dict) or info.get("app") != APP_NAME:
        raise ValueError("not a LinuxComm station")
    return info, ip.removeprefix("::ffff:")


def check_peer(address: str, own_instance: str = "") -> PeerStatus:
    try:
        parse_address(address)
    except ValueError as e:
        return PeerStatus(OFFLINE, str(e))
    try:
        info, ip = fetch_status(address)
    except HTTPStatusError as e:
        return PeerStatus(OFFLINE if e.status in (502, 503, 504) else FOREIGN, str(e))
    except (ValueError, http.client.HTTPException):
        return PeerStatus(FOREIGN, "Something other than LinuxComm answers there")
    except OSError as e:
        return PeerStatus(OFFLINE, describe_error(e))
    name = str(info.get("name") or "")
    if own_instance and info.get("instance") == own_instance:
        return PeerStatus(SELF, "This station", name, ip)
    if info.get("dnd"):
        return PeerStatus(DND, "Do not disturb", name, ip)
    return PeerStatus(ONLINE, "Online", name, ip)


class PeerMonitor:
    """Polls every peer's /api/status in the background and reports changes."""

    def __init__(self, get_peers: Callable[[], list[tuple[str, str]]],
                 on_status: Callable[[str, PeerStatus], None],
                 own_instance: str = "", interval: float = 10.0):
        self._get_peers = get_peers
        self._on_status = on_status
        self._own = own_instance
        self._interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="peer-check")
        self._thread = threading.Thread(target=self._run, name="peer-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def refresh(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                futures = {self._pool.submit(check_peer, addr, self._own): pid for pid, addr in self._get_peers()}
                for fut in as_completed(futures):
                    if self._stop.is_set():
                        return
                    self._on_status(futures[fut], fut.result())
            except RuntimeError:
                return  # pool shut down while we were submitting
            except Exception:
                log.exception("Peer status check failed")
            self._wake.wait(self._interval)


# -- outgoing audio ---------------------------------------------------------------

class _Refused(Exception):
    pass


def _refusal(resp: http.client.HTTPResponse, payload: dict) -> str:
    """Why a station (or a proxy in front of it) refused a request."""
    if resp.status in (400, 403, 409, 500) and payload.get("reason"):
        return str(payload["reason"])  # our own server: wrong key, do not disturb, ...
    return describe_http_status(resp.status, resp.getheader("Server", ""))


# -- another station's alarms -------------------------------------------------------------

class RemoteError(Exception):
    """A request to another station failed; str() is a message for the user."""


def _alarms_call(address: str, method: str, path: str, station: str, key: str, body: dict | None = None) -> dict:
    try:
        host, port = parse_address(address)
    except ValueError as e:
        raise RemoteError(str(e)) from None
    headers = {H_STATION: quote(station), "User-Agent": USER_AGENT, "Accept": "application/json"}
    if key:
        headers[H_AUTH] = make_auth(key, station)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT_S * 2)
    try:
        conn.request(method, BASE_PATH + path, body=data, headers=headers)
        resp = conn.getresponse()
        payload = _read_json(resp)
    except (OSError, http.client.HTTPException) as e:
        raise RemoteError(describe_error(e)) from None
    finally:
        conn.close()
    if resp.status != 200 or not payload.get("ok") or not isinstance(payload.get("alarms"), dict):
        raise RemoteError(_refusal(resp, payload))
    return payload["alarms"]


def fetch_alarms(address: str, station: str, key: str) -> dict:
    """Another station's alarms: {"1": {"time": "07:30", "enabled": true} | None, "2": ..., "3": ...}."""
    return _alarms_call(address, "GET", "/api/alarms", station, key)


def set_remote_alarm(address: str, slot: int, time: str, enabled: bool, station: str, key: str) -> dict:
    return _alarms_call(address, "PUT", f"/api/alarms/{slot}", station, key, {"time": time, "enabled": enabled})


def delete_remote_alarm(address: str, slot: int, station: str, key: str) -> dict:
    return _alarms_call(address, "DELETE", f"/api/alarms/{slot}", station, key)


class _Sender(threading.Thread):
    """Streams queued audio to a single station over one chunked HTTP POST."""

    def __init__(self, target_id: str, address: str, station_name: str, network_key: str,
                 broadcast: bool, on_state: Callable[[str, str, str], None]):
        super().__init__(name=f"talk:{address}", daemon=True)
        self.target_id = target_id
        self.address = address
        self._station_name = station_name
        self._key = network_key
        self._broadcast = broadcast
        self._on_state = on_state
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=SEND_QUEUE_CHUNKS)
        self._finished = threading.Event()

    def put(self, pcm: bytes) -> None:
        if self._finished.is_set():
            return
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            # The link can't keep up: drop the oldest audio so latency stays bounded.
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(pcm)

    def finish(self) -> None:
        self._finished.set()

    def _report(self, state: str, detail: str = "") -> None:
        try:
            self._on_state(self.target_id, state, detail)
        except Exception:
            log.exception("Talk state callback failed")

    def _headers(self) -> dict[str, str]:
        headers = {H_STATION: quote(self._station_name), "User-Agent": USER_AGENT}
        if self._broadcast:
            headers[H_BROADCAST] = "1"
        if self._key:
            headers[H_AUTH] = make_auth(self._key, self._station_name)
        return headers

    def run(self) -> None:
        self._report(CONNECTING)
        try:
            host, port = parse_address(self.address)
            self._ring(host, port)
            self._stream(host, port)
        except _Refused as e:
            self._report(REFUSED, str(e))
        except (OSError, http.client.HTTPException, ValueError) as e:
            self._report(FAILED, describe_error(e))
        else:
            self._report(ENDED)
        finally:
            self._finished.set()

    def _ring(self, host: str, port: int) -> None:
        conn = http.client.HTTPConnection(host, port, timeout=CONNECT_TIMEOUT_S)
        try:
            conn.request("POST", BASE_PATH + "/api/ring", body=b"", headers=self._headers())
            resp = conn.getresponse()
            payload = _read_json(resp)
        finally:
            conn.close()
        if resp.status != 200 or not payload.get("ok"):
            raise _Refused(_refusal(resp, payload))

    def _next_payload(self) -> bytes | None:
        while True:
            try:
                first = self._queue.get(timeout=0.1)
                break
            except queue.Empty:
                if self._finished.is_set():
                    return None
        parts, size = [first], len(first)
        while size < 16384:  # coalesce whatever queued up while we were sending
            try:
                more = self._queue.get_nowait()
            except queue.Empty:
                break
            parts.append(more)
            size += len(more)
        return b"".join(parts)

    def _stream(self, host: str, port: int) -> None:
        conn = http.client.HTTPConnection(host, port, timeout=STREAM_TIMEOUT_S)
        try:
            conn.connect()
            with contextlib.suppress(OSError):
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.putrequest("POST", BASE_PATH + "/api/talk", skip_accept_encoding=True)
            for name, value in self._headers().items():
                conn.putheader(name, value)
            conn.putheader("Content-Type", AUDIO_CONTENT_TYPE)
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            self._report(LIVE)
            while (payload := self._next_payload()) is not None:
                conn.send(b"%X\r\n%s\r\n" % (len(payload), payload))
            conn.send(b"0\r\n\r\n")
            resp = conn.getresponse()
            payload = _read_json(resp)
            if resp.status != 200:
                raise _Refused(_refusal(resp, payload))
        finally:
            conn.close()


class TalkSession:
    """One transmission to one or more stations.

    feed() is called from the audio capture thread. Every target gets its own
    sender thread, so a slow or unreachable station never holds up the others.
    on_state(target_id, state, detail) is called from those sender threads.
    """

    def __init__(self, targets: list[tuple[str, str]], station_name: str, network_key: str,
                 broadcast: bool, on_state: Callable[[str, str, str], None]):
        self.broadcast = broadcast
        self._senders = [_Sender(tid, addr, station_name, network_key, broadcast, on_state)
                         for tid, addr in targets]

    @property
    def target_ids(self) -> list[str]:
        return [s.target_id for s in self._senders]

    def start(self) -> None:
        for s in self._senders:
            s.start()

    def feed(self, pcm: bytes) -> None:
        for s in self._senders:
            s.put(pcm)

    def stop(self) -> None:
        for s in self._senders:
            s.finish()

    def join(self, timeout: float | None = None) -> None:
        for s in self._senders:
            s.join(timeout)
