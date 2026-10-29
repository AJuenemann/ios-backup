from types import SimpleNamespace
from pathlib import Path
import errno
import threading

import pytest

from ios_photos.app import Application
from ios_photos.config import Config, autostart_file
from ios_photos.device.monitor import CONNECTING, READY, Device, DeviceMonitor
from ios_photos.device import monitor as device_monitor
from ios_photos.ui.import_view import ImportView


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    application = SimpleNamespace(
        config=Config(), devices={}, window=None, _held=False, activations=0, holds=0,
    )

    def activate():
        application.activations += 1

    def hold():
        application.holds += 1

    def release():
        application.holds -= 1

    application.activate = activate
    application.hold = hold
    application.release = release
    application.autostart_enabled = lambda: Application.autostart_enabled(application)
    return application


def test_autostart_default_and_persisted_opt_out(app):
    assert app.config.get("background_monitor") is True
    Application.set_autostart(app, app.config.get("background_monitor"))
    assert app.autostart_enabled()
    assert "--monitor" in autostart_file().read_text()
    assert app._held and app.holds == 1
    Application.set_autostart(app, True)
    assert app.holds == 1
    Application.set_autostart(app, False)
    assert not app.autostart_enabled()
    assert not app._held and app.holds == 0
    assert Config().get("background_monitor") is False


def test_device_events_are_independent_snapshots():
    events = []
    monitor = DeviceMonitor(events.append)
    monitor.devices["phone"] = Device("phone", state=CONNECTING)
    monitor._emit()
    monitor.devices["phone"].state = READY
    monitor._emit()
    assert events[0]["phone"].state == CONNECTING
    assert events[1]["phone"].state == READY


def test_connection_opens_once_and_reconnect_opens_again(app):
    Application.set_autostart(app, True)
    monitor = DeviceMonitor(lambda devices: Application._devices_changed(app, devices))
    monitor.devices["phone"] = Device("phone", state=CONNECTING)
    monitor._emit()
    assert app.activations == 1
    monitor.devices["phone"].state = READY
    monitor._emit()
    monitor._emit()
    assert app.activations == 1
    monitor.devices.clear()
    monitor._emit()
    monitor.devices["phone"] = Device("phone", state=READY)
    monitor._emit()
    assert app.activations == 2


def test_device_and_global_opt_out_and_fake_device(app):
    Application.set_autostart(app, True)
    app.config.set_device("phone", open_on_connect=False)
    Application._devices_changed(app, {"phone": Device("phone", state=READY)})
    assert app.activations == 0
    Application._devices_changed(app, {"fake": Device("fake", state=READY, fake=True)})
    assert app.activations == 0
    Application.set_autostart(app, False)
    Application._devices_changed(app, {"other": Device("other", state=READY)})
    assert app.activations == 0


def test_closing_window_keeps_background_hold(app):
    Application.set_autostart(app, True)
    app.window = window = object()
    Application.window_closed(app, window)
    assert app.window is None
    assert app._held and app.holds == 1


def test_existing_mount_is_validated_again(monkeypatch):
    calls = []
    monkeypatch.setattr(device_monitor, "lock_state", lambda udid: (READY, ""))
    monkeypatch.setattr(device_monitor, "pair_state", lambda udid: (READY, ""))
    monkeypatch.setattr(device_monitor, "device_info", lambda udid: {})
    monkeypatch.setattr(device_monitor, "is_mounted", lambda path: True)
    monkeypatch.setattr(device_monitor, "mount",
                        lambda udid: (calls.append(udid) or "/existing", ""))
    monkeypatch.setattr(device_monitor.os.path, "isdir", lambda path: True)
    dev = Device("phone", mountpoint="/existing")
    DeviceMonitor(lambda devices: None)._advance(dev)
    assert calls == ["phone"]
    assert dev.state == READY


def test_mount_replaces_unreadable_existing_connection(monkeypatch):
    calls = []
    monkeypatch.setattr(device_monitor, "runtime_dir", lambda: Path("/runtime"))
    monkeypatch.setattr(device_monitor, "is_mounted", lambda path: True)

    def unreadable(path):
        raise OSError("Input/output error")

    monkeypatch.setattr(device_monitor.os, "listdir", unreadable)
    monkeypatch.setattr(device_monitor, "unmount", lambda path: calls.append(("unmount", path)))
    monkeypatch.setattr(device_monitor.os, "makedirs", lambda *args, **kwargs: None)
    monkeypatch.setattr(device_monitor, "_run", lambda args, **kwargs: (calls.append(("mount", args)) or (0, "")))
    path, error = device_monitor.mount("phone")
    assert path == "/runtime/phone" and not error
    assert calls[0] == ("unmount", "/runtime/phone")
    assert calls[1][0] == "mount"


