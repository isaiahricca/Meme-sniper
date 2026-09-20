from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import PaperCopyTradeV06, SignalPaperTradeV06, SystemState
from app.services.performance_math import calculate_performance

EPOCH_KEY = "v06_verified_epoch"
PERTH = ZoneInfo("Australia/Perth")
settings = get_settings()


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _range_start(range_name: str, epoch: datetime, now: datetime, forward: datetime | None = None) -> datetime:
    key = (range_name or "all").lower()
    if key == "forward" and forward is not None:
        return max(epoch, forward)
    if key == "1h":
        return max(epoch, now - timedelta(hours=1))
    if key == "7d":
        return max(epoch, now - timedelta(days=7))
    if key == "30d":
        return max(epoch, now - timedelta(days=30))
    if key == "today":
        local = now.astimezone(PERTH)
        start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return max(epoch, start_local.astimezone(timezone.utc))
    return epoch


async def verified_epoch(session) -> datetime | None:
    row = await session.get(SystemState, EPOCH_KEY)
    if row is None:
        return None
    return aware(datetime.fromisoformat(row.value))


async def _forward_epoch(session) -> datetime | None:
    row = await session.get(SystemState, settings.forward_epoch_key)
    if row is None:
        return None
    return aware(datetime.fromisoformat(row.value))


async def build_performance(strategy: str = "all", range_name: str = "all") -> dict:
    strategy = (strategy or "all").lower()
    if strategy not in {"all", "copy", "signal"}:
        strategy = "all"

    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        epoch = await verified_epoch(session)
        forward = await _forward_epoch(session)
        if epoch is None:
            return {
                "strategy": strategy, "range": range_name, "epoch": None, "forward_epoch": None,
                "realized_pnl_usd": 0.0, "trades": 0, "wins": 0,
                "win_rate_pct": 0.0, "max_drawdown_usd": 0.0,
                "profit_factor": None, "gross_profit_usd": 0.0,
                "gross_loss_usd": 0.0, "open_trades": 0, "curve": [],
            }
        start = _range_start(range_name, epoch, now, forward)
        events: list[dict] = []
        open_count = 0

        if strategy in {"all", "copy"}:
            rows = list((await session.execute(
                select(PaperCopyTradeV06).where(
                    PaperCopyTradeV06.status == "closed",
                    PaperCopyTradeV06.integrity_status == "verified_closed",
                    PaperCopyTradeV06.exited_at >= start,
                )
            )).scalars())
            for row in rows:
                if row.pnl_usd is None or row.exited_at is None:
                    continue
                events.append({
                    "ts": aware(row.exited_at), "pnl": float(row.pnl_usd),
                    "strategy": "Smart wallet", "token": row.token_mint,
                    "wallet": row.wallet, "id": row.id,
                })
            open_count += len(list((await session.execute(
                select(PaperCopyTradeV06.id).where(PaperCopyTradeV06.status == "open")
            )).scalars()))

        if strategy in {"all", "signal"}:
            rows = list((await session.execute(
                select(SignalPaperTradeV06).where(
                    SignalPaperTradeV06.status == "closed",
                    SignalPaperTradeV06.integrity_status == "verified_closed",
                    SignalPaperTradeV06.closed_at >= start,
                )
            )).scalars())
            for row in rows:
                if row.pnl_usd is None or row.closed_at is None:
                    continue
                events.append({
                    "ts": aware(row.closed_at), "pnl": float(row.pnl_usd),
                    "strategy": "Signal", "token": row.token_mint,
                    "wallet": None, "id": row.id,
                })
            open_count += len(list((await session.execute(
                select(SignalPaperTradeV06.id).where(
                    SignalPaperTradeV06.status == "open",
                    SignalPaperTradeV06.integrity_status == "verified_open",
                )
            )).scalars()))

    metrics = calculate_performance(events)
    metrics.update({
        "strategy": strategy,
        "range": range_name,
        "epoch": epoch.isoformat(),
        "forward_epoch": forward.isoformat() if forward else None,
        "open_trades": open_count,
    })
    return metrics
