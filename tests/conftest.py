import os
from datetime import datetime

import pytest
from PIL import Image


def make_jpeg(path, date=None, size=(64, 48), color=(200, 30, 30)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    if date:
        exif.get_ifd(0x8769)[36867] = date.strftime("%Y:%m:%d %H:%M:%S")
    im.save(path, "JPEG", exif=exif)
    return path


def make_file(path, data=b"x" * 100):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def make_video(path, frames=3, size=(64, 48)):
    import av
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with av.open(path, "w") as c:
        s = c.add_stream("mpeg4", rate=10)
        s.width, s.height, s.pix_fmt = size[0], size[1], "yuv420p"
        for i in range(frames):
            im = Image.new("RGB", size, (i * 40, 100, 200))
            frame = av.VideoFrame.from_image(im)
            for p in s.encode(frame):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    return path


@pytest.fixture
def phone(tmp_path):
    """Simuliertes iPhone mit DCIM-Ordner."""
    root = tmp_path / "phone"
    d = root / "DCIM" / "100APPLE"
    make_jpeg(str(d / "IMG_0001.JPG"), datetime(2024, 5, 1, 10, 0, 0))
    make_video(str(d / "IMG_0001.MOV"))
    make_jpeg(str(d / "IMG_0002.JPG"), datetime(2024, 5, 2, 11, 0, 0), color=(0, 200, 0))
    make_file(str(d / "IMG_0002.AAE"), b"<plist/>")
    make_video(str(d / "IMG_0003.MOV"), frames=5)
    make_jpeg(str(root / "DCIM" / "101APPLE" / "IMG_0100.JPG"), datetime(2023, 12, 24, 18, 0, 0))
    return root


@pytest.fixture
def library(tmp_path):
    from ios_photos.library.db import Library
    lib = Library(tmp_path / "lib")
    yield lib
    lib.close()
