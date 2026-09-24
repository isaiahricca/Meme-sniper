from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from .config import get_settings
from .models import Base

settings = get_settings()

engine_kwargs = dict(future=True, pool_pre_ping=True)
if settings.database_url.startswith("sqlite+aiosqlite"):
    engine_kwargs["connect_args"] = {"timeout": 30}

engine = create_async_engine(settings.database_url, **engine_kwargs)

if settings.database_url.startswith("sqlite+aiosqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        # Keep WAL growth bounded on the small Railway persistent volume.
        cursor.execute("PRAGMA journal_size_limit=16777216")
        cursor.close()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        # Recover small persistent SQLite volumes before normal writers start.
        # Raw Event rows are telemetry, not paper-trade/AI performance history.
        # Pruning them preserves the strategy records while freeing reusable pages.
        if settings.database_url.startswith("sqlite+aiosqlite"):
            def _recover_sqlite(sync_conn):
                try:
                    sync_conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    tables = {
                        row[0] for row in sync_conn.exec_driver_sql(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "events" in tables:
                        sync_conn.exec_driver_sql(
                            "DELETE FROM events WHERE ts < datetime('now', '-6 hours')"
                        )
                except Exception:
                    # Startup should still proceed if there is nothing safe to prune.
                    pass
            await conn.run_sync(_recover_sqlite)

        await conn.run_sync(Base.metadata.create_all)

        # Lightweight forward migration for persistent SQLite deployments.
        # create_all() creates new tables but does not add columns to an existing
        # table, which matters because Railway keeps /data across deploys.
        if settings.database_url.startswith("sqlite+aiosqlite"):
            def _migrate_sqlite(sync_conn):
                candle_columns = {r[1] for r in sync_conn.exec_driver_sql(
                    "PRAGMA table_info(price_candles_v074)"
                ).fetchall()}
                if candle_columns and "last_observed_at" not in candle_columns:
                    sync_conn.exec_driver_sql("ALTER TABLE price_candles_v074 ADD COLUMN last_observed_at DATETIME")
                rows = sync_conn.exec_driver_sql(
                    "PRAGMA table_info(ai_ensemble_decisions_v074)"
                ).fetchall()
                if not rows:
                    return
                existing = {row[1] for row in rows}
                additions = [
                    ("signal_age_seconds", "REAL"),
                    ("trade_status_at_analysis", "VARCHAR(30)"),
                    ("pre_entry", "BOOLEAN DEFAULT 1"),
                ]
                for name, ddl in additions:
                    if name not in existing:
                        sync_conn.exec_driver_sql(
                            f"ALTER TABLE ai_ensemble_decisions_v074 ADD COLUMN {name} {ddl}"
                        )
            await conn.run_sync(_migrate_sqlite)


async def get_session():
    async with SessionLocal() as session:
        yield session
