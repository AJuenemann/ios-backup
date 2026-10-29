import threading
import time
from types import SimpleNamespace

import pytest

from ios_photos import app  # noqa: F401
from ios_photos.device.monitor import READY, Device
from ios_photos.library.importer import run_import
from ios_photos.media.scanner import scan_dcim
from ios_photos.ui import import_view, window
from ios_photos.ui.import_view import ImportView
from ios_photos.ui.window import MainWindow

from conftest import make_jpeg


def _view(device, library, **overrides):
    view = SimpleNamespace(
        device=device, assets=None, loading=False, importing=False,
        _refreshing=False, _loaded_at=0.0, cancel_load=__import__("threading").Event(),
        win=SimpleNamespace(library=library, metacache=None),
        _loaded=lambda assets, quiet=False: None,
        _update_buttons=lambda: None,
    )
    view._scan = lambda cancel, ps=None, pm=None: ImportView._scan(view, cancel, ps, pm)
    for key, value in overrides.items():
        setattr(view, key, value)
    return view


def test_refresh_rescans_and_drops_vanished_assets(tmp_path, library, monkeypatch):
    dcim = tmp_path / "phone" / "DCIM" / "100APPLE"
    make_jpeg(str(dcim / "IMG_0001.JPG"))
    make_jpeg(str(dcim / "IMG_0002.JPG"))
    device = Device("fake-0", state=READY, mountpoint=str(tmp_path / "phone"), fake=True)
    loaded = []
    view = _view(device, library)
    view.assets = scan_dcim(device.mountpoint)
    assert len(view.assets) == 2
    view._loaded = lambda assets, quiet=False: loaded.append((assets, quiet))
    (dcim / "IMG_0002.JPG").unlink()

    monkeypatch.setattr(import_view, "run_in_thread",
                        lambda work, done, failed: done(work()))
    ImportView.refresh(view, force=True)
    assert len(loaded) == 1
    assets, quiet = loaded[0]
    assert quiet is True
    assert [a.primary.name for a in assets] == ["IMG_0001.JPG"]


@pytest.mark.parametrize("state", ["importing", "loading", "refreshing", "no-assets", "interval"])
def test_refresh_skips_when_busy_or_fresh(tmp_path, library, monkeypatch, state):
    dcim = tmp_path / "phone" / "DCIM" / "100APPLE"
    make_jpeg(str(dcim / "IMG_0001.JPG"))
    device = Device("fake-0", state=READY, mountpoint=str(tmp_path / "phone"), fake=True)
    overrides = {"assets": scan_dcim(device.mountpoint)}
    if state == "importing":
        overrides["importing"] = True
    elif state == "loading":
        overrides["loading"] = True
    elif state == "refreshing":
        overrides["_refreshing"] = True
    elif state == "no-assets":
        overrides["assets"] = None
    elif state == "interval":
        overrides["_loaded_at"] = time.monotonic()
    view = _view(device, library, **overrides)
    calls = []
    monkeypatch.setattr(import_view, "run_in_thread", lambda *a: calls.append(a))
    ImportView.refresh(view)
    assert calls == []


def test_show_view_refreshes_existing_device_view(monkeypatch):
    refreshes = []
    dev = Device("u1", state=READY, mountpoint="/x", fake=True)
    view = SimpleNamespace(assets=[object()], refresh=lambda: refreshes.append(True))
    split = SimpleNamespace(set_content=lambda v: None, set_show_content=lambda v: None)
    win = SimpleNamespace(devices={"u1": dev}, import_views={"u1": view},
                          views={"library": SimpleNamespace(assets=[])},
                          split=split, _current_id=None)
    monkeypatch.setattr(window, "ImportView", SimpleNamespace)
    MainWindow.show_view(win, "device:u1")
    assert refreshes == [True]
    MainWindow.show_view(win, "library")
    assert refreshes == [True]


