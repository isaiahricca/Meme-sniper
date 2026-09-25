import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    SystemState, WalletSwapV06, PaperCopyTradeV06,
    SmartWalletProfile, WalletCopyabilityV06, PairLatestPrice, TraderIntelligenceV07,
)
from app.services.rug_shield import risk_gate
from app.services.paper import simulate_buy, simulate_sell
from app.services.forward_test import copy_daily_circuit_open

log = logging.getLogger("paper_copy_v06")
EPOCH_KEY = "v06_verified_epoch"


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _epoch(session) -> datetime | None:
    row = await session.get(SystemState, EPOCH_KEY)
    if row is None:
        return None
    dt = datetime.fromisoformat(row.value)
    return aware(dt)


async def _active_count(session) -> int:
    return len(list((await session.execute(
        select(PaperCopyTradeV06.id).where(PaperCopyTradeV06.status.in_(["pending", "open"]))
    )).scalars()))


async def _seed_new(settings: Settings) -> int:
    now = datetime.now(timezone.utc)
    created = 0
    async with SessionLocal() as session:
        epoch = await _epoch(session)
        if epoch is None:
            return 0

        swaps = list((await session.execute(
            select(WalletSwapV06)
            .where(
                WalletSwapV06.action == "BUY",
                WalletSwapV06.copy_eligible.is_(True),
                WalletSwapV06.tracked_at_detection.is_(True),
                WalletSwapV06.ts >= epoch,
                WalletSwapV06.pair_address.is_not(None),
                ~select(PaperCopyTradeV06.id).where(
                    PaperCopyTradeV06.swap_id == WalletSwapV06.id
                ).exists(),
            )
            .order_by(WalletSwapV06.ts.asc())
            .limit(300)
        )).scalars())

        for swap in swaps:
            existing = (
                await session.execute(
                    select(PaperCopyTradeV06.id)
                    .where(PaperCopyTradeV06.swap_id == swap.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing:
                continue

            profile = await session.get(SmartWalletProfile, swap.wallet)
            copy = await session.get(WalletCopyabilityV06, swap.wallet)
            trader = await session.get(TraderIntelligenceV07, swap.wallet)
            reject_reason = None
            # V0.7.3 hard floors deliberately ignore weaker values left in an old .env.
            min_profile = max(float(settings.paper_copy_min_profile_score), 75.0)
            min_trader = max(float(settings.paper_copy_min_trader_score), 75.0)
            min_copy = max(float(settings.paper_copy_min_copy_score), 65.0)
            min_obs = max(int(settings.copyability_min_observations), int(settings.paper_copy_min_observations), 20)
            min_pos30 = max(float(settings.paper_copy_min_positive_30s_rate_pct), 55.0)
            min_med60 = max(float(settings.paper_copy_min_median_60s_pct), 5.0)
            min_med300 = max(float(settings.paper_copy_min_median_300s_pct), 7.0)

            if profile is None:
                reject_reason = "no_wallet_profile"
            elif (profile.score or 0.0) < min_profile:
                reject_reason = "profile_score_below_min"
            elif copy is not None and copy.copyability_tier == "AVOID":
                reject_reason = "copyability_avoid"
            elif settings.paper_copy_require_qualified_trader:
                sources=set((profile.source or "").split("+"))
                nansen_backed="nansen" in sources
                verified_copy=bool(copy and copy.eligible_observations >= min_obs and (copy.copyability_score or 0) >= min_copy and copy.copyability_tier not in {"AVOID","UNPROVEN"})
                if trader is None or (trader.combined_score or 0) < min_trader:
                    reject_reason = "trader_intelligence_below_min"
                elif copy is None or copy.eligible_observations < min_obs or copy.copyability_tier == "UNPROVEN":
                    reject_reason = "copyability_unproven"
                elif (copy.copyability_score or 0) < min_copy:
                    reject_reason = "copyability_score_below_min"
                elif copy.positive_30s_rate_pct is None or copy.positive_30s_rate_pct < min_pos30:
                    reject_reason = "positive_30s_rate_below_min"
                elif copy.median_return_60s_pct is None or copy.median_return_60s_pct < min_med60:
                    reject_reason = "median_60s_edge_below_cost_buffer"
                elif copy.median_return_300s_pct is None or copy.median_return_300s_pct < min_med300:
                    reject_reason = "median_300s_edge_below_cost_buffer"
                elif settings.paper_copy_require_nansen_or_verified and not (nansen_backed or verified_copy):
                    reject_reason = "not_nansen_or_verified_copyable"
            elif copy is not None and copy.copyability_tier == "UNPROVEN" and not settings.paper_copy_allow_unproven:
                reject_reason = "copyability_unproven"

            detected = aware(swap.ts) or now
            row = PaperCopyTradeV06(
                swap_id=swap.id,
                wallet=swap.wallet,
                token_mint=swap.token_mint,
                pair_address=swap.pair_address,
                detected_at=detected,
                entry_eligible_at=detected + timedelta(seconds=settings.paper_copy_entry_delay_seconds),
                entry_deadline_at=detected + timedelta(seconds=settings.paper_copy_entry_deadline_seconds),
                status="rejected" if reject_reason else "pending",
                integrity_status="rejected" if reject_reason else "pending",
                reject_reason=reject_reason,
                wallet_profile_score=profile.score if profile else None,
                wallet_copy_score=copy.copyability_score if copy else None,
                wallet_copy_tier=copy.copyability_tier if copy else "UNPROVEN",
                notional_usd=settings.paper_copy_position_usd,
            )
            session.add(row)
            created += 1
        await session.commit()
    return created


async def _enter_due(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    entered = 0
    rejected = 0
    circuit_open, day_pnl, _day_start = await copy_daily_circuit_open(settings)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(PaperCopyTradeV06)
            .where(PaperCopyTradeV06.status == "pending")
            .order_by(PaperCopyTradeV06.entry_eligible_at.asc())
            .limit(100)
        )).scalars())

        for row in rows:
            eligible = aware(row.entry_eligible_at)
            deadline = aware(row.entry_deadline_at)
            if eligible is None or deadline is None:
                row.status = "invalid"
                row.integrity_status = "invalid"
                row.reject_reason = "missing_entry_timestamps"
                rejected += 1
                continue
            if now < eligible:
                continue
            if circuit_open:
                row.status = "rejected"
                row.integrity_status = "rejected"
                row.reject_reason = f"daily_loss_circuit_breaker:{day_pnl:.2f}"
                rejected += 1
                continue
            risk_ok, risk_reason, _risk = await risk_gate(session, row.token_mint, settings)
            if not risk_ok:
                if risk_reason in {"rug_shield_block","rug_shield_caution"}:
                    row.status = "rejected"; row.integrity_status = "rejected"; row.reject_reason = risk_reason; rejected += 1
                elif now > deadline:
                    row.status = "rejected"; row.integrity_status = "rejected"; row.reject_reason = risk_reason; rejected += 1
                continue
            open_ids = (await session.execute(select(PaperCopyTradeV06.id).where(
                PaperCopyTradeV06.status == "open"
            ))).scalars().all()
            if len(open_ids) >= settings.paper_copy_max_open_trades:
                row.status = "rejected"
                row.integrity_status = "rejected"
                row.reject_reason = "max_open_trades"
                rejected += 1
                continue

            latest = await session.get(PairLatestPrice, row.pair_address)
            if latest is None:
                if now > deadline:
                    row.status = "rejected"
                    row.integrity_status = "rejected"
                    row.reject_reason = "no_exact_pair_entry_price"
                    rejected += 1
                continue

            snap_ts = aware(latest.ts)
            valid = bool(
                snap_ts is not None
                and eligible <= snap_ts <= min(deadline, now)
                and latest.source == "dexscreener_exact_pair"
                and latest.base_mint == row.token_mint
                and latest.price_usd > 0
                and latest.liquidity_usd is not None
                and latest.liquidity_usd >= max(settings.paper_copy_min_liquidity_usd, 50_000.0)
                and (now - snap_ts).total_seconds() <= settings.market_max_price_age_seconds
            )
            if not valid:
                if now > deadline:
                    row.status = "rejected"
                    row.integrity_status = "rejected"
                    if snap_ts is None or snap_ts < eligible:
                        row.reject_reason = "no_post_delay_exact_pair_snapshot"
                    elif snap_ts > deadline:
                        row.reject_reason = "entry_snapshot_arrived_too_late"
                    elif latest.source != "dexscreener_exact_pair":
                        row.reject_reason = "entry_snapshot_not_exact_pair"
                    elif latest.base_mint != row.token_mint:
                        row.reject_reason = "entry_pair_base_mismatch"
                    else:
                        row.reject_reason = "entry_liquidity_below_min"
                    rejected += 1
                continue

            # Cost-aware edge gate. The wallet's measured 5-minute median has to
            # clear the exact current fee/slippage estimate plus a safety margin.
            copy_now = await session.get(WalletCopyabilityV06, row.wallet)
            flat_buy = simulate_buy(
                market_price=latest.price_usd,
                notional_usd=row.notional_usd,
                liquidity_usd=latest.liquidity_usd,
                fee_bps=settings.paper_copy_fee_bps,
                base_slippage_bps=settings.paper_copy_slippage_bps,
            )
            flat_sell = simulate_sell(
                market_price=latest.price_usd,
                qty=flat_buy.qty,
                liquidity_usd=latest.liquidity_usd,
                fee_bps=settings.paper_copy_fee_bps,
                base_slippage_bps=settings.paper_copy_slippage_bps,
            )
            flat_net = flat_sell.notional_usd - flat_sell.fee_usd
            friction_pct = max(0.0, (row.notional_usd - flat_net) / row.notional_usd * 100.0)
            expected_gross = float(copy_now.median_return_300s_pct or 0.0) if copy_now else 0.0
            required_gross = friction_pct + max(float(settings.paper_copy_min_net_edge_pct), 2.0)
            if expected_gross < required_gross:
                row.status = "rejected"
                row.integrity_status = "rejected"
                row.reject_reason = f"expected_edge_below_cost:{expected_gross:.2f}<{required_gross:.2f}"
                rejected += 1
                continue

            fill = flat_buy
            row.status = "open"
            row.integrity_status = "verified_open"
            row.entered_at = snap_ts
            row.exit_due_at = snap_ts + timedelta(seconds=settings.paper_copy_max_hold_seconds)
            row.entry_market_price = fill.market_price
            row.entry_fill_price = fill.fill_price
            row.qty = fill.qty
            row.fees_usd = fill.fee_usd
            row.entry_slippage_bps = fill.slippage_bps
            row.liquidity_at_entry_usd = latest.liquidity_usd
            row.detection_to_entry_seconds = (snap_ts - aware(row.detected_at)).total_seconds()
            row.max_favourable_pct = 0.0
            row.max_adverse_pct = 0.0
            entered += 1

        await session.commit()
    return entered, rejected


async def _manage_open(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    closed = 0
    invalid = 0
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(PaperCopyTradeV06)
            .where(PaperCopyTradeV06.status == "open")
            .order_by(PaperCopyTradeV06.entered_at.asc())
            .limit(200)
        )).scalars())

        for row in rows:
            exit_due = aware(row.exit_due_at)
            latest = await session.get(PairLatestPrice, row.pair_address)
            if latest is None:
                if exit_due and now > exit_due + timedelta(seconds=settings.paper_copy_exit_price_grace_seconds):
                    row.status = "invalid"
                    row.integrity_status = "invalid"
                    row.exited_at = now
                    row.exit_reason = "exact_pair_price_unavailable_after_max_hold"
                    invalid += 1
                continue

            snap_ts = aware(latest.ts)
            if (
                snap_ts is None
                or latest.source != "dexscreener_exact_pair"
                or latest.base_mint != row.token_mint
                or not row.entry_fill_price
                or not row.qty
            ):
                if exit_due and now > exit_due + timedelta(seconds=settings.paper_copy_exit_price_grace_seconds):
                    row.status = "invalid"
                    row.integrity_status = "invalid"
                    row.exited_at = now
                    row.exit_reason = "invalid_exact_pair_state_after_max_hold"
                    invalid += 1
                continue

            if exit_due and snap_ts > exit_due + timedelta(seconds=settings.paper_copy_exit_price_grace_seconds):
                row.status = "invalid"
                row.integrity_status = "invalid"
                row.exited_at = snap_ts
                row.exit_reason = "missed_exit_window_or_downtime"
                invalid += 1
                continue
            if not 0 <= (now - snap_ts).total_seconds() <= settings.market_max_price_age_seconds:
                if exit_due and now > exit_due + timedelta(seconds=settings.paper_copy_exit_price_grace_seconds):
                    row.status = "invalid"
                    row.integrity_status = "invalid"
                    row.exited_at = now
                    row.exit_reason = "stale_exact_pair_price_after_max_hold"
                    invalid += 1
                continue

            mark = ((latest.price_usd / row.entry_fill_price) - 1.0) * 100.0
            if abs(mark) > settings.copyability_extreme_return_pct:
                row.status = "invalid"
                row.integrity_status = "invalid"
                row.exited_at = snap_ts
                row.exit_reason = "extreme_price_move_excluded_for_integrity"
                invalid += 1
                continue
            row.max_favourable_pct = max(row.max_favourable_pct or 0.0, mark)
            row.max_adverse_pct = min(row.max_adverse_pct or 0.0, mark)

            reason = None
            risk_ok, risk_reason, _risk = await risk_gate(session, row.token_mint, settings)
            entry_liq = float(row.liquidity_at_entry_usd or 0)
            liq_drop = ((entry_liq - float(latest.liquidity_usd or 0)) / entry_liq * 100.0) if entry_liq > 0 and latest.liquidity_usd is not None else 0.0
            source_sold = False
            if settings.paper_copy_exit_on_source_wallet_sell and row.entered_at is not None:
                source_sold = (await session.execute(
                    select(WalletSwapV06.id).where(
                        WalletSwapV06.wallet == row.wallet,
                        WalletSwapV06.token_mint == row.token_mint,
                        WalletSwapV06.action == "SELL",
                        WalletSwapV06.ts >= row.entered_at,
                        WalletSwapV06.ts <= snap_ts,
                    ).limit(1)
                )).scalar_one_or_none() is not None
            effective_tp = min(float(settings.paper_copy_take_profit_pct), 15.0)
            effective_sl = min(float(settings.paper_copy_stop_loss_pct), 8.0)
            trailing_active = (row.max_favourable_pct or 0.0) >= max(float(settings.paper_copy_trailing_activate_pct), 8.0)
            trailing_hit = trailing_active and mark <= (row.max_favourable_pct or 0.0) - max(float(settings.paper_copy_trailing_retrace_pct), 3.0)
            if risk_reason == "rug_shield_block" or liq_drop >= settings.rug_shield_emergency_liquidity_drop_pct:
                reason = "rug_shield_emergency"
            elif source_sold:
                reason = "source_wallet_sell"
            elif mark >= effective_tp:
                reason = "take_profit"
            elif trailing_hit:
                reason = "trailing_profit"
            elif mark <= -effective_sl:
                reason = "stop_loss"
            elif exit_due and snap_ts >= exit_due:
                reason = "max_hold"
            if not reason:
                continue

            liquidity = latest.liquidity_usd
            if liquidity is None or liquidity <= 0:
                row.status = "invalid"
                row.integrity_status = "invalid"
                row.exited_at = snap_ts
                row.exit_reason = "exit_liquidity_unavailable"
                invalid += 1
                continue

            sell = simulate_sell(
                market_price=latest.price_usd,
                qty=row.qty,
                liquidity_usd=liquidity,
                fee_bps=settings.paper_copy_fee_bps,
                base_slippage_bps=settings.paper_copy_slippage_bps,
            )
            net = sell.notional_usd - sell.fee_usd
            pnl = net - row.notional_usd
            row.status = "closed"
            row.integrity_status = "verified_closed"
            row.exited_at = snap_ts
            row.exit_reason = reason
            row.exit_market_price = sell.market_price
            row.exit_fill_price = sell.fill_price
            row.exit_slippage_bps = sell.slippage_bps
            row.fees_usd += sell.fee_usd
            row.pnl_usd = pnl
            row.pnl_pct = pnl / row.notional_usd * 100.0
            closed += 1

        await session.commit()
    return closed, invalid


async def run_paper_copy_v06(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.paper_copy_enabled:
        log.info("Verified paper-copy disabled")
        return
    log.info(
        "Verified paper-copy active; CONSERVATIVE $%.2f/trade delay=%.1fs TP=+%.1f%% SL=-%.1f%% daily_limit=$%.2f",
        settings.paper_copy_position_usd,
        settings.paper_copy_entry_delay_seconds,
        min(settings.paper_copy_take_profit_pct, 15.0),
        min(settings.paper_copy_stop_loss_pct, 8.0),
        settings.paper_copy_daily_loss_limit_usd,
    )
    while not stop.is_set():
        try:
            seeded = await _seed_new(settings)
            entered, rejected = await _enter_due(settings)
            closed, invalid = await _manage_open(settings)
            if seeded or entered or rejected or closed or invalid:
                log.info(
                    "Verified copy cycle seed=%d enter=%d reject=%d close=%d invalid=%d",
                    seeded, entered, rejected, closed, invalid,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Verified paper-copy error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(settings.paper_copy_refresh_seconds, 1.0))
        except asyncio.TimeoutError:
            pass