def test_broken_fuse_mount_is_still_detected(monkeypatch):
    def broken(path):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(device_monitor.os, "stat", broken)
    monkeypatch.setattr(device_monitor.os.path, "ismount", lambda path: False)
    assert device_monitor.is_mounted("/broken")


def test_mount_password_error_reports_locked(monkeypatch):
    monkeypatch.setattr(device_monitor, "lock_state", lambda udid: (READY, ""))
    monkeypatch.setattr(device_monitor, "pair_state", lambda udid: (READY, ""))
    monkeypatch.setattr(device_monitor, "device_info", lambda udid: {})
    monkeypatch.setattr(device_monitor, "mount", lambda udid: (None, "Device is PasswordProtected"))
    dev = Device("phone")
    assert DeviceMonitor(lambda devices: None)._advance(dev)
    assert dev.state == device_monitor.LOCKED


def test_filesystem_error_is_emitted_instead_of_staying_connecting(monkeypatch):
    events = []
    monitor = DeviceMonitor(events.append)
    monkeypatch.setattr(device_monitor, "list_udids", lambda: ["phone"])

    def failed(dev):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(monitor, "_advance", failed)
    monitor._poll()
    assert events[-1]["phone"].state == device_monitor.ERROR
    assert "Input/output error" in events[-1]["phone"].message


@pytest.mark.parametrize("out,code,state", [
    ("true", 0, READY),
    ("false", 0, READY),
    ("", 0, READY),
    ("timeout", 1, READY),
])
def test_lock_state(monkeypatch, out, code, state):
    monkeypatch.setattr(device_monitor, "_run", lambda *args, **kwargs: (code, out))
    assert device_monitor.lock_state("phone")[0] == state


def test_readable_mount_does_not_override_screen_lock(monkeypatch):
    events = []
    monitor = DeviceMonitor(events.append)
    monitor.devices["phone"] = Device("phone", state=READY, mountpoint="/readable")
    monkeypatch.setattr(device_monitor, "list_udids", lambda: ["phone"])
    monkeypatch.setattr(device_monitor, "lock_state", lambda udid: (device_monitor.LOCKED, ""))
    monkeypatch.setattr(device_monitor.os.path, "isdir", lambda path: True)
    monitor._poll()
    assert events[-1]["phone"].state == device_monitor.LOCKED


@pytest.mark.parametrize("state, cancelled", [
    (device_monitor.LOCKED, False),
    (device_monitor.ERROR, True),
    (device_monitor.UNTRUSTED, True),
])
def test_unavailable_device_hides_content_but_only_lock_keeps_import_running(state, cancelled):
    sections, states, closed = [], [], []
    view = SimpleNamespace(
        set_title=lambda title: None,
        window_title=SimpleNamespace(set_title=lambda title: None),
        cancel_load=threading.Event(), cancel_import=threading.Event(),
        loading=True, assets=[object()],
        grid=SimpleNamespace(set_sections=sections.append),
        _device_preview=SimpleNamespace(close=lambda: closed.append(True)),
        _show_state=lambda *args: states.append(args),
        _update_buttons=lambda: None,
    )
    ImportView.set_device(view, Device("phone", state=state))
    assert view.assets is None and not view.loading
    assert view.cancel_load.is_set()
    assert view.cancel_import.is_set() == cancelled
    assert sections == [[]]
    assert closed == [True]
    assert states[0][0] == state


def test_unlocking_during_import_does_not_start_a_parallel_scan():
    loaded = []
    view = SimpleNamespace(
        set_title=lambda title: None,
        window_title=SimpleNamespace(set_title=lambda title: None),
        assets=None, loading=False, importing=True,
        load=lambda: loaded.append(True),
        _update_buttons=lambda: None,
    )
    ImportView.set_device(view, Device("phone", state=READY))
    assert not loaded


def test_late_scan_result_cannot_reveal_locked_device():
    view = SimpleNamespace(
        device=Device("phone", state=device_monitor.LOCKED),
        assets=None, loading=False,
    )
    ImportView._loaded(view, [object()])
    assert view.assets is None
