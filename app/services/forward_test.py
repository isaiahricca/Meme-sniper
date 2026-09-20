from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, func

from app.config import Settings
from app.db import SessionLocal
from app.models import SystemState, PaperCopyTradeV06, SignalPaperTradeV06

PERTH = ZoneInfo("Australia/Perth")


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def ensure_forward_epoch(settings: Settings) -> datetime:
    """Create a clean V0.7.3 forward-test epoch once and quarantine old pending/open trades.

    Historical verified trades stay in the database and remain visible under All/7D/30D.
    The forward epoch only separates the new strategy regime from the old losing regime.
    """
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        row = await session.get(SystemState, settings.forward_epoch_key)
        if row is not None:
            return aware(datetime.fromisoformat(row.value)) or now

        # Do not let V0.7.2 pending/open positions contaminate the new forward test.
        pending_copy = list((await session.execute(
            select(PaperCopyTradeV06).where(PaperCopyTradeV06.status == "pending")
        )).scalars())
        for trade in pending_copy:
            trade.status = "rejected"
            trade.integrity_status = "rejected"
            trade.reject_reason = "v073_forward_epoch_quarantine"

        open_copy = list((await session.execute(
            select(PaperCopyTradeV06).where(PaperCopyTradeV06.status == "open")
        )).scalars())
        for trade in open_copy:
            trade.status = "invalid"
            trade.integrity_status = "invalid"
            trade.exited_at = now
            trade.exit_reason = "v073_forward_epoch_quarantine"

        pending_signal = list((await session.execute(
            select(SignalPaperTradeV06).where(SignalPaperTradeV06.status == "pending")
        )).scalars())
        for trade in pending_signal:
            trade.status = "rejected"
            trade.integrity_status = "rejected"
            trade.exit_reason = "v073_forward_epoch_quarantine"

        open_signal = list((await session.execute(
            select(SignalPaperTradeV06).where(SignalPaperTradeV06.status == "open")
        )).scalars())
        for trade in open_signal:
            trade.status = "invalid"
            trade.integrity_status = "invalid"
            trade.closed_at = now
            trade.exit_reason = "v073_forward_epoch_quarantine"

        session.add(SystemState(key=settings.forward_epoch_key, value=now.isoformat()))
        await session.commit()
        return now


async def forward_epoch(settings: Settings) -> datetime | None:
    async with SessionLocal() as session:
        row = await session.get(SystemState, settings.forward_epoch_key)
        if row is None:
            return None
        return aware(datetime.fromisoformat(row.value))


def perth_day_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(PERTH)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def verified_copy_pnl_since(start: datetime) -> float:
    async with SessionLocal() as session:
        value = (await session.execute(
            select(func.coalesce(func.sum(PaperCopyTradeV06.pnl_usd), 0.0)).where(
                PaperCopyTradeV06.status == "closed",
                PaperCopyTradeV06.integrity_status == "verified_closed",
                PaperCopyTradeV06.exited_at >= start,
            )
        )).scalar_one()
        return float(value or 0.0)


async def copy_daily_circuit_open(settings: Settings) -> tuple[bool, float, datetime]:
    epoch = await forward_epoch(settings)
    start = perth_day_start_utc()
    if epoch is not None and epoch > start:
        start = epoch
    pnl = await verified_copy_pnl_since(start)
    return pnl <= -abs(settings.paper_copy_daily_loss_limit_usd), pnl, start


async def shadow_signal_summary(settings: Settings) -> dict:
    """Return V0.7.3 signal shadow results without mixing them into verified P/L."""
    epoch = await forward_epoch(settings)
    if epoch is None:
        return {"trades": 0, "wins": 0, "pnl_usd": 0.0, "profit_factor": None, "win_rate_pct": 0.0}
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(SignalPaperTradeV06).where(
                SignalPaperTradeV06.status == "closed",
                SignalPaperTradeV06.integrity_status == "shadow_closed",
                SignalPaperTradeV06.closed_at >= epoch,
            )
        )).scalars())
    pnls = [float(x.pnl_usd or 0.0) for x in rows]
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = -sum(x for x in pnls if x < 0)
    wins = sum(1 for x in pnls if x > 0)
    return {
        "trades": len(pnls),
        "wins": wins,
        "pnl_usd": round(sum(pnls), 4),
        "gross_profit_usd": round(gross_profit, 4),
        "gross_loss_usd": round(gross_loss, 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else (None if gross_profit == 0 else 999.0),
        "win_rate_pct": round(wins / len(pnls) * 100.0, 1) if pnls else 0.0,
    }
