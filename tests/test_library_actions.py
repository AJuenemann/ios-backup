from types import SimpleNamespace
import threading

import pytest

from ios_photos import app  # noqa: F401
from ios_photos.ui import library_view
from ios_photos.ui import window


def test_trash_requires_confirmation_and_defaults_to_cancel(monkeypatch):
    calls = []
    lib = SimpleNamespace(trash_assets=lambda ids: calls.append(ids))
    dialog = SimpleNamespace(set_default_response=lambda response: calls.append(response))
    monkeypatch.setattr(library_view, "confirm", lambda *args, **kwargs: dialog)
    view = SimpleNamespace(
        _ids=lambda: [1, 2], _trashing=False,
        win=SimpleNamespace(library=lib, is_busy=lambda: False),
    )
    library_view.LibraryView._trash_selected(view)
    assert calls == ["cancel"]


def test_selection_controls_trash_action_and_visible_bar():
    states, visibility = [], []
    view = SimpleNamespace(
        selection_label=SimpleNamespace(set_label=lambda text: None),
        selection_bar=SimpleNamespace(set_reveal_child=visibility.append),
        actions=SimpleNamespace(lookup_action=lambda name: SimpleNamespace(set_enabled=states.append)),
        win=SimpleNamespace(is_busy=lambda: False), _trashing=False,
    )
    library_view.LibraryView._on_selection(view, [])
    library_view.LibraryView._on_selection(view, [object()])
    view._trashing = True
    library_view.LibraryView._on_selection(view, [object()])
    assert states == [False, True, False]
    assert visibility == [False, True, True]


def test_trash_dialog_receives_file_progress_and_cancel_event(monkeypatch):
    confirmations, updates, messages = [], [], []
    expected_cancel = threading.Event()
    progress = SimpleNamespace(
        cancel_event=expected_cancel,
        cancel_button=SimpleNamespace(set_tooltip_text=lambda text: None),
        present=lambda win: None, finish=lambda: None,
        update=lambda **kwargs: updates.append(kwargs),
    )
    monkeypatch.setattr(library_view, "ProgressDialog", lambda *args, **kwargs: progress)
    monkeypatch.setattr(library_view, "confirm",
                        lambda *args, **kwargs: (
                            confirmations.append(args[4]) or
                            SimpleNamespace(set_default_response=lambda response: None)))
    monkeypatch.setattr(library_view, "idle", lambda func, *args: func(*args))
    monkeypatch.setattr(library_view, "run_in_thread", lambda work, done, failed: done(work()))

    def trash(ids, progress, cancel):
        assert ids == [1] and cancel is expected_cancel
        progress(0, 2, "photo.jpg")
        progress(1, 2, "photo.jpg")
        progress(2, 2, "video.mov")
        return 1, []

    view = SimpleNamespace(
        _ids=lambda: [1], _trashing=False,
        _on_selection=lambda selected: None,
        grid=SimpleNamespace(selected={1}, clear_selection=lambda: None),
        win=SimpleNamespace(
            library=SimpleNamespace(trash_assets=trash), is_busy=lambda: False,
            library_changed=lambda: None, import_views={}, toast=messages.append,
        ),
    )
    library_view.LibraryView._trash_selected(view)
    confirmations[0](False)
    assert [update["fraction"] for update in updates] == [0, 0.5, 1]
    assert updates[1]["text"] == "1 von 2 Dateien bearbeitet"
    assert "video.mov" in updates[-1]["detail"]
    assert messages == ["1 Objekt in den Papierkorb verschoben"]
    assert view._trashing is False


@pytest.mark.parametrize("mode", ["days", "all", "years", "months"])
def test_select_all_without_existing_selection(mode):
    from ios_photos.ui.photogrid import PhotoGrid, Section

    grid = PhotoGrid(SimpleNamespace())
    assets = [SimpleNamespace(key=i) for i in range(3)]
    grid.set_sections([Section(items=assets)])
    modes = []
    view = SimpleNamespace(assets=assets, mode=mode, grid=grid,
                           _set_mode=modes.append)
    assert not grid.selected
    library_view.LibraryView.select_all(view)
    assert grid.selected == {0, 1, 2}
    assert modes == (["all"] if mode in ("years", "months") else [])


@pytest.mark.parametrize("ctrl", [False, True])
def test_click_toggles_individual_assets_without_clearing_selection(ctrl):
    from ios_photos.ui.photogrid import PhotoGrid, Section

    grid = PhotoGrid(SimpleNamespace())
    assets = [SimpleNamespace(key=i) for i in range(4)]
    grid.set_sections([Section(items=assets)])
    grid.click(assets[0], ctrl=ctrl)
    grid.click(assets[2], ctrl=ctrl)
    assert grid.selected == {0, 2}
    grid.click(assets[0], ctrl=ctrl)
    assert grid.selected == {2}
    grid.click(assets[2], ctrl=ctrl)
    assert grid.selected == set()


def test_click_preserves_shift_ranges_and_explicit_single_selection():
    from ios_photos.ui.photogrid import PhotoGrid, Section

    grid = PhotoGrid(SimpleNamespace())
    assets = [SimpleNamespace(key=i) for i in range(5)]
    grid.set_sections([Section(items=assets)])
    grid.click(assets[1])
    grid.click(assets[3], shift=True)
    assert grid.selected == {1, 2, 3}
    grid.click(assets[0], ctrl=True, shift=True)
    assert grid.selected == {0, 1, 2, 3}
    grid.select_only(assets[4])
    assert grid.selected == {4}
    grid.clear_selection()
    assert grid.selected == set()


def test_window_select_all_accelerator_does_not_need_grid_focus(monkeypatch):
    from gi.repository import Gio

    selected, accelerators = [], {}
    page = SimpleNamespace(select_all=lambda: selected.append(True))
    monkeypatch.setattr(window, "LibraryView", SimpleNamespace)
    actions = Gio.SimpleActionGroup()
    win = SimpleNamespace(
        split=SimpleNamespace(get_content=lambda: page),
        app=SimpleNamespace(set_accels_for_action=accelerators.__setitem__),
        add_action=actions.add_action,
    )
    win._select_all = lambda: window.MainWindow._select_all(win)
    window.MainWindow._install_actions(win)
    assert accelerators["win.select-all"] == ["<Control>a"]
    actions.lookup_action("select-all").activate(None)
    assert selected == [True]


@pytest.mark.parametrize("visible", ["grid", "state", "loading"])
def test_window_select_all_only_selects_ready_import_grid(monkeypatch, visible):
    selected = []
    page = SimpleNamespace(
        grid=SimpleNamespace(select_all=lambda: selected.append(True)),
        stack=SimpleNamespace(get_visible_child_name=lambda: visible),
    )
    monkeypatch.setattr(window, "LibraryView", type("OtherPage", (), {}))
    monkeypatch.setattr(window, "ImportView", SimpleNamespace)
    win = SimpleNamespace(split=SimpleNamespace(get_content=lambda: page))
    window.MainWindow._select_all(win)
    assert selected == ([True] if visible == "grid" else [])
