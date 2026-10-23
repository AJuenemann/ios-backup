"""Einstellungen."""

import shutil

from gi.repository import Adw, GLib, Gtk

from . import maintenance
from .preview import can_play_video
from .util import fmt_count, fmt_size, run_in_thread


def _button_row(title, subtitle, label, callback, destructive=False):
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    btn = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
    if destructive:
        btn.add_css_class("destructive-action")
    btn.connect("clicked", lambda *_: callback())
    row.add_suffix(btn)
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Einstellungen", search_enabled=False)
        self.win = win
        page = Adw.PreferencesPage(title="Allgemein", icon_name="preferences-system-symbolic")
        self.add(page)

        # ---- Mediathek
        g = Adw.PreferencesGroup(
            title="Mediathek",
            description="Originaldateien werden unverändert nach Aufnahmedatum abgelegt "
                        "(JJJJ/MM/TT/IMG_1234.HEIC). Alben und der Index liegen im versteckten "
                        "Ordner „.ios-photos“.")
        page.add(g)
        self.path_row = Adw.ActionRow(title="Speicherort", subtitle=GLib.markup_escape_text(str(win.library_path)))
        self.path_row.set_subtitle_selectable(True)
        change = Gtk.Button(label="Anderen Ordner …", valign=Gtk.Align.CENTER,
                            tooltip_text="Eine andere (oder neue) Mediathek verwenden – es werden keine Dateien verschoben")
        change.connect("clicked", lambda *_: (self.close(), maintenance.choose_library(win)))
        self.path_row.add_suffix(change)
        g.add(self.path_row)
        g.add(_button_row("Mediathek umziehen",
                          "Alle Fotos samt Alben und Index an einen neuen Ort kopieren oder verschieben",
                          "Umziehen …", lambda: (self.close(), maintenance.relocate_library(win))))
        self.stats_row = Adw.ActionRow(title="Inhalt", subtitle="…")
        g.add(self.stats_row)

        # ---- Wartung
        g = Adw.PreferencesGroup(title="Bestehende Sicherungen")
        page.add(g)
        g.add(_button_row("Vorhandene Fotos erfassen",
                          "Nimmt Dateien im Mediathek-Ordner, die noch nicht im Index sind (z. B. ältere "
                          "Sicherungen), auf – damit werden sie nicht erneut importiert",
                          "Erfassen", lambda: (self.close(), maintenance.run_index(win))))
        g.add(_button_row("In Datumsordner einsortieren",
                          "Verschiebt Dateien aus alten DCIM-Ordnern (100APPLE …) nach JJJJ/MM/TT",
                          "Einsortieren …", lambda: (self.close(), maintenance.run_reorganize(win))))
        g.add(_button_row("Index prüfen",
                          "Entfernt Einträge, deren Dateien außerhalb der App gelöscht wurden",
                          "Prüfen", lambda: maintenance.run_cleanup(win)))

        # ---- Geräte
        g = Adw.PreferencesGroup(
            title="iPhone anschließen",
            description="Wie „Fotos öffnen“ in Apple Fotos: Die App startet beim Anmelden unsichtbar im "
                        "Hintergrund und öffnet sich, sobald ein iPhone angeschlossen wird.")
        page.add(g)
        self.autostart = Adw.SwitchRow(title="Im Hintergrund auf iPhones warten",
                                       active=win.app.autostart_enabled())
        self.autostart.connect("notify::active", self._on_autostart)
        g.add(self.autostart)
        for udid, info in sorted(win.config.get("devices").items()):
            if udid.startswith("fake-"):
                continue
            row = Adw.SwitchRow(title=info.get("name") or udid, subtitle="Beim Anschließen öffnen",
                                active=info.get("open_on_connect", True))
            row.connect("notify::active",
                        lambda r, _p, u=udid: win.config.set_device(u, open_on_connect=r.get_active()))
            g.add(row)

        # ---- Vorschau
        g = Adw.PreferencesGroup(title="Vorschau")
        page.add(g)
        if can_play_video():
            g.add(Adw.ActionRow(title="Video-Wiedergabe", subtitle="Verfügbar"))
        else:
            row = Adw.ActionRow(
                title="Video-Wiedergabe nicht verfügbar",
                subtitle="Für die Wiedergabe von iPhone-Videos (HEVC/H.264) in der Vorschau:\n"
                         "<tt>sudo apt install gstreamer1.0-libav</tt>")
            row.set_subtitle_selectable(True)
            g.add(row)
        self.cache_row = _button_row("Vorschaubilder", "…", "Leeren", self._clear_cache)
        g.add(self.cache_row)

        self._load_stats()

    def _load_stats(self):
        lib = self.win.library
        cache = str(self.win.cache_root)

        def work():
            counts = lib.counts() if lib else None
            albums = len(lib.albums()) if lib else 0
            return counts, albums, maintenance.cache_size(cache)

        def done(res):
            counts, albums, csize = res
            if counts:
                self.stats_row.set_subtitle(
                    f"{fmt_count(counts['all'])} · {fmt_count(counts['video'], 'Video', 'Videos')} · "
                    f"{fmt_count(albums, 'Album', 'Alben')}")
            else:
                self.stats_row.set_subtitle("Mediathek nicht verfügbar")
            self.cache_row.set_subtitle(f"Zwischenspeicher: {fmt_size(csize)}")

        run_in_thread(work, done)

    def _clear_cache(self):
        shutil.rmtree(self.win.cache_root, ignore_errors=True)
        self.win.loader._mem.clear()
        self._load_stats()

    def _on_autostart(self, row, _pspec):
        self.win.app.set_autostart(row.get_active())
