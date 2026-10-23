"""Kleine Helfer für die Oberfläche: deutsche Formatierung, Threads, CSS."""

import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from gi.repository import Gdk, GLib, Gtk

MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
          "September", "Oktober", "November", "Dezember"]
MONTHS_SHORT = ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.",
                "Okt.", "Nov.", "Dez."]
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def fmt_int(n):
    return f"{n:,}".replace(",", ".")


def fmt_count(n, one="Objekt", many="Objekte"):
    return f"{fmt_int(n)} {one if n == 1 else many}"


def fmt_size(n):
    for unit in ("Byte", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "Byte" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1000


def fmt_duration(sec):
    sec = int(round(sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_day(d):
    if isinstance(d, datetime):
        d = d.date()
    today = date.today()
    if d == today:
        return "Heute"
    if d == today - timedelta(days=1):
        return "Gestern"
    return f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]} {d.year}"


def fmt_month(year, month):
    return f"{MONTHS[month - 1]} {year}"


def fmt_datetime(dt):
    return f"{dt.day}. {MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M}"


def run_in_thread(func, on_done=None, on_error=None):
    """Führt `func` im Hintergrund aus und ruft `on_done(result)` im GTK-Hauptthread auf."""

    def worker():
        try:
            result = func()
        except Exception as e:  # noqa: BLE001 – Fehler an die Oberfläche weitergeben
            import traceback
            traceback.print_exc()
            if on_error:
                GLib.idle_add(on_error, e)
            return
        if on_done:
            GLib.idle_add(on_done, result)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def idle(func, *args):
    """Thread-sicherer Aufruf im Hauptthread (Rückgabewert wird ignoriert)."""

    def call():
        func(*args)
        return False

    GLib.idle_add(call)


def load_css():
    provider = Gtk.CssProvider()
    provider.load_from_path(str(Path(__file__).with_name("style.css")))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def group_by_day(assets, descending=False):
    """[(date, [assets])] sortiert."""
    groups = {}
    for a in assets:
        groups.setdefault(a.sort_date.date(), []).append(a)
    days = sorted(groups, reverse=descending)
    return [(d, sorted(groups[d], key=lambda a: a.sort_date, reverse=descending)) for d in days]
