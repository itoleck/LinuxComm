"""Tests for background images: settings, file names, downloading and the CSS.

Run with:  python3 -m unittest discover -s tests -v
"""

import os
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linuxcomm import backgrounds  # noqa: E402


def tiny_png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"\x00\xff\x00\x00"  # one red pixel
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = tiny_png()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        routes = {
            "/photos/sunset.png": (200, "image/png", PNG),
            "/random": (200, "image/jpeg", PNG),           # no extension in the URL
            "/page.html": (200, "text/html", b"<html></html>"),
            "/huge.png": (200, "image/png", PNG * 1000),
        }
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/photos/sunset.png")
            self.end_headers()
            return
        status, ctype, body = routes.get(self.path, (404, "text/plain", b"missing"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.home = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.images = Path(self.home) / "linuxcomm" / "data" / "images"

    def test_images_folder_and_settings(self):
        self.assertEqual(backgrounds.images_folder(), self.images)
        self.assertIsNone(backgrounds.resolve("  "))
        self.assertEqual(backgrounds.resolve("beach.jpg"), self.images / "beach.jpg")
        self.assertEqual(backgrounds.resolve("/usr/share/backgrounds/a.png"), Path("/usr/share/backgrounds/a.png"))
        self.assertEqual(backgrounds.resolve("~/Pictures/b.png"), Path(self.home) / "Pictures" / "b.png")
        self.assertEqual(backgrounds.setting_for(self.images / "beach.jpg"), "beach.jpg")
        self.assertEqual(backgrounds.setting_for(Path("/usr/share/backgrounds/a.png")), "/usr/share/backgrounds/a.png")

    def test_file_names(self):
        f = backgrounds.filename_for
        self.assertEqual(f("https://example.com/pics/Sunset%20Beach.JPG?x=1", "image/jpeg"), "Sunset-Beach.jpg")
        self.assertEqual(f("https://picsum.photos/1600/900", "image/jpeg"), "900.jpg")
        self.assertEqual(f("https://example.com/", "image/png"), "background.png")
        self.assertEqual(f("https://example.com/../../etc/passwd", "image/png"), "passwd.png")

    def test_download(self):
        path = backgrounds.download(f"{self.base}/photos/sunset.png")
        self.assertEqual(path, self.images / "sunset.png")
        self.assertEqual(path.read_bytes(), PNG)
        again = backgrounds.download(f"{self.base}/photos/sunset.png")
        self.assertEqual(again.name, "sunset-2.png", "an existing image is never overwritten")
        self.assertEqual(backgrounds.download(f"{self.base}/random").name, "random.jpg")
        self.assertEqual(backgrounds.download(f"{self.base}/redirect").name, "sunset-3.png")
        self.assertEqual(sorted(p.name for p in self.images.iterdir()),
                         ["random.jpg", "sunset-2.png", "sunset-3.png", "sunset.png"])

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "isn't an image"):
            backgrounds.download(f"{self.base}/page.html")
        with self.assertRaisesRegex(ValueError, "http"):
            backgrounds.download("file:///etc/passwd")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            backgrounds.download(f"{self.base}/nothing.png")
        self.assertEqual(backgrounds.describe_error(cm.exception), "Download failed (HTTP 404)")
        with mock.patch.object(backgrounds, "MAX_BYTES", len(PNG) * 10):
            with self.assertRaisesRegex(ValueError, "larger than"):
                backgrounds.download(f"{self.base}/huge.png")
        leftovers = list(self.images.iterdir()) if self.images.exists() else []
        self.assertEqual(leftovers, [], "nothing half-downloaded is left behind")

    def test_css(self):
        self.assertEqual(backgrounds.css(None, "#FFFFFF", 70), "")
        text = backgrounds.css(Path("/home/pi/linuxcomm/data/images/my beach.jpg"), "#0F172A", 70)
        self.assertIn('url("file:///home/pi/linuxcomm/data/images/my%20beach.jpg")', text)
        self.assertIn("linear-gradient(rgba(15, 23, 42, 0.3), rgba(15, 23, 42, 0.3))", text)
        self.assertIn("background-size: cover", text)
        self.assertEqual(text.count("{"), text.count("}"))


if __name__ == "__main__":
    unittest.main()
