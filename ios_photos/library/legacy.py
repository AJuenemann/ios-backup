"""Altbestand erfassen (z. B. alte DCIM-Kopien) und in die Datumsstruktur JJJJ/MM/TT einsortieren."""

import os
import re
from dataclasses import dataclass, field

from ..media.metadata import apply_metadata
from ..media.scanner import scan_tree
from .importer import _DirIndex, date_dir, plan_names

DATE_DIR_RE = re.compile(r"^\d{4}/\d{2}/\d{2}/[^/]+$")


def index_library(library, progress=None, cancel=None):
    """Nimmt alle noch unbekannten Mediendateien unterhalb der Mediathek in den Index auf.

    progress(phase, done, total) mit phase in {"scan", "meta", "save"}.
    Gibt die Anzahl neu erfasster Objekte zurück.
    """
    known = library.known_rels()
    assets = scan_tree(str(library.root), progress=lambda n: progress and progress("scan", n, 0))
    new = [a for a in assets if not any(f.rel in known for f in a.files)]
    if cancel is not None and cancel.is_set():
        return 0
    apply_metadata(new, progress=lambda d, t: progress and progress("meta", d, t), cancel=cancel)
    if cancel is not None and cancel.is_set():
        return 0
    for i, a in enumerate(new):
        library.add_asset(a, {f: f.rel for f in a.files}, legacy=True, commit=False)
        if progress and i % 200 == 0:
            progress("save", i, len(new))
    library.commit()
    library.set_meta("indexed", "1")
    return len(new)


@dataclass
class ReorgPlan:
    moves: list = field(default_factory=list)   # [(alt_rel, neu_rel)]
    assets: int = 0

    @property
    def files(self):
        return len(self.moves)


def plan_reorganize(library):
    """Ermittelt, welche Dateien außerhalb von JJJJ/MM/TT liegen und wohin sie verschoben würden."""
    plan = ReorgPlan()
    index = _DirIndex(str(library.root))
    for asset in library.query():
        if all(DATE_DIR_RE.match(f.rel) for f in asset.files):
            continue
        target = date_dir(asset)
        names = plan_names(asset.files, target, index)
        for f in asset.files:
            index.add(target, names[f])
            plan.moves.append((f.rel, f"{target}/{names[f]}"))
        plan.assets += 1
    return plan


def execute_reorganize(library, plan, progress=None, cancel=None):
    """Verschiebt die Dateien gemäß Plan (rename, gleiches Laufwerk). Gibt (ok, fehler) zurück."""
    root = str(library.root)
    ok, errors = 0, []
    old_dirs = set()
    for i, (old, new) in enumerate(plan.moves):
        if cancel is not None and cancel.is_set():
            break
        src, dst = os.path.join(root, old), os.path.join(root, new)
        try:
            if os.path.exists(dst):
                raise FileExistsError(f"{new} existiert bereits")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
            library.move_file(old, new, commit=False)
            old_dirs.add(os.path.dirname(src))
            ok += 1
        except OSError as e:
            errors.append((old, str(e)))
        if i % 100 == 0:
            library.commit()
            if progress:
                progress(i + 1, len(plan.moves))
    library.commit()
    if progress:
        progress(len(plan.moves), len(plan.moves))
    for d in sorted(old_dirs, key=len, reverse=True):
        _remove_if_empty(d, root)
    return ok, errors


def _remove_if_empty(d, root):
    while os.path.realpath(d) != os.path.realpath(root):
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)
