import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    WalletTrade, Token, TrackedWallet, SmartWalletProfile,
    WalletCopyability, PaperCopyTrade
)
from app.services.paper import simulate_buy, simulate_sell

log = logging.getLogger("paper_copy")


def _aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _open_count(session) -> int:
    rows = list((
        await session.execute(
            select(PaperCopyTrade.id)
            .where(PaperCopyTrade.status == "open")
        )
    ).scalars())
    return len(rows)


async def _seed_new_wallet_buys(settings: Settings) -> int:
    created = 0
    now = datetime.now(timezone.utc)

    async with SessionLocal() as session:
        buys = list((
            await session.execute(
                select(WalletTrade)
                .where(
                    WalletTrade.side == "BUY",
                    WalletTrade.observed_price_usd.is_not(None),
                    WalletTrade.observed_price_usd > 0,
                )
                .order_by(WalletTrade.ts.desc())
                .limit(500)
            )
        ).scalars())

        for trade in buys:
            existing = (
                await session.execute(
                    select(PaperCopyTrade.id)
                    .where(PaperCopyTrade.wallet_trade_id == trade.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing:
                continue

            tracked = await session.get(TrackedWallet, trade.wallet)
            profile = await session.get(SmartWalletProfile, trade.wallet)
            copy = await session.get(WalletCopyability, trade.wallet)
            token = await session.get(Token, trade.token_mint)

            reject_reason = None
            if not tracked or not tracked.enabled:
                reject_reason = "wallet_not_tracked"
            elif profile is None:
                reject_reason = "no_wallet_profile"
            elif profile.score < settings.paper_copy_min_profile_score:
                reject_reason = "profile_score_below_min"
            elif copy is not None and copy.copyability_tier == "AVOID":
                reject_reason = "copyability_avoid"
            elif (
                copy is not None
                and copy.copyability_tier == "UNPROVEN"
                and not settings.paper_copy_allow_unproven
            ):
                reject_reason = "copyability_unproven"
            elif token is None or not token.price_usd or token.price_usd <= 0:
                reject_reason = "no_market_price"
            elif not token.liquidity_usd or token.liquidity_usd < settings.paper_copy_min_liquidity_usd:
                reject_reason = "liquidity_below_min"

            status = "rejected" if reject_reason else "pending"
            session.add(PaperCopyTrade(
                wallet_trade_id=trade.id,
                wallet=trade.wallet,
                token_mint=trade.token_mint,
                detected_at=_aware(trade.ts) or now,
                entry_due_at=now + timedelta(seconds=settings.paper_copy_entry_delay_seconds),
                status=status,
                reject_reason=reject_reason,
                wallet_profile_score=profile.score if profile else None,
                wallet_copy_score=copy.copyability_score if copy else None,
                wallet_copy_tier=copy.copyability_tier if copy else "UNPROVEN",
                detected_price_usd=trade.observed_price_usd,
                notional_usd=settings.paper_copy_position_usd,
            ))
            created += 1

        await session.commit()
    return created


async def _enter_due(settings: Settings) -> int:
    now = datetime.now(timezone.utc)
    entered = 0

    async with SessionLocal() as session:
        rows = list((
            await session.execute(
                select(PaperCopyTrade)
                .where(
                    PaperCopyTrade.status == "pending",
                    PaperCopyTrade.entry_due_at <= now,
                )
                .order_by(PaperCopyTrade.entry_due_at.asc())
                .limit(100)
            )
        ).scalars())

        for row in rows:
            if await _open_count(session) >= settings.paper_copy_max_open_trades:
                row.status = "rejected"
                row.reject_reason = "max_open_trades"
                continue

            token = await session.get(Token, row.token_mint)
            if token is None or not token.price_usd or token.price_usd <= 0:
                row.status = "rejected"
                row.reject_reason = "no_entry_price"
                continue
            if not token.liquidity_usd or token.liquidity_usd < settings.paper_copy_min_liquidity_usd:
                row.status = "rejected"
                row.reject_reason = "entry_liquidity_below_min"
                continue

            fill = simulate_buy(
                market_price=token.price_usd,
                notional_usd=row.notional_usd,
                liquidity_usd=token.liquidity_usd,
                fee_bps=settings.paper_copy_fee_bps,
                base_slippage_bps=settings.paper_copy_slippage_bps,
            )

            row.status = "open"
            row.entered_at = now
            row.exit_due_at = now + timedelta(seconds=settings.paper_copy_max_hold_seconds)
            row.entry_market_price = fill.market_price
            row.entry_fill_price = fill.fill_price
            row.qty = fill.qty
            row.fees_usd = fill.fee_usd
            row.entry_slippage_bps = fill.slippage_bps
            row.liquidity_at_entry_usd = token.liquidity_usd
            detected = _aware(row.detected_at)
            row.detection_to_entry_seconds = (
                (now - detected).total_seconds() if detected else None
            )
            row.max_favourable_pct = 0.0
            row.max_adverse_pct = 0.0
            entered += 1

        await session.commit()
    return entered


async def _manage_open(settings: Settings) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    checked = 0
    closed = 0

    async with SessionLocal() as session:
        rows = list((
            await session.execute(
                select(PaperCopyTrade)
                .where(PaperCopyTrade.status == "open")
                .order_by(PaperCopyTrade.entered_at.asc())
                .limit(200)
            )
        ).scalars())

        for row in rows:
            checked += 1
            token = await session.get(Token, row.token_mint)
            if token is None or not token.price_usd or token.price_usd <= 0:
                continue
            if not row.entry_fill_price or not row.qty:
                continue

            mark_pct = ((token.price_usd / row.entry_fill_price) - 1.0) * 100.0
            row.max_favourable_pct = max(row.max_favourable_pct or 0.0, mark_pct)
            row.max_adverse_pct = min(row.max_adverse_pct or 0.0, mark_pct)

            reason = None
            if mark_pct >= settings.paper_copy_take_profit_pct:
                reason = "take_profit"
            elif mark_pct <= -settings.paper_copy_stop_loss_pct:
                reason = "stop_loss"
            elif row.exit_due_at and _aware(row.exit_due_at) <= now:
                reason = "max_hold"

            if not reason:
                continue

            liquidity = token.liquidity_usd or row.liquidity_at_entry_usd or 1.0
            sell = simulate_sell(
                market_price=token.price_usd,
                qty=row.qty,
                liquidity_usd=liquidity,
                fee_bps=settings.paper_copy_fee_bps,
                base_slippage_bps=settings.paper_copy_slippage_bps,
            )

            net = sell.notional_usd - sell.fee_usd
            pnl = net - row.notional_usd

            row.status = "closed"
            row.exited_at = now
            row.exit_reason = reason
            row.exit_market_price = sell.market_price
            row.exit_fill_price = sell.fill_price
            row.exit_slippage_bps = sell.slippage_bps
            row.fees_usd += sell.fee_usd
            row.pnl_usd = pnl
            row.pnl_pct = (pnl / row.notional_usd) * 100.0
            closed += 1

        await session.commit()
    return checked, closed


async def run_paper_copy(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.paper_copy_enabled:
        log.info("Paper-copy engine disabled")
        return

    log.info(
        "Paper-copy engine active; $%.2f/trade delay=%ss TP=+%.1f%% SL=-%.1f%% max=%ss",
        settings.paper_copy_position_usd,
        settings.paper_copy_entry_delay_seconds,
        settings.paper_copy_take_profit_pct,
        settings.paper_copy_stop_loss_pct,
        settings.paper_copy_max_hold_seconds,
    )

    while not stop.is_set():
        try:
            seeded = await _seed_new_wallet_buys(settings)
            entered = await _enter_due(settings)
            checked, closed = await _manage_open(settings)
            if seeded or entered or closed:
                log.info(
                    "Paper-copy cycle: seeded=%d entered=%d closed=%d open_checked=%d",
                    seeded, entered, closed, checked
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Paper-copy engine error: %s", exc)

        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=max(settings.paper_copy_refresh_seconds, 1),
            )
        except asyncio.TimeoutError:
            pass
