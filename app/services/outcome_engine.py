
import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import SignalMeasurement, Token

log = logging.getLogger("outcomes")


async def capture_due_measurements() -> int:
    now = datetime.now(timezone.utc)
    captured = 0
    async with SessionLocal() as session:
        rows = list((
            await session.execute(
                select(SignalMeasurement)
                .where(
                    SignalMeasurement.captured_at.is_(None),
                    SignalMeasurement.due_at <= now,
                )
                .order_by(SignalMeasurement.due_at.asc())
                .limit(200)
            )
        ).scalars())

        for m in rows:
            token = await session.get(Token, m.token_mint)
            if not token or not token.price_usd or token.price_usd <= 0:
                continue
            m.captured_at = now
            m.observed_price = token.price_usd
            m.return_pct = ((token.price_usd / m.baseline_price) - 1.0) * 100.0
            captured += 1

        await session.commit()
    return captured


async def run_outcome_engine(settings: Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await capture_due_measurements()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Outcome engine error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
