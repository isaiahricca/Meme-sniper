
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import Token, Signal, PaperTrade, WalletTrade, SignalMeasurement
from app.services.signal_engine import SignalInputs, score_signal
from app.services.paper import simulate_buy, simulate_sell

log = logging.getLogger("paper")
HORIZONS = (10, 30, 60, 300)


async def _smart_wallet_counts(session, mint: str) -> tuple[int, int]:
    rows = list((
        await session.execute(
            select(WalletTrade).where(WalletTrade.token_mint == mint)
        )
    ).scalars())
    buys = sum(1 for x in rows[-50:] if x.side == "BUY")
    sells = sum(1 for x in rows[-50:] if x.side == "SELL")
    return buys, sells


async def _open_count(session) -> int:
    result = await session.execute(
        select(PaperTrade).where(PaperTrade.status == "open")
    )
    return len(list(result.scalars()))


async def _record_signal_if_changed(session, token: Token, scored) -> Signal | None:
    latest = (
        await session.execute(
            select(Signal)
            .where(Signal.token_mint == token.mint)
            .order_by(Signal.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)
    should_record = (
        latest is None
        or latest.decision != scored.decision
        or abs(latest.total_score - scored.total_score) >= 2.0
        or (now - latest.ts.replace(tzinfo=timezone.utc) if latest.ts.tzinfo is None else now - latest.ts)
            >= timedelta(seconds=60)
    )
    if not should_record:
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

    if token.price_usd and token.price_usd > 0:
        for horizon in HORIZONS:
            session.add(SignalMeasurement(
                signal_id=sig.id,
                token_mint=token.mint,
                horizon_seconds=horizon,
                baseline_price=token.price_usd,
                due_at=now + timedelta(seconds=horizon),
            ))
    return sig


async def evaluate_once(settings: Settings) -> None:
    async with SessionLocal() as session:
        result = await session.execute(
            select(Token).order_by(Token.discovered_at.desc()).limit(50)
        )
        tokens = list(result.scalars())

        for token in tokens:
            if not token.price_usd or not token.liquidity_usd:
                continue
            if token.liquidity_usd < settings.min_liquidity_usd:
                continue

            wb, ws = await _smart_wallet_counts(session, token.mint)
            scored = score_signal(
                SignalInputs(
                    price_change_m5_pct=token.price_change_m5_pct or 0.0,
                    liquidity_usd=token.liquidity_usd or 0.0,
                    buys_m5=token.buys_m5 or 0,
                    sells_m5=token.sells_m5 or 0,
                    smart_wallet_buys=wb,
                    smart_wallet_sells=ws,
                ),
                entry_threshold=settings.paper_entry_score,
            )

            await _record_signal_if_changed(session, token, scored)

            open_trade = (
                await session.execute(
                    select(PaperTrade).where(
                        PaperTrade.token_mint == token.mint,
                        PaperTrade.status == "open",
                    )
                )
            ).scalar_one_or_none()

            if open_trade is None:
                if (
                    scored.total_score >= settings.paper_entry_score
                    and await _open_count(session) < settings.paper_max_open_positions
                ):
                    fill = simulate_buy(
                        market_price=token.price_usd,
                        notional_usd=settings.paper_position_usd,
                        liquidity_usd=token.liquidity_usd,
                        fee_bps=settings.paper_fee_bps,
                        base_slippage_bps=settings.paper_base_slippage_bps,
                    )
                    session.add(PaperTrade(
                        token_mint=token.mint,
                        notional_usd=settings.paper_position_usd,
                        entry_market_price=fill.market_price,
                        entry_fill_price=fill.fill_price,
                        qty=fill.qty,
                        fees_usd=fill.fee_usd,
                        entry_score=scored.total_score,
                    ))
            else:
                mark_return = ((token.price_usd / open_trade.entry_fill_price) - 1.0) * 100.0
                exit_reason = None
                if mark_return <= -settings.paper_stop_loss_pct:
                    exit_reason = "stop_loss"
                elif mark_return >= settings.paper_take_profit_pct:
                    exit_reason = "take_profit"
                elif scored.total_score <= settings.paper_exit_score:
                    exit_reason = "signal_deterioration"

                if exit_reason:
                    sell = simulate_sell(
                        market_price=token.price_usd,
                        qty=open_trade.qty,
                        liquidity_usd=token.liquidity_usd,
                        fee_bps=settings.paper_fee_bps,
                        base_slippage_bps=settings.paper_base_slippage_bps,
                    )
                    net_proceeds = sell.notional_usd - sell.fee_usd
                    pnl = net_proceeds - open_trade.notional_usd
                    open_trade.status = "closed"
                    open_trade.closed_at = datetime.now(timezone.utc)
                    open_trade.exit_market_price = sell.market_price
                    open_trade.exit_fill_price = sell.fill_price
                    open_trade.fees_usd += sell.fee_usd
                    open_trade.pnl_usd = pnl
                    open_trade.pnl_pct = (pnl / open_trade.notional_usd) * 100.0
                    open_trade.exit_reason = exit_reason

        await session.commit()


async def run_paper_engine(settings: Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await evaluate_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Paper engine error: %s", exc)

        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
