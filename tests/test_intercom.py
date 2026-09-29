"""Protocol tests. They need no GTK/GStreamer and no root: servers use a random port.

Run with:  python3 -m unittest discover -s tests -v
"""

import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import alarms, intercom  # noqa: E402


class FakeSink:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, pcm):
        self.data += pcm

    def close(self):
        self.closed = True


class FakeStation:
    def __init__(self, name="Kitchen", key="", dnd=False):
        self.instance_id = "instance-" + name
        self.name, self.key, self.dnd = name, key, dnd
        self.calls, self.sinks = [], []
        self.finished = threading.Event()

    def station_name(self):
        return self.name

    def network_key(self):
        return self.key

    def do_not_disturb(self):
        return self.dnd

    def incoming_started(self, call):
        self.calls.append(call)
        sink = FakeSink()
        self.sinks.append(sink)
        return sink

    def incoming_finished(self, call):
        self.finished.set()

    # alarms, backed by a real AlarmStore in a temporary folder
    def _store(self):
        if not hasattr(self, "store"):
            self.store = alarms.AlarmStore(Path(tempfile.mkdtemp()) / "alarms.json")
            self.alarm_changes = []
        return self.store

    def alarms(self):
        return self._store().to_json()

    def set_alarm(self, slot, time_text, enabled, caller):
        hour, minute = alarms.parse_time(time_text)
        self._store().set(slot, hour, minute, enabled)
        self.alarm_changes.append((caller, slot, time_text, enabled))
        return self.store.to_json()

    def delete_alarm(self, slot, caller):
        self._store().delete(slot)
        self.alarm_changes.append((caller, slot, None, None))
        return self.store.to_json()

    def receive_text(self, caller, address, text):
        self.__dict__.setdefault("texts", []).append((caller, address, text))


class Recorder:
    def __init__(self):
        self.states = []
        self.lock = threading.Lock()

    def __call__(self, target_id, state, detail):
        with self.lock:
            self.states.append((target_id, state, detail))


def start_server(station):
    server = intercom.IntercomServer(station, port=0)
    server.start()
    return server, f"127.0.0.1:{server.port}"


def talk(address, chunks, name="Office", key="", broadcast=False):
    recorder = Recorder()
    session = intercom.TalkSession([("peer", address)], name, key, broadcast, recorder)
    for chunk in chunks:
        session.feed(chunk)
    session.start()
    time.sleep(0.3)
    session.stop()
    session.join(10)
    return recorder.states


class ParseAddressTests(unittest.TestCase):
    def test_forms(self):
        p = intercom.parse_address
        self.assertEqual(p("kitchen", 80), ("kitchen", 80))
        self.assertEqual(p("kitchen.local:8080", 80), ("kitchen.local", 8080))
        self.assertEqual(p("192.168.1.20", 80), ("192.168.1.20", 80))
        self.assertEqual(p("http://192.168.1.20/", 80), ("192.168.1.20", 80))
        self.assertEqual(p("[fe80::1]:81", 80), ("fe80::1", 81))
        self.assertEqual(p("fe80::1", 80), ("fe80::1", 80))
        for bad in ("", "   ", "host:0", "host:99999", "host:abc", "two words"):
            with self.assertRaises(ValueError, msg=bad):
                p(bad)

    def test_reverse_proxy_urls(self):
        p = intercom.parse_address
        self.assertEqual(p("http://swarmsoft.com/linuxcomm", 80), ("swarmsoft.com", 80))
        self.assertEqual(p("http://swarmsoft.com/linuxcomm/", 80), ("swarmsoft.com", 80))
        self.assertEqual(p("HTTP://swarmsoft.com:8080/linuxcomm", 80), ("swarmsoft.com", 8080))
        self.assertEqual(p("swarmsoft.com/linuxcomm", 80), ("swarmsoft.com", 80))
        with self.assertRaisesRegex(ValueError, "path must be /linuxcomm"):
            p("http://swarmsoft.com/intercom")
        with self.assertRaisesRegex(ValueError, "http://"):
            p("https://swarmsoft.com/linuxcomm")


class AuthTests(unittest.TestCase):
    def test_auth(self):
        header = intercom.make_auth("secret", "Office")
        self.assertIsNone(intercom.check_auth("secret", "Office", header))
        self.assertIsNone(intercom.check_auth("", "Office", None))
        self.assertEqual(intercom.check_auth("secret", "Office", None), "This station requires a network key")
        self.assertEqual(intercom.check_auth("other", "Office", header), "Wrong network key")
        self.assertEqual(intercom.check_auth("secret", "Mallory", header), "Wrong network key")
        old = intercom.make_auth("secret", "Office", now=time.time() - 3600)
        self.assertIn("clocks", intercom.check_auth("secret", "Office", old))


