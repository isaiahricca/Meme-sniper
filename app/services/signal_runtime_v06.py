import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    Token, Signal, WalletSwapV06, TokenPairState, PairLatestPrice,
    SignalMeasurementV06, SignalPaperTradeV06, SmartMoneyClusterV07, SystemState,
)
from app.services.signal_engine import SignalInputs, score_signal
from app.services.paper import simulate_buy, simulate_sell
from app.services.rug_shield import risk_gate

log = logging.getLogger("signal_v06")


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _wallet_flow(session, mint: str) -> tuple[int, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
    rows = list((await session.execute(
        select(WalletSwapV06)
        .where(
            WalletSwapV06.token_mint == mint,
            WalletSwapV06.ts >= cutoff,
            WalletSwapV06.tracked_at_detection.is_(True),
        )
        .order_by(WalletSwapV06.ts.desc())
        .limit(200)
    )).scalars())
    buyers = {r.wallet for r in rows if r.action == "BUY" and r.copy_eligible}
    sellers = {r.wallet for r in rows if r.action == "SELL"}
    return len(buyers), len(sellers)


async def _fresh_pair(session, mint: str, settings: Settings):
    state = await session.get(TokenPairState, mint)
    if state is None or state.status != "active":
        return None
    latest = await session.get(PairLatestPrice, state.pair_address)
    if latest is None:
        return None
    snap = aware(latest.ts)
    now = datetime.now(timezone.utc)
    if (
        snap is None
        or latest.source != "dexscreener_exact_pair"
        or latest.base_mint != mint
        or latest.price_usd <= 0
        or (now - snap).total_seconds() > settings.market_max_price_age_seconds
    ):
        return None
    return state, latest


async def _record_signal_if_changed(session, token: Token, scored, settings: Settings) -> Signal | None:
    latest = (
        await session.execute(
            select(Signal)
            .where(Signal.token_mint == token.mint)
            .order_by(Signal.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    should = (
        latest is None
        or latest.decision != scored.decision
        or abs(latest.total_score - scored.total_score) >= 2.0
        or (now - (aware(latest.ts) or now)) >= timedelta(seconds=60)
    )
    if not should:
        return None

    sig = Signal(
        token_mint=token.mint,
        total_score=scored.total_score,
        momentum_score=scored.momentum_score,
        liquidity_score=scored.liquidity_score,
        flow_score=scored.flow_score,
        wallet_score=scored.wallet_score,
        risk_penalty=scored.risk_penalty,
        decision=scored.decision,
    )
    session.add(sig)
    await session.flush()

    pair = await _fresh_pair(session, token.mint, settings)
    if pair is not None:
        state, price = pair
        baseline_at = aware(price.ts)
        for horizon in settings.signal_horizons:
            session.add(SignalMeasurementV06(
                signal_id=sig.id,
                token_mint=token.mint,
                pair_address=state.pair_address,
                horizon_seconds=horizon,
                baseline_price=price.price_usd,
                due_at=baseline_at + timedelta(seconds=horizon),
            ))
    return sig


async def _active_count(session) -> int:
    return len(list((await session.execute(
        select(SignalPaperTradeV06.id)
        .where(SignalPaperTradeV06.status.in_(["pending", "open"]))
    )).scalars()))


async def _queue_trade(
    session, token: Token, signal: Signal | None, scored, settings: Settings,
    smart_wallet_buys: int = 0, cluster_score: float | None = None,
) -> tuple[bool, str, float]:
    # V0.7.4 PAPER challenge. The strict verified-copy lane is separate.
    effective_entry = max(float(settings.paper_entry_score), 60.0)
    cluster_bonus = 0.0 if cluster_score is None else max(
        0.0, min(12.0, (float(cluster_score) - 55.0) * 0.30)
    )
    wallet_bonus = min(max(int(smart_wallet_buys), 0), 4) * 2.5
    challenge_score = min(100.0, float(scored.total_score) + cluster_bonus + wallet_bonus)

    smart_confirmed = smart_wallet_buys >= 1 or (
        cluster_score is not None and float(cluster_score) >= 60.0
    )
    market_confirmed = (
        float(scored.flow_score) >= 60.0
        and float(scored.momentum_score) >= 52.0
        and float(scored.liquidity_score) >= 8.0
    )
    if signal is None:
        return False, "no_signal_record", challenge_score
    if not (smart_confirmed or market_confirmed):
        return False, "no_confirmation", challenge_score
    if challenge_score < effective_entry:
        return False, "score_below_entry", challenge_score

    now = datetime.now(timezone.utc)
    cooldown_start = now - timedelta(seconds=max(int(settings.paper_signal_reentry_cooldown_seconds), 0))
    recent = (
        await session.execute(
            select(SignalPaperTradeV06.id)
            .where(
                SignalPaperTradeV06.token_mint == token.mint,
                SignalPaperTradeV06.signal_at >= cooldown_start,
            )
            .order_by(SignalPaperTradeV06.signal_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if recent:
        return False, "token_cooldown", challenge_score
    if await _active_count(session) >= settings.paper_max_open_positions:
        return False, "max_open", challenge_score

    pair = await _fresh_pair(session, token.mint, settings)
    if pair is None:
        return False, "no_fresh_exact_pair", challenge_score
    state, latest = pair
    if latest.liquidity_usd is None or latest.liquidity_usd < settings.min_liquidity_usd:
        return False, "liquidity_below_min", challenge_score

    # Size by verified pair liquidity so a $1k headline position is only used
    # where the pool can plausibly absorb it. This is still paper-only.
    safe_notional = float(latest.liquidity_usd) * max(
        0.0001, float(settings.paper_position_liquidity_fraction)
    )
    if safe_notional < float(settings.paper_position_min_usd):
        return False, "liquidity_too_shallow_for_min_size", challenge_score
    notional = min(float(settings.paper_position_usd), safe_notional)

    # The entry clock starts when THIS cycle makes the decision, not when an old
    # signal row happened to be written. Reusing signal.ts caused silent expired
    # entry windows after restarts / unchanged scores.
    signal_at = now
    eligible = signal_at + timedelta(milliseconds=settings.paper_latency_ms)
    session.add(SignalPaperTradeV06(
        signal_id=signal.id,
        token_mint=token.mint,
        pair_address=state.pair_address,
        signal_at=signal_at,
        entry_eligible_at=eligible,
        entry_deadline_at=eligible + timedelta(seconds=settings.paper_signal_entry_deadline_seconds),
        status="pending",
        integrity_status="shadow_pending" if settings.paper_signal_shadow_mode else "pending",
        notional_usd=notional,
        fees_usd=0.0,
        entry_score=challenge_score,
        max_favourable_pct=0.0,
        max_adverse_pct=0.0,
    ))
    return True, "queued", challenge_score


async def evaluate_signals(settings: Settings) -> dict:
    stats = {
        "evaluated": 0,
        "market_eligible": 0,
        "recorded": 0,
        "queued": 0,
        "below_market_liquidity": 0,
        "no_confirmation": 0,
        "score_below_entry": 0,
        "token_cooldown": 0,
        "max_open": 0,
        "no_fresh_exact_pair": 0,
        "liquidity_below_min": 0,
        "liquidity_too_shallow_for_min_size": 0,
        "no_signal_record": 0,
    }
    async with SessionLocal() as session:
        tokens = list((await session.execute(
            select(Token).order_by(Token.discovered_at.desc()).limit(120)
        )).scalars())
        cluster_cutoff = datetime.now(timezone.utc) - timedelta(minutes=2)
        clusters = list((await session.execute(
            select(SmartMoneyClusterV07)
            .where(SmartMoneyClusterV07.last_seen_at >= cluster_cutoff)
            .order_by(SmartMoneyClusterV07.last_seen_at.desc())
            .limit(200)
        )).scalars())
        cluster_by_mint = {}
        for cluster in clusters:
            cluster_by_mint.setdefault(cluster.token_mint, cluster)

        for token in tokens:
            stats["evaluated"] += 1
            if (
                not token.price_usd
                or not token.liquidity_usd
                or token.liquidity_usd < settings.min_liquidity_usd
            ):
                stats["below_market_liquidity"] += 1
                continue
            stats["market_eligible"] += 1

            buys, sells = await _wallet_flow(session, token.mint)
            scored = score_signal(
                SignalInputs(
                    price_change_m5_pct=token.price_change_m5_pct or 0.0,
                    liquidity_usd=token.liquidity_usd or 0.0,
                    buys_m5=token.buys_m5 or 0,
                    sells_m5=token.sells_m5 or 0,
                    smart_wallet_buys=buys,
                    smart_wallet_sells=sells,
                ),
                entry_threshold=max(float(settings.paper_entry_score), 60.0),
            )
            sig = await _record_signal_if_changed(session, token, scored, settings)
            if sig is not None:
                stats["recorded"] += 1
            else:
                # Trading eligibility is evaluated every cycle. A signal not
                # changing by 2 points must not freeze the execution engine.
                sig = (
                    await session.execute(
                        select(Signal)
                        .where(Signal.token_mint == token.mint)
                        .order_by(Signal.ts.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()

            cluster = cluster_by_mint.get(token.mint)
            cluster_score = float(cluster.cluster_score) if cluster is not None else None
            queued, reason, _challenge_score = await _queue_trade(
                session, token, sig, scored, settings,
                smart_wallet_buys=buys, cluster_score=cluster_score,
            )
            if queued:
                stats["queued"] += 1
            elif reason in stats:
                stats[reason] += 1

        row = await session.get(SystemState, "v074_challenge_gate_stats")
        payload = json.dumps({
            **stats,
            "ts": datetime.now(timezone.utc).isoformat(),
        }, separators=(",", ":"))
        if row is None:
            session.add(SystemState(key="v074_challenge_gate_stats", value=payload))
        else:
            row.value = payload
        await session.commit()
    return stats

async def _enter_pending(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    entered = 0
    rejected = 0
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(SignalPaperTradeV06)
            .where(SignalPaperTradeV06.status == "pending")
            .order_by(SignalPaperTradeV06.entry_eligible_at.asc()).limit(100)
        )).scalars())
        for row in rows:
            eligible, deadline = aware(row.entry_eligible_at), aware(row.entry_deadline_at)
            if eligible is None or deadline is None:
                row.status = "invalid"; row.integrity_status = "invalid"; row.exit_reason = "missing_entry_timestamps"
                rejected += 1; continue
            if now < eligible:
                continue
            risk_ok, risk_reason, _risk = await risk_gate(session, row.token_mint, settings)
            if not risk_ok:
                if risk_reason in {"rug_shield_block","rug_shield_caution"} or now > deadline:
                    row.status="rejected"; row.integrity_status="rejected"; row.exit_reason=risk_reason; rejected += 1
                continue
            latest = await session.get(PairLatestPrice, row.pair_address)
            if latest is None:
                if now > deadline:
                    row.status = "rejected"; row.integrity_status = "rejected"; row.exit_reason = "no_exact_pair_entry_price"; rejected += 1
                continue
            snap = aware(latest.ts)
            valid = bool(
                snap is not None and eligible <= snap <= deadline
                and latest.source == "dexscreener_exact_pair"
                and latest.base_mint == row.token_mint
                and latest.price_usd > 0
                and latest.liquidity_usd is not None
                and latest.liquidity_usd >= settings.min_liquidity_usd
                and (now - snap).total_seconds() <= settings.market_max_price_age_seconds
            )
            if not valid:
                if now > deadline:
                    row.status = "rejected"; row.integrity_status = "rejected"; row.exit_reason = "no_timely_exact_pair_entry"; rejected += 1
                continue
            is_shadow = row.integrity_status == "shadow_pending"
            fill = simulate_buy(
                market_price=latest.price_usd,
                notional_usd=row.notional_usd,
                liquidity_usd=latest.liquidity_usd,
                fee_bps=settings.paper_fee_bps,
                base_slippage_bps=settings.paper_base_slippage_bps,
            )
            row.status = "open"; row.integrity_status = "shadow_open" if is_shadow else "verified_open"
            row.opened_at = snap
            row.exit_due_at = snap + timedelta(seconds=settings.paper_signal_max_hold_seconds)
            row.entry_market_price = fill.market_price; row.entry_fill_price = fill.fill_price
            row.qty = fill.qty; row.fees_usd = fill.fee_usd
            entered += 1
        await session.commit()
    return entered, rejected


async def _manage_open(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    closed = invalid = 0
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(SignalPaperTradeV06).where(SignalPaperTradeV06.status == "open")
            .order_by(SignalPaperTradeV06.opened_at.asc()).limit(100)
        )).scalars())
        for row in rows:
            exit_due = aware(row.exit_due_at)
            latest = await session.get(PairLatestPrice, row.pair_address)
            if latest is None:
                if exit_due and now > exit_due + timedelta(seconds=settings.paper_signal_exit_price_grace_seconds):
                    row.status = "invalid"; row.integrity_status = "invalid"; row.closed_at = now; row.exit_reason = "price_unavailable_after_max_hold"; invalid += 1
                continue
            snap = aware(latest.ts)
            if snap is None or latest.source != "dexscreener_exact_pair" or latest.base_mint != row.token_mint or not row.entry_fill_price or not row.qty:
                if exit_due and now > exit_due + timedelta(seconds=settings.paper_signal_exit_price_grace_seconds):
                    row.status = "invalid"; row.integrity_status = "invalid"; row.closed_at = now; row.exit_reason = "invalid_pair_state_after_max_hold"; invalid += 1
                continue
            if exit_due and snap > exit_due + timedelta(seconds=settings.paper_signal_exit_price_grace_seconds):
                row.status = "invalid"; row.integrity_status = "invalid"; row.closed_at = snap; row.exit_reason = "missed_exit_window_or_downtime"; invalid += 1; continue
            if (now - snap).total_seconds() > settings.market_max_price_age_seconds:
                continue
            mark = ((latest.price_usd / row.entry_fill_price) - 1.0) * 100.0
            if abs(mark) > settings.copyability_extreme_return_pct:
                row.status = "invalid"; row.integrity_status = "invalid"; row.closed_at = snap; row.exit_reason = "extreme_price_move_excluded_for_integrity"; invalid += 1; continue
            row.max_favourable_pct = max(row.max_favourable_pct or 0.0, mark)
            row.max_adverse_pct = min(row.max_adverse_pct or 0.0, mark)

            latest_sig = (await session.execute(
                select(Signal).where(Signal.token_mint == row.token_mint).order_by(Signal.ts.desc()).limit(1)
            )).scalar_one_or_none()
            reason = None
            _risk_ok, risk_reason, _risk = await risk_gate(session, row.token_mint, settings)
            trailing_active = (row.max_favourable_pct or 0.0) >= max(float(settings.paper_copy_trailing_activate_pct), 8.0)
            trailing_hit = trailing_active and mark <= (row.max_favourable_pct or 0.0) - max(float(settings.paper_copy_trailing_retrace_pct), 3.0)
            if risk_reason == "rug_shield_block": reason = "rug_shield_emergency"
            elif mark <= -settings.paper_stop_loss_pct: reason = "stop_loss"
            elif mark >= settings.paper_take_profit_pct: reason = "take_profit"
            elif trailing_hit: reason = "trailing_profit"
            elif latest_sig and latest_sig.total_score <= settings.paper_exit_score: reason = "signal_deterioration"
            elif exit_due and snap >= exit_due: reason = "max_hold"
            if not reason: continue
            if latest.liquidity_usd is None or latest.liquidity_usd <= 0:
                row.status = "invalid"; row.integrity_status = "invalid"; row.closed_at = snap; row.exit_reason = "exit_liquidity_unavailable"; invalid += 1; continue
            is_shadow = row.integrity_status == "shadow_open"
            fill = simulate_sell(
                market_price=latest.price_usd, qty=row.qty, liquidity_usd=latest.liquidity_usd,
                fee_bps=settings.paper_fee_bps, base_slippage_bps=settings.paper_base_slippage_bps,
            )
            net = fill.notional_usd - fill.fee_usd
            pnl = net - row.notional_usd
            row.status = "closed"; row.integrity_status = "shadow_closed" if is_shadow else "verified_closed"; row.closed_at = snap; row.exit_reason = reason
            row.exit_market_price = fill.market_price; row.exit_fill_price = fill.fill_price; row.fees_usd += fill.fee_usd
            row.pnl_usd = pnl; row.pnl_pct = pnl / row.notional_usd * 100.0
            closed += 1
        await session.commit()
    return closed, invalid


async def _capture_measurements(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    captured = invalid = 0
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(SignalMeasurementV06).where(SignalMeasurementV06.captured_at.is_(None))
            .order_by(SignalMeasurementV06.due_at.asc()).limit(500)
        )).scalars())
        for row in rows:
            due = aware(row.due_at)
            if due is None or due > now: continue
            latest = await session.get(PairLatestPrice, row.pair_address)
            if latest is None:
                if now > due + timedelta(seconds=settings.copyability_capture_grace_seconds):
                    row.captured_at = now; row.integrity_status = "invalid"; row.invalid_reason = "exact_pair_price_unavailable"; invalid += 1
                continue
            snap = aware(latest.ts)
            if snap is None or snap < due:
                if now > due + timedelta(seconds=settings.copyability_capture_grace_seconds):
                    row.captured_at = now; row.integrity_status = "invalid"; row.invalid_reason = "no_post_due_snapshot"; invalid += 1
                continue
            lag = (snap - due).total_seconds()
            row.captured_at = snap; row.capture_lag_seconds = lag; row.observed_price = latest.price_usd
            if latest.source != "dexscreener_exact_pair": row.integrity_status = "invalid"; row.invalid_reason = "not_exact_pair_refresh"; invalid += 1; continue
            if latest.base_mint != row.token_mint: row.integrity_status = "invalid"; row.invalid_reason = "pair_base_mismatch"; invalid += 1; continue
            if lag > settings.copyability_max_capture_lag_seconds: row.integrity_status = "invalid"; row.invalid_reason = "late_snapshot"; invalid += 1; continue
            if row.baseline_price <= 0 or latest.price_usd <= 0: row.integrity_status = "invalid"; row.invalid_reason = "nonpositive_price"; invalid += 1; continue
            raw = ((latest.price_usd / row.baseline_price) - 1.0) * 100.0
            row.raw_return_pct = raw
            if abs(raw) > settings.copyability_extreme_return_pct:
                row.integrity_status = "extreme_excluded"; row.invalid_reason = "return_exceeds_integrity_threshold"; row.valid_for_score = False; invalid += 1
            else:
                row.integrity_status = "valid"; row.valid_for_score = True; captured += 1
        await session.commit()
    return captured, invalid


async def run_signal_runtime_v06(settings: Settings, stop: asyncio.Event) -> None:
    log.info(
        "Signal runtime active; mode=%s position=$%.0f entry>=%.1f max_open=%d TP=+%.1f%% SL=-%.1f%%",
        "AGGRESSIVE PAPER CHALLENGE" if settings.paper_signal_shadow_mode else "VERIFIED PAPER",
        settings.paper_position_usd,
        max(float(settings.paper_entry_score), 60.0),
        settings.paper_max_open_positions,
        settings.paper_take_profit_pct,
        settings.paper_stop_loss_pct,
    )
    while not stop.is_set():
        try:
            gate = await evaluate_signals(settings)
            entered, rejected = await _enter_pending(settings)
            closed, bad_trades = await _manage_open(settings)
            captures, bad_measurements = await _capture_measurements(settings)
            if any([gate["recorded"], gate["queued"], entered, rejected, closed, bad_trades, captures, bad_measurements]):
                log.info(
                    "Signal cycle signals=%d queued=%d enter=%d reject=%d close=%d invalid=%d measured=%d measure_invalid=%d gates=%s",
                    gate["recorded"], gate["queued"], entered, rejected, closed, bad_trades,
                    captures, bad_measurements,
                    {k: v for k, v in gate.items() if v and k not in {"recorded", "queued"}},
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Verified signal runtime error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(settings.signal_refresh_seconds, 1.0))
        except asyncio.TimeoutError:
            pass
