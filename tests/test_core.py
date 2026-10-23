import os
import threading
from datetime import datetime

from conftest import make_file, make_jpeg

from ios_photos.library import legacy
from ios_photos.library.db import Library
from ios_photos.library.importer import run_import
from ios_photos.library.relocate import RelocateError, relocate
from ios_photos.media.metadata import apply_metadata
from ios_photos.media.scanner import build_assets, scan_dcim, scan_tree, SourceFile
from ios_photos.media.thumbnails import get_thumbnail


def _sf(name, ftype, size=10):
    return SourceFile(f"/x/{name}", f"100APPLE/{name}", name, size, 0, ftype)


def test_grouping():
    files = [
        _sf("IMG_0001.HEIC", "photo"), _sf("IMG_0001.MOV", "video"),
        _sf("IMG_0002.JPG", "photo"), _sf("IMG_0002.AAE", "sidecar"),
        _sf("IMG_E0002.JPG", "photo"), _sf("IMG_O0002.AAE", "sidecar"),
        _sf("IMG_0003.MOV", "video"),
        _sf("DSC00071.JPG", "photo"), _sf("DSC00071.ARW", "raw"),
        _sf("IMG_0004.PNG", "photo"),
        _sf("IMG_0005.AAE", "sidecar"),
    ]
    assets = {a.key: a for a in build_assets(files)}
    assert set(assets) == {f"100APPLE/{k}" for k in
                           ("IMG_0001", "IMG_0002", "IMG_0003", "DSC00071", "IMG_0004")}
    live = assets["100APPLE/IMG_0001"]
    assert live.kind == "live" and live.live_video.name == "IMG_0001.MOV"
    assert live.primary.name == "IMG_0001.HEIC"
    edited = assets["100APPLE/IMG_0002"]
    assert edited.kind == "photo" and edited.edited and len(edited.files) == 4
    assert edited.primary.name == "IMG_0002.JPG" and edited.display.name == "IMG_E0002.JPG"
    assert assets["100APPLE/IMG_0003"].kind == "video"
    assert assets["100APPLE/DSC00071"].raw and assets["100APPLE/DSC00071"].primary.name == "DSC00071.JPG"
    assert assets["100APPLE/IMG_0004"].screenshot


def test_scan_and_metadata(phone):
    assets = scan_dcim(str(phone))
    apply_metadata(assets)
    by = {a.primary.name: a for a in assets}
    assert by["IMG_0001.JPG"].kind == "live"
    assert by["IMG_0001.JPG"].date == datetime(2024, 5, 1, 10, 0, 0)
    assert by["IMG_0003.MOV"].kind == "video"
    assert scan_dcim(str(phone / "nope")) is None


def _import(library, phone, **kw):
    assets = scan_dcim(str(phone))
    apply_metadata(assets)
    return assets, run_import(library, assets, udid="u1", device_name="Test", **kw)


def test_import_and_dedupe(phone, library):
    assets, res = _import(library, phone)
    assert res.imported == 4 and not res.failed and not res.cancelled
    root = library.root
    assert (root / "2024/05/01/IMG_0001.JPG").exists()
    assert (root / "2024/05/01/IMG_0001.MOV").exists()
    assert (root / "2024/05/02/IMG_0002.AAE").exists()
    assert (root / "2023/12/24/IMG_0100.JPG").exists()
    assert not list(root.rglob("*.part"))
    assert library.count() == 4
    assert library.imports()[0]["count"] == 4
    # zweiter Durchlauf: alles bereits importiert
    assets2 = library.mark_status(scan_dcim(str(phone)))
    assert all(a.status == "imported" for a in assets2)
    _, res2 = _import(library, phone)
    assert res2.imported == 0 and res2.skipped == 4 and res2.import_id is None


def test_collision_keeps_pairs_together(phone, library):
    make_file(str(library.root / "2024/05/01/img_0001.jpg"), b"other")
    _, res = _import(library, phone)
    assert res.imported == 4
    assert (library.root / "2024/05/01/IMG_0001_1.JPG").exists()
    assert (library.root / "2024/05/01/IMG_0001_1.MOV").exists()