class TalkTests(unittest.TestCase):
    def test_audio_arrives_intact(self):
        station = FakeStation()
        server, address = start_server(station)
        try:
            chunks = [bytes([i]) * 640 for i in range(50)]
            states = talk(address, chunks, broadcast=True)
            self.assertTrue(station.finished.wait(5))
            self.assertEqual([s for _, s, _ in states], [intercom.CONNECTING, intercom.LIVE, intercom.ENDED])
            self.assertEqual(bytes(station.sinks[0].data), b"".join(chunks))
            self.assertTrue(station.sinks[0].closed)
            call = station.calls[0]
            self.assertEqual(call.caller, "Office")
            self.assertEqual(call.address, "127.0.0.1")
            self.assertTrue(call.broadcast)
        finally:
            server.stop()

    def test_unicode_station_name(self):
        station = FakeStation()
        server, address = start_server(station)
        try:
            talk(address, [b"\0\0" * 160], name="Küche Ω")
            self.assertTrue(station.finished.wait(5))
            self.assertEqual(station.calls[0].caller, "Küche Ω")
        finally:
            server.stop()

    def test_do_not_disturb_refuses(self):
        station = FakeStation(dnd=True)
        server, address = start_server(station)
        try:
            states = talk(address, [b"\0\0" * 160])
            self.assertEqual(states[-1][1:], (intercom.REFUSED, "Do not disturb is on"))
            self.assertEqual(station.calls, [])
        finally:
            server.stop()

    def test_network_key(self):
        station = FakeStation(key="s3cret")
        server, address = start_server(station)
        try:
            states = talk(address, [b"\0\0" * 160], key="wrong")
            self.assertEqual(states[-1][1:], (intercom.REFUSED, "Wrong network key"))
            states = talk(address, [b"\1\0" * 160], key="s3cret")
            self.assertEqual(states[-1][1], intercom.ENDED)
            self.assertTrue(station.finished.wait(5))
        finally:
            server.stop()

    def test_unreachable_station_fails(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        states = talk(f"127.0.0.1:{port}", [b"\0\0" * 160])
        self.assertEqual(states[-1][1:], (intercom.FAILED, "LinuxComm is not running there"))


class HangUpTests(unittest.TestCase):
    """The receiving station ends a call while the caller is still talking."""

    def hang_up_during_a_talk(self, reason):
        station = FakeStation()
        server, address = start_server(station)
        self.addCleanup(server.stop)
        recorder = Recorder()
        session = intercom.TalkSession([("peer", address)], "Office", "", False, recorder)
        session.start()
        stop_feeding = threading.Event()

        def feed():  # like a microphone: 20 ms of audio every 20 ms
            while not stop_feeding.is_set():
                session.feed(b"\1\0" * 320)
                time.sleep(0.02)

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        self.addCleanup(stop_feeding.set)
        deadline = time.monotonic() + 5
        while not (station.sinks and station.sinks[0].data) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(station.sinks and station.sinks[0].data, "audio should be arriving")
        started = time.monotonic()
        station.calls[0].hang_up(reason)
        self.assertTrue(station.finished.wait(2), "the call ends on the receiving side")
        received = len(station.sinks[0].data)
        session.join(5)   # the sender notices on its own, while audio is still being fed
        elapsed = time.monotonic() - started
        stop_feeding.set()
        return station, recorder.states, received, elapsed

    def test_hang_up(self):
        station, states, received, elapsed = self.hang_up_during_a_talk("hangup")
        self.assertEqual(states[-1][1:], (intercom.HUNG_UP, "hangup"))
        self.assertLess(elapsed, 1.5, "the caller learns about it quickly")
        self.assertTrue(station.sinks[0].closed)
        time.sleep(0.2)
        self.assertEqual(len(station.sinks[0].data), received, "nothing is played after hanging up")

    def test_hang_up_to_reply(self):
        _, states, _, _ = self.hang_up_during_a_talk("reply")
        self.assertEqual(states[-1][1:], (intercom.HUNG_UP, "reply"))

    def test_hang_up_is_remembered(self):
        call = intercom.IncomingCall("id", "Office", "127.0.0.1", False, 0.0)
        self.assertFalse(call.hung_up)
        call.hang_up("reply")
        call.hang_up("hangup")  # the first reason counts
        self.assertEqual((call.hung_up, call.hangup_reason), (True, "reply"))


class PeerStatusTests(unittest.TestCase):
    def test_states(self):
        station = FakeStation(name="Garage")
        server, address = start_server(station)
        try:
            status = intercom.check_peer(address)
            self.assertEqual((status.state, status.name, status.ip), (intercom.ONLINE, "Garage", "127.0.0.1"))
            self.assertEqual(intercom.check_peer(address, own_instance="instance-Garage").state, intercom.SELF)
            station.dnd = True
            self.assertEqual(intercom.check_peer(address).state, intercom.DND)
        finally:
            server.stop()
        self.assertEqual(intercom.check_peer(address).state, intercom.OFFLINE)
        self.assertEqual(intercom.check_peer("bad address").state, intercom.OFFLINE)

    def test_foreign_web_server(self):
        class Html(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>It works!</html>")

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Html)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            status = intercom.check_peer(f"127.0.0.1:{httpd.server_address[1]}")
            self.assertEqual(status.state, intercom.FOREIGN)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_monitor_reports(self):
        station = FakeStation()
        server, address = start_server(station)
        got = {}
        done = threading.Event()

        def on_status(pid, status):
            got[pid] = status
            if len(got) == 2:
                done.set()

        monitor = intercom.PeerMonitor(lambda: [("a", address), ("b", "127.0.0.1:1")], on_status, interval=60)
        try:
            monitor.start()
            self.assertTrue(done.wait(10))
            self.assertEqual(got["a"].state, intercom.ONLINE)
            self.assertEqual(got["b"].state, intercom.OFFLINE)
        finally:
            monitor.stop()
            server.stop()


class RoutingTests(unittest.TestCase):
    """The /linuxcomm prefix, older stations' paths, and reverse-proxy headers."""

    def setUp(self):
        self.station = FakeStation(name="Office")
        self.server, self.address = start_server(self.station)
        self.port = self.server.port

    def tearDown(self):
        self.server.stop()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_paths(self):
        self.assertEqual(self.request("GET", "/linuxcomm/api/status")[0], 200)
        self.assertEqual(self.request("GET", "/api/status")[0], 200, "stations before 0.0.6 use this path")
        for page in ("/linuxcomm", "/linuxcomm/", "/"):
            status, body = self.request("GET", page)
            self.assertEqual(status, 200, page)
            self.assertIn(b"Office", body)
        self.assertEqual(self.request("GET", "/linuxcommx/api/status")[0], 404)
        self.assertEqual(self.request("GET", "/other")[0], 404)
        self.assertEqual(self.request("POST", "/linuxcomm/api/other", body=b"")[0], 404)

    def test_older_station_can_still_call(self):
        headers = {intercom.H_STATION: "Old", "Content-Type": "application/octet-stream"}
        self.assertEqual(self.request("POST", "/api/ring", body=b"", headers=headers)[0], 200)
        self.assertEqual(self.request("POST", "/api/talk", body=b"\1\0" * 50, headers=headers)[0], 200)
        self.assertTrue(self.station.finished.wait(5))
        self.assertEqual(bytes(self.station.sinks[0].data), b"\1\0" * 50)

    def test_caller_address_from_proxy_headers(self):
        cases = [({}, "127.0.0.1"),
                 ({"X-Forwarded-For": "203.0.113.5, 10.0.0.1"}, "203.0.113.5"),
                 ({"X-Real-IP": "198.51.100.7", "X-Forwarded-For": "203.0.113.5"}, "198.51.100.7")]
        for extra, expected in cases:
            self.station.finished.clear()
            headers = {intercom.H_STATION: "Kitchen", **extra}
            self.assertEqual(self.request("POST", "/linuxcomm/api/talk", body=b"\0\0", headers=headers)[0], 200)
            self.assertTrue(self.station.finished.wait(5))
            self.assertEqual(self.station.calls[-1].address, expected, extra)

    def test_talk_to_a_url_address(self):
        states = talk(f"http://127.0.0.1:{self.port}/linuxcomm", [b"\2\0" * 160])
        self.assertEqual(states[-1][1], intercom.ENDED)
        self.assertEqual(intercom.check_peer(f"http://127.0.0.1:{self.port}/linuxcomm/").state, intercom.ONLINE)


class RemoteAlarmTests(unittest.TestCase):
    """Reading and setting another station's alarms."""

    def setUp(self):
        self.station = FakeStation(name="Kitchen")
        self.server, self.address = start_server(self.station)
        self.addCleanup(self.server.stop)

    def test_read_set_turn_off_and_delete(self):
        self.assertEqual(intercom.fetch_alarms(self.address, "Office", ""), {"1": None, "2": None, "3": None})
        result = intercom.set_remote_alarm(self.address, 2, "06:45", True, "Office", "")
        self.assertEqual(result["2"], {"time": "06:45", "enabled": True})
        result = intercom.set_remote_alarm(self.address, 2, "06:45", False, "Office", "")
        self.assertEqual(result["2"], {"time": "06:45", "enabled": False})
        self.assertEqual(intercom.delete_remote_alarm(self.address, 2, "Office", "")["2"], None)
        self.assertEqual([c[:2] for c in self.station.alarm_changes], [("Office", 2)] * 3)

    def test_bad_requests_are_explained(self):
        for slot, time_text, message in ((2, "25:00", "00:00 to 23:59"), (4, "07:00", "alarms 1, 2 and 3")):
            with self.assertRaisesRegex(intercom.RemoteError, message):
                intercom.set_remote_alarm(self.address, slot, time_text, True, "Office", "")

    def test_network_key(self):
        self.station.key = "s3cret"
        with self.assertRaisesRegex(intercom.RemoteError, "network key"):
            intercom.fetch_alarms(self.address, "Office", "")
        with self.assertRaisesRegex(intercom.RemoteError, "Wrong network key"):
            intercom.set_remote_alarm(self.address, 1, "07:00", True, "Office", "wrong")
        self.assertEqual(intercom.set_remote_alarm(self.address, 1, "07:00", True, "Office", "s3cret")["1"]["time"],
                         "07:00")

    def test_unreachable_station(self):
        with self.assertRaisesRegex(intercom.RemoteError, "not running"):
            intercom.fetch_alarms("127.0.0.1:1", "Office", "")


class TextTests(unittest.TestCase):
    """Text messages between stations."""

    def setUp(self):
        self.station = FakeStation(name="Kitchen")
        self.server, self.address = start_server(self.station)
        self.addCleanup(self.server.stop)

    def test_send(self):
        intercom.send_text(self.address, "  Dinner is ready!\nCome down.\x07 ", "Office", "")
        self.assertEqual(self.station.texts, [("Office", "127.0.0.1", "Dinner is ready!\nCome down.")])

    def test_refusals_are_explained(self):
        for text, message in (("", "empty"), ("   \x00 ", "empty"), ("x" * 1001, "longer than 1000")):
            with self.assertRaisesRegex(intercom.RemoteError, message):
                intercom.send_text(self.address, text, "Office", "")
        self.station.dnd = True
        with self.assertRaisesRegex(intercom.RemoteError, "Do not disturb"):
            intercom.send_text(self.address, "Hello", "Office", "")
        self.station.dnd, self.station.key = False, "s3cret"
        with self.assertRaisesRegex(intercom.RemoteError, "network key"):
            intercom.send_text(self.address, "Hello", "Office", "")
        intercom.send_text(self.address, "Hello", "Office", "s3cret")
        self.assertEqual(self.station.texts, [("Office", "127.0.0.1", "Hello")])

    def test_server_checks_the_text_too(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("POST", "/linuxcomm/api/text", body=b'{"text": 42}', headers={intercom.H_STATION: "Office"})
        resp = conn.getresponse()
        self.assertEqual((resp.status, json.loads(resp.read())["reason"]), (400, "Send the message as text"))
        conn.close()

    def test_unreachable_station(self):
        with self.assertRaisesRegex(intercom.RemoteError, "not running"):
            intercom.send_text("127.0.0.1:1", "Hello", "Office", "")


class ProxyErrorTests(unittest.TestCase):
    """What a station sees when a proxy, an older station or another server answers."""

    def serve(self, status, server_header, location=None):
        class Handler(BaseHTTPRequestHandler):
            server_version = server_header
            sys_version = ""

            def reply(self):
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = reply

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return f"127.0.0.1:{httpd.server_address[1]}"

    def test_messages(self):
        cases = [
            (404, "LinuxComm/0.0.5", None, intercom.FOREIGN, "older LinuxComm"),
            (404, "nginx", None, intercom.FOREIGN, "No LinuxComm station at /linuxcomm"),
            (502, "nginx", None, intercom.OFFLINE, "can't reach the station"),
            (301, "nginx", "https://example.com/linuxcomm/api/status", intercom.FOREIGN, "HTTPS"),
        ]
        for status, server, location, state, text in cases:
            address = self.serve(status, server, location)
            peer = intercom.check_peer(address)
            self.assertEqual(peer.state, state, (status, server))
            self.assertIn(text, peer.detail)
            refused = talk(address, [b"\0\0" * 160])[-1]
            self.assertEqual(refused[1], intercom.REFUSED)
            self.assertIn(text, refused[2])


if __name__ == "__main__":
    unittest.main()
