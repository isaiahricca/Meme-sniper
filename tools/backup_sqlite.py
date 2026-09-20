from __future__ import annotations
import argparse
import sqlite3
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description="Create a consistent SQLite backup for cloud migration.")
    ap.add_argument("source", nargs="?", default="memesniper.db")
    ap.add_argument("dest", nargs="?", default="cloud_seed/memesniper.db")
    args = ap.parse_args()
    src = Path(args.source).resolve()
    dst = Path(args.dest).resolve()
    if not src.exists():
        raise SystemExit(f"Source database not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(dst.as_posix(), timeout=30)
    try:
        source.backup(target)
        target.execute("PRAGMA integrity_check")
        result = target.fetchone() if False else None
        target.commit()
    finally:
        target.close()
        source.close()
    # Verify separately so the cursor result is actually consumed.
    check = sqlite3.connect(dst.as_posix())
    try:
        verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if verdict != "ok":
        dst.unlink(missing_ok=True)
        raise SystemExit(f"Backup integrity check failed: {verdict}")
    print(f"Cloud database backup ready: {dst}")


if __name__ == "__main__":
    main()
