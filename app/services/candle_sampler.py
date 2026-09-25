import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, delete

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    Signal, SignalPaperTradeV06, TokenPairState, PairLatestPrice,
    AIEnsembleDecisionV074, PriceCandleV074,
)

log = logging.getLogger("candles")


def utc_naive(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def minute_bucket(now: datetime) -> datetime:
    return utc_naive(now).replace(second=0, microsecond=0)


async def _watch_mints(settings: Settings) -> list[str]:
    async with SessionLocal() as session:
        open_mints = list((await session.execute(
            select(SignalPaperTradeV06.token_mint)
            .where(SignalPaperTradeV06.status == "open")
            .order_by(SignalPaperTradeV06.opened_at.desc())
            .limit(settings.candle_watch_limit)
        )).scalars())

        ai_mints = list((await session.execute(
            select(AIEnsembleDecisionV074.token_mint)
            .order_by(AIEnsembleDecisionV074.created_at.desc())
            .limit(settings.candle_watch_limit)
        )).scalars())

        signal_mints = list((await session.execute(
            select(Signal.token_mint)
            .where(Signal.ts >= datetime.now(timezone.utc) - timedelta(minutes=10))
            .order_by(Signal.total_score.desc(), Signal.ts.desc())
            .limit(settings.candle_watch_limit)
        )).scalars())

        out, seen = [], set()
        for mint in open_mints + ai_mints + signal_mints:
            if mint and mint not in seen:
                seen.add(mint)
                out.append(mint)
            if len(out) >= settings.candle_watch_limit:
                break
        return out


async def _sample(settings: Settings) -> int:
    mints = await _watch_mints(settings)
    if not mints:
        return 0
    now = datetime.now(timezone.utc)
    now_db = utc_naive(now)
    count = 0
    async with SessionLocal() as session:
        states = list((await session.execute(
            select(TokenPairState).where(
                TokenPairState.token_mint.in_(mints),
                TokenPairState.status == "active",
            )
        )).scalars())
        for state in states:
            latest = await session.get(PairLatestPrice, state.pair_address)
            if (latest is None or latest.price_usd <= 0 or latest.base_mint != state.token_mint
                    or latest.source != "dexscreener_exact_pair"):
                continue
            ts = utc_naive(latest.ts)
            if ts is None or not 0 <= (now_db - ts).total_seconds() <= settings.market_max_price_age_seconds:
                continue
            bucket = minute_bucket(ts)
            row = (await session.execute(
                select(PriceCandleV074)
                .where(
                    PriceCandleV074.token_mint == state.token_mint,
                    PriceCandleV074.pair_address == state.pair_address,
                    PriceCandleV074.bucket_at == bucket,
                )
                .order_by(PriceCandleV074.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            px = float(latest.price_usd)
            if row is None:
                session.add(PriceCandleV074(
                    token_mint=state.token_mint,
                    pair_address=state.pair_address,
                    bucket_at=bucket,
                    open=px, high=px, low=px, close=px,
                    liquidity_usd=latest.liquidity_usd,
                    samples=1, last_observed_at=ts,
                ))
            else:
                if row.last_observed_at is not None and ts <= utc_naive(row.last_observed_at):
                    continue
                row.high = max(float(row.high), px)
                row.low = min(float(row.low), px)
                row.close = px
                row.liquidity_usd = latest.liquidity_usd
                row.samples = int(row.samples or 0) + 1
                row.last_observed_at = ts
            count += 1

        cutoff = now_db - timedelta(minutes=max(settings.candle_history_minutes, 30))
        await session.execute(delete(PriceCandleV074).where(PriceCandleV074.bucket_at < cutoff))
        await session.commit()
    return count


async def run_candle_sampler(settings: Settings, stop: asyncio.Event) -> None:
    log.info("Live candle sampler ACTIVE; %ss samples, %dm history", settings.candle_sample_seconds, settings.candle_history_minutes)
    while not stop.is_set():
        try:
            await _sample(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Candle sampler error: %s", str(exc)[:300])
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(settings.candle_sample_seconds, 2.0))
        except asyncio.TimeoutError:
            pass
