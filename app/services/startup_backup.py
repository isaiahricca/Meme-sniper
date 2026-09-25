"""Create a verified, one-time restore point before the integrity migration."""
import logging
from pathlib import Path
import shutil
import sqlite3
import time
from contextlib import closing

from sqlalchemy.engine import make_url

log = logging.getLogger("startup_backup")
BACKUP_NAME = "before-integrity-repair-20260925.sqlite"


def backup_before_integrity_upgrade(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    source = Path(url.database).resolve()
    if not source.exists():
        return None
    destination = source.parent / "backups" / BACKUP_NAME
    if destination.exists():
        with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as check:
            if check.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Existing pre-upgrade backup failed integrity validation")
        log.info("Pre-upgrade SQLite backup already verified: %s", destination.name)
        return destination

    destination.parent.mkdir(exist_ok=True)
    started = time.monotonic()
    temporary = destination.with_suffix(".partial")
    # Exclusive creation prevents overwriting an incomplete or concurrent backup.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as original:
        required = original.execute("PRAGMA page_count").fetchone()[0] * original.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(destination.parent).free < required + 128 * 1024 * 1024:
            raise RuntimeError("Insufficient disk space for pre-upgrade SQLite backup; database unchanged")
        with temporary.open("xb"):
            pass
        try:
            def progress(status, remaining, total):
                if time.monotonic() - started > 180:
                    raise TimeoutError("Pre-upgrade SQLite backup exceeded 180 seconds")
            with closing(sqlite3.connect(temporary)) as target:
                original.backup(target, pages=512, progress=progress, sleep=0.05)
                if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("New pre-upgrade backup failed integrity validation")
            temporary.rename(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    log.info("Pre-upgrade SQLite backup VERIFIED: %s (%d bytes)", destination.name, destination.stat().st_size)
    return destination
