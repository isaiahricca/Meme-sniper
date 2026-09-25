import sqlite3
from types import SimpleNamespace

import pytest

from app.services import startup_backup


def database(tmp_path):
    path = tmp_path / "research.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE history(value TEXT)")
        db.execute("INSERT INTO history VALUES ('preserved')")
    return path, f"sqlite+aiosqlite:///{path.as_posix()}"


def test_backup_preserves_history_and_is_not_overwritten(tmp_path):
    path, url = database(tmp_path)
    backup = startup_backup.backup_before_integrity_upgrade(url)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO history VALUES ('later')")
    assert startup_backup.backup_before_integrity_upgrade(url) == backup
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT value FROM history").fetchall() == [("preserved",)]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 2


def test_low_disk_fails_without_modifying_source(tmp_path, monkeypatch):
    path, url = database(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(startup_backup.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match="Insufficient disk"):
        startup_backup.backup_before_integrity_upgrade(url)
    assert path.read_bytes() == before


def test_corrupt_existing_backup_fails_closed(tmp_path):
    path, url = database(tmp_path)
    backup = startup_backup.backup_before_integrity_upgrade(url)
    backup.write_bytes(b"corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        startup_backup.backup_before_integrity_upgrade(url)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM history").fetchone()[0] == "preserved"


def test_memory_and_absent_database_are_not_created(tmp_path):
    assert startup_backup.backup_before_integrity_upgrade("sqlite+aiosqlite:///:memory:") is None
    path = tmp_path / "absent.sqlite"
    assert startup_backup.backup_before_integrity_upgrade(f"sqlite+aiosqlite:///{path.as_posix()}") is None
    assert not path.exists()


def test_wal_committed_data_is_in_backup(tmp_path):
    path, url = database(tmp_path)
    with sqlite3.connect(path) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO history VALUES ('in-wal')")
        writer.commit()
        backup = startup_backup.backup_before_integrity_upgrade(url)
        with sqlite3.connect(backup) as reader:
            assert reader.execute("SELECT count(*) FROM history").fetchone()[0] == 2