def test_album_and_partial(phone, library):
    album = library.create_album("Urlaub")
    _, res = _import(library, phone, album_id=album)
    assert library.albums()[0]["count"] == 4
    assert len(library.query(album_id=album)) == 4
    # später auf dem iPhone bearbeitet → IMG_E0002.JPG kommt dazu
    make_jpeg(str(phone / "DCIM/100APPLE/IMG_E0002.JPG"), datetime(2024, 5, 2, 11, 0, 0))
    assets = library.mark_status(scan_dcim(str(phone)))
    new = [a for a in assets if a.status == "new"]
    assert len(new) == 1 and new[0].extra.get("partial_of")
    res = run_import(library, assets)
    assert res.imported == 1
    assert (library.root / "2024/05/02/IMG_E0002.JPG").exists()
    assert library.count() == 4


def test_cancel(phone, library):
    ev = threading.Event()
    ev.set()
    _, res = _import(library, phone, cancel=ev)
    assert res.cancelled and res.imported == 0
    assert library.count() == 0
    assert not [p for p in library.root.rglob("*") if p.is_file() and ".ios-photos" not in str(p)]


def test_legacy_index_and_reorganize(tmp_path):
    root = tmp_path / "backup"
    make_jpeg(str(root / "100APPLE/IMG_0001.JPG"), datetime(2022, 1, 2, 3, 4, 5))
    make_file(str(root / "100APPLE/IMG_0001.AAE"), b"<plist/>")
    make_jpeg(str(root / "101APPLE/IMG_0001.JPG"), datetime(2022, 1, 2, 9, 0, 0), color=(1, 2, 3),
              size=(80, 60))
    make_file(str(root / ".MISC/Info.plist"))
    make_file(str(root / "ispDump.bin"))
    lib = Library(root)
    assert legacy.index_library(lib) == 2
    assert legacy.index_library(lib) == 0
    plan = legacy.plan_reorganize(lib)
    assert plan.assets == 2 and plan.files == 3
    ok, errors = legacy.execute_reorganize(lib, plan)
    assert ok == 3 and not errors
    names = sorted(os.listdir(root / "2022/01/02"))
    assert names == ["IMG_0001.AAE", "IMG_0001.JPG", "IMG_0001_1.JPG"]
    assert not (root / "100APPLE").exists() and (root / ".MISC/Info.plist").exists()
    assert legacy.plan_reorganize(lib).files == 0
    assert lib.remove_missing() == 0
    lib.close()


def test_relocate(phone, library, tmp_path):
    _import(library, phone)
    src = str(library.root)
    library.close()
    target = str(tmp_path / "elsewhere" / "Mediathek")
    try:
        relocate(src, os.path.join(src, "sub"))
    except RelocateError:
        pass
    else:
        raise AssertionError("Ziel innerhalb der Mediathek muss abgelehnt werden")
    relocate(src, target, delete_source=False)
    lib2 = Library(target)
    assert lib2.count() == 4 and lib2.remove_missing() == 0
    assert os.path.exists(os.path.join(target, "2024/05/01/IMG_0001.MOV"))
    lib2.close()


def test_thumbnails(phone, tmp_path):
    from PIL import Image
    import pillow_heif
    heic = tmp_path / "x.heic"
    Image.new("RGB", (400, 300), (10, 20, 30)).save(heic, format="HEIF")
    assert pillow_heif
    for p in (phone / "DCIM/100APPLE/IMG_0001.JPG", phone / "DCIM/100APPLE/IMG_0003.MOV", heic):
        out = get_thumbnail(str(tmp_path / "cache"), str(p), size=100)
        assert Image.open(out).size[0] <= 100


def test_scan_tree_skips_hidden(tmp_path):
    make_jpeg(str(tmp_path / "a/IMG_1.JPG"))
    make_jpeg(str(tmp_path / ".hidden/IMG_2.JPG"))
    assert [a.primary.name for a in scan_tree(str(tmp_path))] == ["IMG_1.JPG"]
