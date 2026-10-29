import builtins
import sqlite3
from pathlib import Path

import pytest

from ios_photos.library.importer import run_import
from ios_photos.media.scanner import scan_dcim


@pytest.mark.parametrize("photo_database", ["missing", "corrupt", "deleted"])
def test_scan_and_import_never_access_photodata(phone, library, monkeypatch, photo_database):
    photo_data = phone / "PhotoData"
    photo_data.mkdir()
    database = photo_data / "Photos.sqlite"
    if photo_database == "corrupt":
        database.write_bytes(b"not sqlite")
    elif photo_database == "deleted":
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE ZASSET (ZFILENAME TEXT, ZTRASHEDSTATE INTEGER, ZHIDDEN INTEGER)")
            db.execute("INSERT INTO ZASSET VALUES ('IMG_0001.JPG',1,1)")
    before = {p.name: p.read_bytes() for p in photo_data.iterdir()}
    connect, open_file = sqlite3.connect, builtins.open

    def checked_connect(path, *args, **kwargs):
        assert str(phone) not in str(path), "SQLite must never open the device"
        return connect(path, *args, **kwargs)

    def checked_open(path, *args, **kwargs):
        assert "PhotoData" not in str(path), "PhotoData must not be read"
        return open_file(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    monkeypatch.setattr(builtins, "open", checked_open)
    assets = scan_dcim(str(phone))
    assert len(assets) == 4
    result = run_import(library, assets, udid="test-device")
    assert result.imported == 4 and not result.fatal and not result.failed
    for asset in library.query():
        for file in asset.files:
            original = next(f for a in assets for f in a.files if f.name == file.name)
            assert Path(file.path).read_bytes() == Path(original.path).read_bytes()
    again = run_import(library, scan_dcim(str(phone)), udid="test-device")
    assert again.imported == 0 and again.skipped == 4
    assert {p.name: p.read_bytes() for p in photo_data.iterdir()} == before


def test_existing_deleted_flag_does_not_hide_local_files(phone, library):
    result = run_import(library, scan_dcim(str(phone)), udid="test-device")
    album = library.create_album("Existing")
    library.add_to_album(album, result.asset_ids)
    library.db.execute("ALTER TABLE assets ADD COLUMN iphone_deleted INTEGER DEFAULT 1")
    library.commit()
    assert len(library.query()) == library.counts()["all"] == 4
    assert library.albums()[0]["count"] == 4
