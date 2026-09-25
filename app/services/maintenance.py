"""Bound disposable storage without deleting trades, decisions or measurements."""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, delete

from app.db import SessionLocal
from app.models import Event, PairPriceObservation

log = logging.getLogger("maintenance")


async def prune_telemetry():
    now = datetime.now(timezone.utc)
    removed = {}
    # Bounded transactions avoid a large DELETE monopolising SQLite's writer.
    for model, cutoff in ((Event, now - timedelta(hours=6)),
                          (PairPriceObservation, now - timedelta(minutes=10))):
        async with SessionLocal() as session:
            ids = select(model.id).where(model.ts < cutoff).order_by(model.ts).limit(5000)
            result = await session.execute(delete(model).where(model.id.in_(ids)))
            await session.commit()
            removed[model.__tablename__] = result.rowcount
    return removed


async def run_maintenance(settings, stop):
    while not stop.is_set():
        try:
            removed = await prune_telemetry()
            if any(removed.values()):
                log.info("Disposable telemetry pruned: %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telemetry maintenance failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
