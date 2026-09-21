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
        cursor.close()

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Lightweight forward migration for persistent SQLite deployments.
        # create_all() creates new tables but does not add columns to an existing
        # table, which matters because Railway keeps /data across deploys.
        if settings.database_url.startswith("sqlite+aiosqlite"):
            def _migrate_sqlite(sync_conn):
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
