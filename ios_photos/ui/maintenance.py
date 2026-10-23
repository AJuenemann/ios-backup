"""Abläufe für Altbestand erfassen, einsortieren und Mediathek umziehen (mit Fortschrittsdialog)."""

import os

from gi.repository import Gio, GLib, Gtk

from ..library import legacy
from ..library.importer import ImportCancelled
from ..library.relocate import RelocateError, check_target, relocate
from .dialogs import ProgressDialog, alert, confirm
from .util import fmt_count, fmt_int, fmt_size, idle, run_in_thread


def run_index(win, then=None):
    lib = win.library
    if lib is None:
        return
    dlg = ProgressDialog("Vorhandene Fotos erfassen", "Mediathek-Ordner wird durchsucht …")
    dlg.present(win)
    labels = {"scan": "Dateien werden gesucht …", "meta": "Aufnahmedaten werden gelesen …",
              "save": "Index wird gespeichert …"}

    def progress(phase, done, total):
        detail = f"{fmt_int(done)} von {fmt_int(total)}" if total else f"{fmt_int(done)} Dateien"
        idle(dlg.update, done / total if total else None, labels[phase], detail)

    def done(count):
        dlg.finish()
        win.library_changed()
        if dlg.cancel_event.is_set():
            win.toast("Erfassen abgebrochen")
            return
        win.toast(f"{fmt_count(count)} erfasst" if count else "Keine neuen Dateien gefunden")
        if then:
            then()

    def failed(err):
        dlg.finish()
        alert(win, "Erfassen fehlgeschlagen", str(err))

    run_in_thread(lambda: legacy.index_library(lib, progress=progress, cancel=dlg.cancel_event),
                  done, failed)


def run_reorganize(win):
    lib = win.library
    if lib is None:
        return

    def planned(plan):
        if not plan.files:
            alert(win, "Alles einsortiert", "Alle Objekte liegen bereits in Datumsordnern (JJJJ/MM/TT).")
            win.update_banner()
            return
        confirm(
            win, "In Datumsordner einsortieren?",
            f"{fmt_count(plan.assets)} ({fmt_int(plan.files)} Dateien) werden aus alten Ordnern wie "
            f"„100APPLE“ nach <b>JJJJ/MM/TT</b> verschoben. Dateinamen und Inhalte bleiben unverändert, "
            "bei Namensgleichheit wird „_1“ angehängt. Leere alte Ordner werden entfernt.",
            "Einsortieren", lambda _c: execute(plan))

    def execute(plan):
        dlg = ProgressDialog("Einsortieren", "Dateien werden verschoben …")
        dlg.present(win)

        def progress(done, total):
            idle(dlg.update, done / total if total else 1, None, f"{fmt_int(done)} von {fmt_int(total)}")

        def done(res):
            ok, errors = res
            dlg.finish()
            win.library_changed()
            win.update_banner()
            if errors:
                lines = "\n".join(f"• {n}: {e}" for n, e in errors[:10])
                alert(win, f"{fmt_int(len(errors))} Dateien konnten nicht verschoben werden", lines)
            win.toast(f"{fmt_int(ok)} Dateien einsortiert")

        run_in_thread(lambda: legacy.execute_reorganize(lib, plan, progress, dlg.cancel_event), done,
                      lambda e: (dlg.finish(), alert(win, "Einsortieren fehlgeschlagen", str(e))))

    run_in_thread(lambda: legacy.plan_reorganize(lib), planned,
                  lambda e: alert(win, "Fehler", str(e)))


def run_cleanup(win):
    lib = win.library
    if lib is None:
        return

    def done(n):
        win.library_changed()
        win.toast(f"{fmt_int(n)} fehlende Dateien aus dem Index entfernt" if n
                  else "Index ist in Ordnung")

    run_in_thread(lib.remove_missing, done)


def choose_library(win):
    dialog = Gtk.FileDialog(title="Mediathek-Ordner wählen")

    def chosen(d, res):
        try:
            folder = d.select_folder_finish(res)
        except Exception:  # noqa: BLE001 – abgebrochen
            return
        win.set_library_path(folder.get_path())

    dialog.select_folder(win, None, chosen)


def relocate_library(win):
    if win.library is None:
        alert(win, "Mediathek nicht verfügbar", "Die aktuelle Mediathek ist nicht erreichbar.")
        return
    if win.is_busy():
        alert(win, "Gerade nicht möglich", "Bitte warte, bis der laufende Import abgeschlossen ist.")
        return
    src = str(win.library.root)
    dialog = Gtk.FileDialog(title="Neuen Speicherort wählen")

    def chosen(d, res):
        try:
            folder = d.select_folder_finish(res).get_path()
        except Exception:  # noqa: BLE001
            return
        target = folder
        if os.listdir(folder):
            target = os.path.join(folder, os.path.basename(src.rstrip("/")) or "iPhone Fotos")
        try:
            check_target(src, target)
        except (RelocateError, OSError) as e:
            alert(win, "Ziel nicht geeignet", str(e))
            return

        def size_known(total):
            confirm(
                win, "Mediathek umziehen?",
                f"Die Mediathek ({fmt_size(total)}) wird nach\n<b>{GLib.markup_escape_text(target)}</b>\nkopiert und überprüft. "
                "Anschließend verwendet die App den neuen Ort.",
                "Umziehen", lambda delete: start(target, delete),
                extra_check="Alten Ordner nach erfolgreichem Umzug löschen")

        run_in_thread(lambda: _tree_size(src), size_known)

    def start(target, delete):
        dlg = ProgressDialog("Mediathek wird umgezogen", "Dateien werden kopiert …")
        dlg.present(win)
        win.close_library()

        def progress(done, total, name):
            idle(dlg.update, done / total if total else 1, None,
                 f"{fmt_size(done)} von {fmt_size(total)} – {name}")

        def done(path):
            dlg.finish()
            win.set_library_path(path)
            win.toast("Mediathek erfolgreich umgezogen")

        def failed(err):
            dlg.finish()
            win.set_library_path(src)
            if isinstance(err, ImportCancelled):
                win.toast("Umzug abgebrochen – die Mediathek bleibt am alten Ort")
            else:
                alert(win, "Umzug fehlgeschlagen",
                      f"{err}\n\nDie Mediathek bleibt unverändert am alten Ort.")

        run_in_thread(lambda: relocate(src, target, delete_source=delete, progress=progress,
                                       cancel=dlg.cancel_event), done, failed)

    dialog.set_initial_folder(Gio.File.new_for_path(os.path.dirname(src.rstrip("/")) or "/"))
    dialog.select_folder(win, None, chosen)


def _tree_size(root):
    total = 0
    for dirpath, _d, files in os.walk(root):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def cache_size(path):
    return _tree_size(path) if os.path.isdir(path) else 0
