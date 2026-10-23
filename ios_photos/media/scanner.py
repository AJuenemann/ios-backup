"""Durchsucht DCIM-artige Ordner und fasst zusammengehörige Dateien zu Objekten zusammen.

Ein Objekt (Asset) entspricht einem Eintrag in Apple Fotos, z. B.:
  IMG_1234.HEIC + IMG_1234.MOV (Live Photo) + IMG_1234.AAE (Bearbeitung)
  IMG_E1234.HEIC (bearbeitete Version) + IMG_O1234.AAE
  DSC00071.JPG + DSC00071.ARW (RAW + JPEG)
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

PHOTO_EXT = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp"}
RAW_EXT = {".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".orf", ".rw2"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".3gp", ".avi"}
SIDECAR_EXT = {".aae", ".xmp"}
MEDIA_EXT = PHOTO_EXT | RAW_EXT | VIDEO_EXT

_EDIT_RE = re.compile(r"^([A-Za-z]+)_([EO])(\d{3,})$")


def file_type(name):
    ext = os.path.splitext(name)[1].lower()
    if ext in PHOTO_EXT:
        return "photo"
    if ext in RAW_EXT:
        return "raw"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT:
        return "sidecar"
    return None


def base_and_variant(stem):
    """IMG_E1234 -> ('IMG_1234', 'E'); IMG_1234 -> ('IMG_1234', '')."""
    m = _EDIT_RE.match(stem)
    if m:
        return f"{m.group(1)}_{m.group(3)}", m.group(2).upper()
    return stem, ""


def name_key(name):
    return name.upper()


@dataclass(eq=False)
class SourceFile:
    path: str          # absoluter Pfad
    rel: str           # relativ zur Quelle (z. B. "100APPLE/IMG_1234.HEIC")
    name: str
    size: int
    mtime: float
    ftype: str         # photo | raw | video | sidecar
    variant: str = ""  # "" | "E" (bearbeitet) | "O" (Original-Anpassungen)

    @property
    def fingerprint(self):
        return (name_key(self.name), self.size)


@dataclass
class Asset:
    key: str
    files: list
    kind: str = "photo"           # photo | video | live
    raw: bool = False
    screenshot: bool = False
    edited: bool = False
    date: datetime | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    status: str = "new"           # new | imported
    extra: dict = field(default_factory=dict)

    @property
    def primary(self):
        """Originaldatei, die das Objekt repräsentiert (Datum, Fingerprint)."""
        def rank(f):
            order = {"photo": 0, "raw": 1, "video": 2, "sidecar": 3}[f.ftype]
            return (f.variant != "", order, f.name)
        return min(self.files, key=rank)

    @property
    def display(self):
        """Datei für Vorschaubilder – wie Apple die bearbeitete Version, falls vorhanden."""
        edited = [f for f in self.files if f.variant == "E" and f.ftype in ("photo", "video")]
        if edited and self.kind != "video":
            stills = [f for f in edited if f.ftype == "photo"]
            if stills:
                return stills[0]
        if self.kind == "video":
            vids = [f for f in self.files if f.ftype == "video"]
            return sorted(vids, key=lambda f: f.variant != "E")[0]
        return self.primary

    @property
    def live_video(self):
        if self.kind != "live":
            return None
        vids = [f for f in self.files if f.ftype == "video" and f.variant == ""]
        return vids[0] if vids else None

    @property
    def size(self):
        return sum(f.size for f in self.files)

    @property
    def sort_date(self):
        return self.date or datetime.fromtimestamp(self.primary.mtime)

    @property
    def media_files(self):
        return [f for f in self.files if f.ftype != "sidecar"]


def build_assets(files, key_prefix=""):
    """Gruppiert eine Liste von SourceFile (eines Ordners) zu Assets."""
    groups = {}
    for f in files:
        stem = os.path.splitext(f.name)[0]
        base, variant = base_and_variant(stem)
        f.variant = variant
        groups.setdefault(base.upper(), []).append(f)

    assets = []
    for base, members in groups.items():
        if not any(m.ftype != "sidecar" for m in members):
            continue  # verwaiste .AAE-Dateien
        originals = [m for m in members if m.variant == ""]
        has_still = any(m.ftype in ("photo", "raw") for m in originals)
        has_video = any(m.ftype == "video" for m in originals)
        if not originals:
            has_still = any(m.ftype in ("photo", "raw") for m in members)
            has_video = any(m.ftype == "video" for m in members)
        if has_still and has_video:
            kind = "live"
        elif has_video:
            kind = "video"
        else:
            kind = "photo"
        stills = [m for m in members if m.ftype == "photo"]
        folder = os.path.dirname(members[0].rel)
        a = Asset(
            key=f"{key_prefix}{folder}/{base}" if folder else f"{key_prefix}{base}",
            files=sorted(members, key=lambda m: m.name),
            kind=kind,
            raw=any(m.ftype == "raw" for m in members),
            edited=any(m.variant == "E" for m in members)
            or any(m.ftype == "sidecar" and m.variant == "" for m in members),
            screenshot=bool(stills) and all(s.name.lower().endswith(".png") for s in stills),
        )
        assets.append(a)
    return assets


def scan_tree(root, skip_dirs=(".ios-photos",), progress=None):
    """Durchsucht `root` rekursiv (versteckte Ordner werden übersprungen)."""
    assets = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in skip_dirs)
        files = []
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            ftype = file_type(name)
            if not ftype:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            files.append(SourceFile(path, rel, name, st.st_size, st.st_mtime, ftype))
        if files:
            assets.extend(build_assets(files))
            count += len(files)
            if progress:
                progress(count)
    return assets


def scan_dcim(mountpoint, progress=None):
    """Durchsucht den DCIM-Ordner eines iPhones. Gibt None zurück, wenn DCIM fehlt (gesperrt)."""
    dcim = os.path.join(mountpoint, "DCIM")
    if not os.path.isdir(dcim):
        return None
    return scan_tree(dcim, progress=progress)
