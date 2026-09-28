"""Background images for the main window (Preferences → Appearance).

Images live in ~/linuxcomm/data/images. The setting holds a file name from there,
or a full path to an image elsewhere.
"""

from __future__ import annotations

import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import APP_NAME, __version__, themes
from .config import data_dir

MAX_BYTES = 30 * 1024 * 1024
TIMEOUT_S = 30
HEADER_ALPHA = 0.8  # the header bar lets the image show through a little

EXTENSION_FOR_TYPE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/tiff": ".tiff", "image/svg+xml": ".svg", "image/avif": ".avif",
}
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".svg", ".avif"}


def images_folder() -> Path:
    return data_dir() / "images"


def resolve(setting: str) -> Path | None:
    """The file a setting refers to: a name in images_folder(), or a path (None = no image)."""
    setting = setting.strip()
    if not setting:
        return None
    path = Path(setting).expanduser()
    return path if path.is_absolute() else images_folder() / path


def setting_for(path: Path) -> str:
    """What to store for an image: just its name when it is in images_folder()."""
    return path.name if path.parent == images_folder() else str(path)


def filename_for(url: str, content_type: str) -> str:
    """A safe file name for a downloaded image, from the URL (and its type if the URL has no extension)."""
    base = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    stem, ext = os.path.splitext(base)
    stem = re.sub(r"[^\w.-]+", "-", stem).strip("-.")[:80] or "background"
    ext = ext.lower() if ext.lower() in EXTENSIONS else EXTENSION_FOR_TYPE.get(content_type, ".img")
    return stem + ext


def unique_path(path: Path) -> Path:
    """`path`, or `name-2.ext`, `name-3.ext`, ... so an existing image is never overwritten."""
    if not path.exists():
        return path
    for n in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(path)


def download(url: str, folder: Path | None = None) -> Path:
    """Download an image into `folder` (default images_folder()) and return its path.

    Raises ValueError (with a message for the user) or a network error; see describe_error().
    """
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("Enter the http:// or https:// address of an image")
    folder = folder or images_folder()
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{__version__}", "Accept": "image/*"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        content_type = response.headers.get_content_type()
        if not content_type.startswith("image/"):
            raise ValueError(f"That address isn't an image (it is {content_type})")
        if int(response.headers.get("Content-Length") or 0) > MAX_BYTES:
            raise ValueError(f"The image is larger than {MAX_BYTES // (1024 * 1024)} MB")
        folder.mkdir(parents=True, exist_ok=True)
        target = unique_path(folder / filename_for(response.geturl(), content_type))
        partial = target.with_name(target.name + ".part")
        try:
            received = 0
            with open(partial, "wb") as f:
                while chunk := response.read(64 * 1024):
                    received += len(chunk)
                    if received > MAX_BYTES:
                        raise ValueError(f"The image is larger than {MAX_BYTES // (1024 * 1024)} MB")
                    f.write(chunk)
            if received == 0:
                raise ValueError("The download was empty")
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
    return target


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"Download failed (HTTP {exc.code})"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "The download timed out"
    if isinstance(exc, urllib.error.URLError):
        return f"Could not connect ({exc.reason})"
    if isinstance(exc, OSError):
        return f"Could not save the image: {exc.strerror or exc}"
    return "The download failed"


def css(path: Path | None, background: str, strength: int) -> str:
    """Put the image behind the whole main window, under a veil of the theme's background color."""
    if path is None:
        return ""
    strength = max(0, min(100, int(strength)))
    veil = themes.rgba(background, round(1 - strength / 100, 2))
    header = themes.rgba(background, HEADER_ALPHA)
    return f"""
window.main-window {{
  background-color: {background};
  background-image: linear-gradient({veil}, {veil}), url("{path.as_uri()}");
  background-size: cover; background-position: center; background-repeat: no-repeat;
}}
window.main-window headerbar, window.main-window .top-bar {{ background-color: {header}; background-image: none; }}
"""
