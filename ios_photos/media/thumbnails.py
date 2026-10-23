"""Erzeugt Vorschaubilder (HEIC, JPEG, PNG, RAW, Video) mit Festplatten-Cache."""

import hashlib
import io
import os
import subprocess

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pillow_heif = None

try:
    import av
except ImportError:  # pragma: no cover
    av = None

from .scanner import RAW_EXT, VIDEO_EXT

THUMB_SIZE = 360
PREVIEW_SIZE = 2048


def cache_path(cache_root, src_path, size, mtime, fsize):
    h = hashlib.sha1(f"{src_path}|{size}|{int(mtime)}|{fsize}".encode()).hexdigest()
    return os.path.join(cache_root, str(size), h[:2], h + ".jpg")


def _open_raw(path):
    for tag in ("-PreviewImage", "-JpgFromRaw", "-ThumbnailImage"):
        try:
            data = subprocess.run(["exiftool", "-b", tag, path], capture_output=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            data = b""
        if data:
            im = Image.open(io.BytesIO(data))
            orient = subprocess.run(
                ["exiftool", "-n", "-s3", "-Orientation", path], capture_output=True, timeout=30
            ).stdout.strip()
            rot = {b"3": 180, b"6": 270, b"8": 90}.get(orient)
            im.load()
            return im.rotate(rot, expand=True) if rot else im
    raise OSError("Keine Vorschau im RAW gefunden")


def _open_video(path, size):
    if av is None:
        raise OSError("PyAV nicht installiert")
    with av.open(path) as c:
        stream = c.streams.video[0]
        stream.thread_type = "AUTO"
        for frame in c.decode(stream):
            im = frame.to_image()
            rot = getattr(frame, "rotation", 0) or 0
            if rot:
                im = im.rotate(rot, expand=True)
            return im
    raise OSError("Kein Videobild")


def open_image(path, size):
    """Öffnet ein Bild/Video als richtig ausgerichtetes PIL-Bild (ungefähr in `size`)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return _open_video(path, size)
    if ext in RAW_EXT:
        im = _open_raw(path)
    else:
        im = Image.open(path)
        if im.format == "JPEG":
            im.draft("RGB", (size, size))
        im = ImageOps.exif_transpose(im)
    return im


def make_thumbnail(path, dest, size=THUMB_SIZE):
    im = open_image(path, size)
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        im = im.convert("RGBA")
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    im.thumbnail((size, size), Image.Resampling.LANCZOS)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    im.convert("RGB").save(tmp, "JPEG", quality=88)
    os.replace(tmp, dest)
    return dest


def get_thumbnail(cache_root, path, size=THUMB_SIZE, mtime=None, fsize=None):
    """Gibt den Pfad eines zwischengespeicherten JPEG-Vorschaubilds zurück (erzeugt es bei Bedarf)."""
    if mtime is None or fsize is None:
        st = os.stat(path)
        mtime, fsize = st.st_mtime, st.st_size
    dest = cache_path(cache_root, path, size, mtime, fsize)
    if os.path.exists(dest):
        return dest
    return make_thumbnail(path, dest, size)