def test_window_focus_refreshes_device_page_without_preview(monkeypatch):
    refreshes = []

    class FakeImportView:
        pass

    page = FakeImportView()
    page._device_preview = None
    page.refresh = lambda: refreshes.append(True)
    monkeypatch.setattr(window, "ImportView", FakeImportView)
    win = SimpleNamespace(is_active=lambda: True,
                          split=SimpleNamespace(get_content=lambda: page))
    MainWindow._on_active(win)
    assert refreshes == [True]
    page._device_preview = object()
    MainWindow._on_active(win)
    assert refreshes == [True]
    win_inactive = SimpleNamespace(is_active=lambda: False,
                                   split=SimpleNamespace(get_content=lambda: page))
    page._device_preview = None
    MainWindow._on_active(win_inactive)
    assert refreshes == [True]


def test_import_of_vanished_files_is_skipped_without_fatal(phone, library):
    assets = scan_dcim(str(phone))
    for a in assets:
        for f in a.files:
            if "0002" in f.name or "0003" in f.name:
                __import__("os").unlink(f.path)
    result = run_import(library, assets, udid="u1")
    assert not result.fatal and not result.cancelled
    assert result.imported == 2
    assert len(result.failed) == 2
    assert all(msg == "nicht mehr auf dem Gerät" for _, msg in result.failed)


def test_many_vanished_files_do_not_trigger_connection_fatal(phone, library):
    assets = scan_dcim(str(phone))
    for a in assets:
        for f in a.files:
            __import__("os").unlink(f.path)
    result = run_import(library, assets, udid="u1")
    assert not result.fatal
    assert result.imported == 0 and len(result.failed) == 4


def test_quiet_refresh_result_is_discarded_during_import():
    view = SimpleNamespace(device=Device("u1", state=READY), importing=True,
                           assets=["alt"], loading=True)
    ImportView._loaded(view, ["neu"], quiet=True)
    assert view.assets == ["alt"] and view.loading is True


def test_start_import_cancels_running_refresh(phone, library, monkeypatch):
    old_event = threading.Event()
    view = SimpleNamespace(
        importing=False, win=SimpleNamespace(library=library),
        device=Device("u1", state=READY, mountpoint=str(phone), fake=True),
        cancel_load=old_event, cancel_import=None,
        stop_btn=SimpleNamespace(set_sensitive=lambda v: None),
        prog_bar=SimpleNamespace(set_fraction=lambda v: None),
        prog_label=SimpleNamespace(set_label=lambda v: None),
        progress_revealer=SimpleNamespace(set_reveal_child=lambda v: None),
        _update_buttons=lambda: None, current_album_id=lambda: None,
        _import_done=lambda result: None, _import_failed=lambda err: None,
    )
    monkeypatch.setattr(import_view, "run_in_thread", lambda *a, **k: None)
    ImportView.start_import(view, scan_dcim(str(phone)))
    assert view.importing is True
    assert old_event.is_set()
    assert view.cancel_load is not old_event and not view.cancel_load.is_set()


def test_scan_dcim_honors_cancel(phone):
    ev = threading.Event()
    ev.set()
    assert scan_dcim(str(phone), cancel=ev) is None


def test_import_done_refreshes_view_after_failures(monkeypatch):
    from ios_photos.library.importer import ImportResult

    refreshes = []
    result = ImportResult(failed=[("IMG_0002.JPG", "nicht mehr auf dem Gerät")])
    view = SimpleNamespace(
        importing=True, progress_revealer=SimpleNamespace(set_reveal_child=lambda v: None),
        win=SimpleNamespace(library=None, library_changed=lambda: None,
                            on_import_finished=lambda v: None),
        grid=SimpleNamespace(clear_selection=lambda: None),
        rebuild=lambda: None, _update_buttons=lambda: None,
        assets=[object()], device=Device("u1", state=READY),
        refresh=lambda force=False: refreshes.append(force),
    )
    monkeypatch.setattr(import_view, "alert", lambda *a, **k: None)
    ImportView._import_done(view, result)
    assert refreshes == [True]
