"""Liest Aufnahmedatum, Dauer und Abmessungen per exiftool (parallel, in Stapeln)."""

import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

TAGS = [
    "-DateTimeOriginal", "-CreationDate", "-CreateDate", "-DateCreated",
    "-Duration", "-ImageWidth", "-ImageHeight", "-Rotation", "-Orientation",
]
BATCH = 100


def have_exiftool():
    return shutil.which("exiftool") is not None


def parse_date(value):
    """'2026:10:11 13:10:23+02:00' -> naive datetime in Ortszeit der Aufnahme."""
    if not isinstance(value, str) or len(value) < 19 or value.startswith("0000"):
        return None
    try:
        return datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _interpret(rec):
    date = None
    for tag in ("DateTimeOriginal", "CreationDate", "DateCreated", "CreateDate"):
        date = parse_date(rec.get(tag))
        if date:
            break
    w, h = rec.get("ImageWidth"), rec.get("ImageHeight")
    rot = rec.get("Rotation")
    if isinstance(w, int) and isinstance(h, int):
        if rot in (90, 270) or rec.get("Orientation") in (5, 6, 7, 8):
            w, h = h, w
    else:
        w = h = None
    dur = rec.get("Duration")
    return {
        "date": date,
        "duration": float(dur) if isinstance(dur, (int, float)) else None,
        "width": w,
        "height": h,
    }


def _run_batch(paths):
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as fh:
        fh.write("\n".join(paths))
        argfile = fh.name
    try:
        proc = subprocess.run(
            ["exiftool", "-json", "-n", "-fast", "-charset", "filename=utf8",
             "-api", "QuickTimeUTC=1", *TAGS, "-@", argfile],
            capture_output=True, timeout=600,
        )
        try:
            records = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
        except ValueError:
            records = []
    except (OSError, subprocess.SubprocessError):
        records = []
    finally:
        os.unlink(argfile)
    return {r.get("SourceFile"): _interpret(r) for r in records}


def read_metadata(paths, progress=None, cancel=None, workers=None):
    """Gibt {pfad: {date, duration, width, height}} zurück."""
    paths = list(paths)
    result = {}
    if not paths or not have_exiftool():
        return result
    workers = workers or max(1, min(4, (os.cpu_count() or 2) - 1))
    batches = [paths[i:i + BATCH] for i in range(0, len(paths), BATCH)]
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for batch_result, batch in zip(pool.map(_run_batch, batches), batches):
            result.update(batch_result)
            done += len(batch)
            if progress:
                progress(done, len(paths))
            if cancel is not None and cancel.is_set():
                pool.shutdown(cancel_futures=True)
                break
    return result


def apply_metadata(assets, progress=None, cancel=None, cache=None):
    """Ergänzt Datum/Dauer/Maße der Assets. `cache` ist ein MetaCache (optional)."""
    todo = {}
    for a in assets:
        if a.date is not None:
            continue
        paths = [a.primary.path]
        if a.kind == "video" and a.display.path != a.primary.path:
            paths.append(a.display.path)
        for f in a.files:
            if f.path in paths and cache is not None:
                hit = cache.get(f)
                if hit is not None:
                    _merge(a, hit)
                    continue
            if f.path in paths:
                todo[f.path] = f
    meta = read_metadata(list(todo), progress=progress, cancel=cancel)
    if cache is not None:
        cache.put_many((todo[p], m) for p, m in meta.items() if p in todo)
    for a in assets:
        for f in a.files:
            if f.path in meta:
                _merge(a, meta[f.path])
    return assets


def _merge(asset, m):
    if asset.date is None and m.get("date"):
        asset.date = m["date"]
    if asset.duration is None and m.get("duration") and asset.kind == "video":
        asset.duration = m["duration"]
    if asset.width is None and m.get("width"):
        asset.width, asset.height = m["width"], m["height"]
