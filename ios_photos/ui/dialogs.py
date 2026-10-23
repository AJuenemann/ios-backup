"""Wiederverwendbare Dialoge."""

import threading

from gi.repository import Adw, GLib, Gtk


class ProgressDialog(Adw.Dialog):
    """Fortschrittsdialog für längere Hintergrundaufgaben mit Abbrechen-Knopf."""

    def __init__(self, title, text="", cancellable=True):
        super().__init__(title=title, content_width=440, can_close=False)
        self.cancel_event = threading.Event()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=24,
                      margin_bottom=24, margin_start=24, margin_end=24)
        self.label = Gtk.Label(label=text, wrap=True, xalign=0)
        self.bar = Gtk.ProgressBar(show_text=False)
        self.detail = Gtk.Label(xalign=0, ellipsize=3)
        self.detail.add_css_class("dim-label")
        self.detail.add_css_class("caption")
        box.append(self.label)
        box.append(self.bar)
        box.append(self.detail)
        if cancellable:
            btn = Gtk.Button(label="Abbrechen", halign=Gtk.Align.END)
            btn.connect("clicked", self._on_cancel)
            box.append(btn)
            self.cancel_button = btn
        tv = Adw.ToolbarView()
        hb = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        tv.add_top_bar(hb)
        tv.set_content(box)
        self.set_child(tv)
        self._pulse_id = GLib.timeout_add(120, self._pulse)
        self._determinate = False

    def _pulse(self):
        if not self._determinate:
            self.bar.pulse()
            return True
        self._pulse_id = 0
        return False

    def _on_cancel(self, btn):
        self.cancel_event.set()
        btn.set_sensitive(False)
        self.label.set_label("Wird abgebrochen …")

    def update(self, fraction=None, text=None, detail=None):
        if fraction is not None:
            self._determinate = True
            self.bar.set_fraction(max(0.0, min(1.0, fraction)))
        if text is not None:
            self.label.set_label(text)
        if detail is not None:
            self.detail.set_label(detail)

    def finish(self):
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0
        self.set_can_close(True)
        self.close()


def alert(parent, heading, body="", ok="OK"):
    d = Adw.AlertDialog(heading=heading, body=body)
    d.add_response("ok", ok)
    d.present(parent)
    return d


def confirm(parent, heading, body, ok_label, callback, destructive=False, extra_check=None):
    """Bestätigungsdialog (Text mit Pango-Markup). `callback(checked)` nur bei Bestätigung."""
    d = Adw.AlertDialog(heading=heading, body=body, body_use_markup=True)
    d.add_response("cancel", "Abbrechen")
    d.add_response("ok", ok_label)
    d.set_response_appearance(
        "ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive else Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.set_close_response("cancel")
    check = None
    if extra_check:
        check = Gtk.CheckButton(label=extra_check)
        d.set_extra_child(check)

    def on_response(_d, resp):
        if resp == "ok":
            callback(check.get_active() if check else False)

    d.connect("response", on_response)
    d.present(parent)
    return d


def ask_text(parent, heading, body, ok_label, callback, initial=""):
    """Fragt einen Text ab (z. B. Albumname). `callback(text)` bei Bestätigung."""
    d = Adw.AlertDialog(heading=heading, body=body)
    entry = Gtk.Entry(text=initial, activates_default=True)
    d.set_extra_child(entry)
    d.add_response("cancel", "Abbrechen")
    d.add_response("ok", ok_label)
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.set_close_response("cancel")
    d.set_response_enabled("ok", bool(initial.strip()))
    entry.connect("changed", lambda e: d.set_response_enabled("ok", bool(e.get_text().strip())))

    def on_response(_d, resp):
        if resp == "ok" and entry.get_text().strip():
            callback(entry.get_text().strip())

    d.connect("response", on_response)
    d.present(parent)
    GLib.idle_add(entry.grab_focus)
    return d
