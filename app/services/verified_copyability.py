import asyncio
import logging
import statistics
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    WalletSwapV06, WalletSwapMeasurementV06, WalletCopyabilityV06,
    TokenPairState, PairLatestPrice, SmartWalletProfile,
)
from app.services.smart_money import hft_penalty, copyability_tier
from app.services.copyability_math import observed_edge_score, combine_score, classify_return
from app.services.measurement_prices import first_observation

log = logging.getLogger("verified_copyability")


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None



async def _initialize_pending_swaps(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    initialized = 0
    invalidated = 0
    async with SessionLocal() as session:
        swaps = list((await session.execute(
            select(WalletSwapV06)
            .where(
                WalletSwapV06.action == "BUY",
                WalletSwapV06.copy_eligible.is_(True),
                WalletSwapV06.integrity_status == "pending",
            )
            .order_by(WalletSwapV06.ts.asc())
            .limit(200)
        )).scalars())

        for swap in swaps:
            swap_ts = aware(swap.ts)
            if swap_ts is None:
                swap.integrity_status = "invalid"
                swap.invalid_reason = "missing_detection_time"
                invalidated += 1
                continue

            deadline = swap_ts + timedelta(seconds=settings.copyability_baseline_max_wait_seconds)
            state = await session.get(TokenPairState, swap.token_mint)
            latest = await session.get(PairLatestPrice, state.pair_address) if state and state.status == "active" else None

            usable = False
            if latest is not None:
                snap_ts = aware(latest.ts)
                usable = bool(
                    snap_ts is not None
                    and swap_ts <= snap_ts <= min(deadline, now)
                    and latest.source == "dexscreener_exact_pair"
                    and latest.base_mint == swap.token_mint
                    and latest.price_usd > 0
                    and latest.liquidity_usd is not None
                    and latest.liquidity_usd >= settings.copyability_min_baseline_liquidity_usd
                    and (now - snap_ts).total_seconds() <= settings.market_max_price_age_seconds
                )

            if not usable:
                if now > deadline:
                    swap.integrity_status = "invalid"
                    if state is None or state.status != "active":
                        swap.invalid_reason = "no_pinned_pair_before_deadline"
                    elif latest is None:
                        swap.invalid_reason = "no_exact_pair_baseline_before_deadline"
                    elif latest.source != "dexscreener_exact_pair":
                        swap.invalid_reason = "baseline_not_exact_pair_refresh"
                    elif latest.base_mint != swap.token_mint:
                        swap.invalid_reason = "baseline_pair_base_mismatch"
                    elif (latest.liquidity_usd or 0) < settings.copyability_min_baseline_liquidity_usd:
                        swap.invalid_reason = "baseline_liquidity_below_min"
                    else:
                        swap.invalid_reason = "no_timely_post_detection_baseline"
                    invalidated += 1
                continue

            existing = (
                await session.execute(
                    select(WalletSwapMeasurementV06.id)
                    .where(WalletSwapMeasurementV06.swap_id == swap.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing:
                swap.integrity_status = "measuring"
                continue

            snap_ts = aware(latest.ts)
            swap.pair_address = state.pair_address
            swap.baseline_price_usd = latest.price_usd
            swap.baseline_liquidity_usd = latest.liquidity_usd
            swap.baseline_at = snap_ts
            swap.baseline_delay_seconds = (snap_ts - swap_ts).total_seconds()
            swap.integrity_status = "measuring"

            for horizon in settings.copyability_horizons:
                session.add(WalletSwapMeasurementV06(
                    swap_id=swap.id,
                    wallet=swap.wallet,
                    token_mint=swap.token_mint,
                    pair_address=state.pair_address,
                    horizon_seconds=horizon,
                    baseline_price=latest.price_usd,
                    due_at=snap_ts + timedelta(seconds=horizon),
                ))
            initialized += 1

        await session.commit()
    return initialized, invalidated


async def _capture_due(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    captured = 0
    excluded = 0
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(WalletSwapMeasurementV06)
            .where(WalletSwapMeasurementV06.captured_at.is_(None))
            .order_by(WalletSwapMeasurementV06.due_at.asc())
            .limit(500)
        )).scalars())

        touched_swaps = set()
        for row in rows:
            due = aware(row.due_at)
            if due is None or due > now:
                continue
            latest = await first_observation(session, row.pair_address, due, now)
            if latest is None:
                if now > due + timedelta(seconds=settings.copyability_capture_grace_seconds):
                    row.captured_at = now
                    row.integrity_status = "invalid"
                    row.invalid_reason = "exact_pair_price_unavailable"
                    excluded += 1
                    touched_swaps.add(row.swap_id)
                continue

            snap_ts = aware(latest.ts)
            if snap_ts is None or snap_ts < due or snap_ts > now:
                if now > due + timedelta(seconds=settings.copyability_capture_grace_seconds):
                    row.captured_at = now
                    row.integrity_status = "invalid"
                    row.invalid_reason = "no_post_due_snapshot"
                    excluded += 1
                    touched_swaps.add(row.swap_id)
                continue

            lag = (snap_ts - due).total_seconds()
            row.captured_at = snap_ts
            row.capture_lag_seconds = lag
            row.observed_price = latest.price_usd
            row.observed_liquidity_usd = latest.liquidity_usd
            touched_swaps.add(row.swap_id)

            if latest.source != "dexscreener_exact_pair":
                row.integrity_status = "invalid"
                row.invalid_reason = "measurement_not_exact_pair_refresh"
                excluded += 1
                continue
            if latest.base_mint != row.token_mint:
                row.integrity_status = "invalid"
                row.invalid_reason = "pair_base_mismatch"
                excluded += 1
                continue
            if lag > settings.copyability_max_capture_lag_seconds:
                row.integrity_status = "invalid"
                row.invalid_reason = "late_snapshot"
                excluded += 1
                continue
            if row.baseline_price <= 0 or latest.price_usd <= 0:
                row.integrity_status = "invalid"
                row.invalid_reason = "nonpositive_price"
                excluded += 1
                continue

            raw = ((latest.price_usd / row.baseline_price) - 1.0) * 100.0
            row.raw_return_pct = raw
            ok, reason = classify_return(raw, settings.copyability_extreme_return_pct)
            if not ok:
                row.integrity_status = "extreme_excluded"
                row.invalid_reason = reason
                row.valid_for_score = False
                excluded += 1
            else:
                row.integrity_status = "valid"
                row.valid_for_score = True
                captured += 1

        # Mark swaps complete once every scheduled measurement has resolved.
        for swap_id in touched_swaps:
            pending = (
                await session.execute(
                    select(WalletSwapMeasurementV06.id)
                    .where(
                        WalletSwapMeasurementV06.swap_id == swap_id,
                        WalletSwapMeasurementV06.captured_at.is_(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pending is None:
                swap = await session.get(WalletSwapV06, swap_id)
                if swap and swap.integrity_status == "measuring":
                    swap.integrity_status = "complete"

        await session.commit()
    return captured, excluded


async def _recalculate_wallet(settings: Settings, wallet: str) -> None:
    async with SessionLocal() as session:
        profile = await session.get(SmartWalletProfile, wallet)
        if profile is None:
            return

        rows = list((await session.execute(
            select(WalletSwapMeasurementV06)
            .where(WalletSwapMeasurementV06.wallet == wallet)
            .order_by(WalletSwapMeasurementV06.id.desc())
            .limit(4000)
        )).scalars())

        valid = [x for x in rows if x.valid_for_score and x.raw_return_pct is not None]
        excluded_count = len([x for x in rows if x.captured_at is not None and not x.valid_for_score])
        by_horizon: dict[int, list[float]] = {}
        by_swap: dict[int, list[WalletSwapMeasurementV06]] = {}
        for row in valid:
            by_horizon.setdefault(row.horizon_seconds, []).append(row.raw_return_pct)
            by_swap.setdefault(row.swap_id, []).append(row)

        # One independent BUY episode counts once. Require a valid 30s observation
        # before calling the episode an eligible copyability observation.
        eligible_swaps = {
            swap_id: samples for swap_id, samples in by_swap.items()
            if any(x.horizon_seconds == 30 for x in samples)
        }
        obs = len(eligible_swaps)

        med10 = _median(by_horizon.get(10, []))
        med30 = _median(by_horizon.get(30, []))
        med60 = _median(by_horizon.get(60, []))
        med300 = _median(by_horizon.get(300, []))

        r30 = by_horizon.get(30, [])
        positive30 = (sum(1 for x in r30 if x > 0) / len(r30) * 100.0) if r30 else None

        hits = 0
        lead_horizons: list[int] = []
        for samples in eligible_swaps.values():
            target_horizons = sorted(
                x.horizon_seconds for x in samples
                if x.raw_return_pct is not None and x.raw_return_pct >= settings.copyability_target_return_pct
            )
            if target_horizons:
                hits += 1
                lead_horizons.append(target_horizons[0])
        target_hit = (hits / obs * 100.0) if obs else None
        median_lead = statistics.median(lead_horizons) if lead_horizons else None

        edge = observed_edge_score(med10, med30, med60, med300, positive30, target_hit)
        penalty = hft_penalty(
            profile.total_trades_30d,
            settings.copyability_hft_trades_30d_soft,
            settings.copyability_hft_trades_30d_hard,
        )
        score = combine_score(profile.score or 0.0, edge, obs, settings.copyability_min_observations, penalty)
        tier = copyability_tier(score, obs, settings.copyability_min_observations)

        result = await session.get(WalletCopyabilityV06, wallet)
        if result is None:
            result = WalletCopyabilityV06(wallet=wallet)
            session.add(result)
        result.updated_at = datetime.now(timezone.utc)
        result.eligible_observations = obs
        result.excluded_measurements = excluded_count
        result.median_return_10s_pct = med10
        result.median_return_30s_pct = med30
        result.median_return_60s_pct = med60
        result.median_return_300s_pct = med300
        result.positive_30s_rate_pct = positive30
        result.target_hit_rate_pct = target_hit
        result.median_target_horizon_seconds = median_lead
        result.hft_penalty = penalty
        result.observed_edge_score = edge
        result.copyability_score = score
        result.copyability_tier = tier
        await session.commit()


async def _recalculate_all(settings: Settings) -> None:
    async with SessionLocal() as session:
        wallets = list((await session.execute(select(SmartWalletProfile.wallet))).scalars())
    for wallet in wallets[: max(settings.smart_wallet_max_tracked * 2, 100)]:
        await _recalculate_wallet(settings, wallet)


async def run_verified_copyability(settings: Settings, stop: asyncio.Event) -> None:
    log.info(
        "Verified copyability active; horizons=%s min_obs=%d extreme_guard=%s%%",
        settings.copyability_horizons,
        settings.copyability_min_observations,
        settings.copyability_extreme_return_pct,
    )
    while not stop.is_set():
        try:
            initialized, invalid = await _initialize_pending_swaps(settings)
            captured, excluded = await _capture_due(settings)
            await _recalculate_all(settings)
            if initialized or invalid or captured or excluded:
                log.info(
                    "Copyability cycle baseline=%d baseline_invalid=%d captured=%d excluded=%d",
                    initialized, invalid, captured, excluded,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Verified copyability error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(settings.copyability_refresh_seconds, 1.0))
        except asyncio.TimeoutError:
            pass
