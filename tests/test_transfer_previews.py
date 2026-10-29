import builtins
import io
import threading
from types import SimpleNamespace

import pytest

from ios_photos import app  # noqa: F401
from ios_photos.library import importer
from ios_photos.ui.thumbloader import ThumbLoader


def test_recycled_cell_ignores_outdated_thumbnail_callback():
    from ios_photos.media.scanner import Asset, SourceFile
    from ios_photos.ui.photogrid import PhotoGrid, ThumbCell

    callbacks = []
    loader = SimpleNamespace(
        request=lambda f, cb: callbacks.append(cb),
        cancel=lambda f, cb: None,
    )
    grid = PhotoGrid(loader)
    cell = ThumbCell(grid)
    assets = [
        Asset(str(i), [SourceFile(f"/photo{i}.jpg", f"photo{i}.jpg", f"photo{i}.jpg",
                                 10, 0, "photo")])
        for i in range(2)
    ]
    cell.bind(assets[0], 100)
    cell.bind(assets[1], 100)
    current_request = cell._texture_cb
    callbacks[0](None)
    assert cell._texture_cb is current_request
    callbacks[1](None)
    assert cell._texture_cb is None
    cell.unbind()


def test_inflight_thumbnail_is_not_queued_twice_after_cancel(tmp_path):
    loader = ThumbLoader(tmp_path, workers=0)
    file = SimpleNamespace(path="photo")
    first, second = [], []
    loader.request(file, first.append)
    assert loader._stack.pop() is file  # Worker has started.
    loader.cancel(file, first.append)
    loader.request(file, second.append)
    assert loader._stack == []
    texture = object()
    loader._deliver(file, texture)
    assert first == [] and second == [texture]
    assert loader.request(file, first.append) is texture
    loader.stop()


def test_thumbnail_failure_can_retry_after_cooldown(tmp_path, monkeypatch):
    from ios_photos.ui import thumbloader

    now = [10.]
    monkeypatch.setattr(thumbloader.time, "monotonic", lambda: now[0])
    loader = ThumbLoader(tmp_path, workers=0)
    file = SimpleNamespace(path="photo")
    results = []
    loader.request(file, results.append)
    loader._stack.pop()
    loader._deliver(file, None)
    assert results == [None]
    now[0] += 6
    loader.request(file, results.append)
    assert loader._stack == [file]
    loader._deliver(file, object())
    assert file.path not in loader._failed
    loader.stop()


@pytest.mark.parametrize("permanent", [False, True])
def test_premature_eof_is_retried_without_accepting_truncated_file(tmp_path, monkeypatch, permanent):
    src, dst = tmp_path / "source", tmp_path / "destination"
    src.write_bytes(b"abcdef")
    original_open = builtins.open
    reads, progress, waits = [], [], []

    def open_file(path, mode):
        if path == str(src):
            reads.append(path)
            if permanent or len(reads) == 1:
                return io.BytesIO(b"abc")
        return original_open(path, mode)

    monkeypatch.setattr(importer, "open", open_file, raising=False)
    cancel = threading.Event()
    monkeypatch.setattr(cancel, "wait", lambda seconds: False)
    if permanent:
        with pytest.raises(importer.IncompleteSourceError, match="3 Bytes kopiert"):
            importer.copy_file(str(src), str(dst), cancel, progress.append,
                               source_waiting=waits.append)
        assert len(reads) == 3 and not dst.exists()
        assert sum(progress) == 0
    else:
        importer.copy_file(str(src), str(dst), cancel, progress.append,
                           source_waiting=waits.append)
        assert len(reads) == 2 and dst.read_bytes() == b"abcdef"
        assert sum(progress) == 6
    assert not (tmp_path / "destination.part").exists()
