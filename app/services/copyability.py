import asyncio
import logging
import statistics
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    Token, WalletTradeMeasurement, WalletCopyability,
    SmartWalletProfile, TrackedWallet
)

log = logging.getLogger("copyability")


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def hft_penalty(trades_30d: int | None, soft: int, hard: int) -> float:
    if trades_30d is None or trades_30d <= soft:
        return 0.0
    if hard <= soft:
        return 40.0
    ratio = (trades_30d - soft) / (hard - soft)
    return round(clamp(ratio, 0.0, 1.0) * 40.0, 2)


def return_component(value: float | None) -> float:
    if value is None:
        return 50.0
    return clamp(50.0 + value * 2.2)


def observed_edge_score(
    avg10: float | None,
    avg30: float | None,
    avg60: float | None,
    avg300: float | None,
    positive30: float | None,
    target_hit: float | None,
) -> float:
    values = [
        (avg10, 0.10), (avg30, 0.25), (avg60, 0.25), (avg300, 0.15)
    ]
    total = 0.0
    weight = 0.0
    for value, w in values:
        if value is not None:
            total += return_component(value) * w
            weight += w
    if positive30 is not None:
        total += clamp(positive30) * 0.15
        weight += 0.15
    if target_hit is not None:
        total += clamp(target_hit) * 0.10
        weight += 0.10
    return round(total / weight, 2) if weight else 50.0


def combine_score(
    birdeye_score: float,
    live_edge: float,
    observations: int,
    minimum: int,
    penalty: float,
) -> float:
    evidence = clamp(observations / max(minimum, 1), 0.0, 1.0)
    live_weight = 0.25 + 0.50 * evidence
    base = birdeye_score * (1.0 - live_weight) + live_edge * live_weight
    return round(clamp(base - penalty), 2)


def tier(score: float, observations: int, minimum: int) -> str:
    if observations < minimum:
        return "UNPROVEN"
    if score >= 85:
        return "A"
    if score >= 72:
        return "B"
    if score >= 60:
        return "C"
    return "AVOID"


def avg(items):
    return statistics.mean(items) if items else None


async def capture_due() -> int:
    now = datetime.now(timezone.utc)
    captured = 0
    async with SessionLocal() as session:
        rows = list((
            await session.execute(
                select(WalletTradeMeasurement)
                .where(
                    WalletTradeMeasurement.captured_at.is_(None),
                    WalletTradeMeasurement.due_at <= now,
                )
                .order_by(WalletTradeMeasurement.due_at.asc())
                .limit(500)
            )
        ).scalars())

        for row in rows:
            token = await session.get(Token, row.token_mint)
            if not token or not token.price_usd or token.price_usd <= 0:
                continue
            row.captured_at = now
            row.observed_price = token.price_usd
            row.return_pct = ((token.price_usd / row.baseline_price) - 1.0) * 100.0
            captured += 1
        await session.commit()
    return captured


async def recalc_wallet(settings: Settings, wallet: str) -> None:
    async with SessionLocal() as session:
        profile = await session.get(SmartWalletProfile, wallet)
        if profile is None:
            return

        rows = list((
            await session.execute(
                select(WalletTradeMeasurement)
                .where(
                    WalletTradeMeasurement.wallet == wallet,
                    WalletTradeMeasurement.captured_at.is_not(None),
                )
                .order_by(WalletTradeMeasurement.captured_at.desc())
                .limit(2000)
            )
        ).scalars())

        by_horizon = {}
        by_trade = {}
        for row in rows:
            if row.return_pct is None:
                continue
            by_horizon.setdefault(row.horizon_seconds, []).append(row.return_pct)
            by_trade.setdefault(row.wallet_trade_id, []).append(row)

        # Only count trades with at least a 30-second observation as real evidence.
        completed_trades = {
            trade_id: samples for trade_id, samples in by_trade.items()
            if any(x.horizon_seconds >= 30 for x in samples)
        }
        observations = len(completed_trades)

        avg10 = avg(by_horizon.get(10, []))
        avg30 = avg(by_horizon.get(30, []))
        avg60 = avg(by_horizon.get(60, []))
        avg300 = avg(by_horizon.get(300, []))

        r30 = by_horizon.get(30, [])
        positive30 = (
            sum(1 for x in r30 if x > 0) / len(r30) * 100.0
            if r30 else None
        )

        target_hits = 0
        lead_horizons = []
        for samples in completed_trades.values():
            hits = sorted(
                x.horizon_seconds for x in samples
                if x.return_pct is not None
                and x.return_pct >= settings.copyability_target_return_pct
            )
            if hits:
                target_hits += 1
                lead_horizons.append(hits[0])

        target_hit = (
            target_hits / observations * 100.0 if observations else None
        )
        median_lead = (
            statistics.median(lead_horizons) if lead_horizons else None
        )

        edge = observed_edge_score(
            avg10, avg30, avg60, avg300, positive30, target_hit
        )
        penalty = hft_penalty(
            profile.total_trades_30d,
            settings.copyability_hft_trades_30d_soft,
            settings.copyability_hft_trades_30d_hard,
        )
        score = combine_score(
            profile.score, edge, observations,
            settings.copyability_min_observations, penalty
        )
        copy_tier = tier(
            score, observations, settings.copyability_min_observations
        )

        row = await session.get(WalletCopyability, wallet)
        if row is None:
            row = WalletCopyability(wallet=wallet)
            session.add(row)

        row.updated_at = datetime.now(timezone.utc)
        row.buy_observations = observations
        row.avg_return_10s_pct = avg10
        row.avg_return_30s_pct = avg30
        row.avg_return_60s_pct = avg60
        row.avg_return_300s_pct = avg300
        row.positive_30s_rate_pct = positive30
        row.target_hit_rate_pct = target_hit
        row.median_target_horizon_seconds = median_lead
        row.hft_penalty = penalty
        row.observed_edge_score = edge
        row.copyability_score = score
        row.copyability_tier = copy_tier

        # We collect evidence first. Only after enough observations do we
        # automatically stop following a clearly uncopyable wallet.
        tracked = await session.get(TrackedWallet, wallet)
        if (
            tracked is not None
            and observations >= settings.copyability_min_observations
            and copy_tier == "AVOID"
        ):
            tracked.enabled = False

        await session.commit()


async def recalc_all(settings: Settings) -> None:
    async with SessionLocal() as session:
        wallets = list((
            await session.execute(select(SmartWalletProfile.wallet))
        ).scalars())
    for wallet in wallets[: max(settings.smart_wallet_max_tracked * 2, 100)]:
        await recalc_wallet(settings, wallet)


async def run_copyability(settings: Settings, stop: asyncio.Event) -> None:
    log.info(
        "Copyability engine active; horizons=%s target=+%.1f%%",
        settings.copyability_horizons,
        settings.copyability_target_return_pct,
    )
    while not stop.is_set():
        try:
            captured = await capture_due()
            await recalc_all(settings)
            if captured:
                log.info("Copyability captured %d post-buy measurement(s)", captured)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Copyability engine error: %s", exc)

        try:
            await asyncio.wait_for(
                stop.wait(), timeout=max(settings.copyability_refresh_seconds, 2)
            )
        except asyncio.TimeoutError:
            pass
