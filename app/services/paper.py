from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    market_price: float
    fill_price: float
    qty: float
    notional_usd: float
    fee_usd: float
    slippage_bps: float


def estimate_slippage_bps(
    *,
    notional_usd: float,
    liquidity_usd: float,
    base_slippage_bps: float,
) -> float:
    if liquidity_usd <= 0:
        return 10_000.0
    # Conservative placeholder impact curve.
    impact_bps = (notional_usd / liquidity_usd) * 100_000.0
    return max(0.0, base_slippage_bps + impact_bps)


def simulate_buy(
    *,
    market_price: float,
    notional_usd: float,
    liquidity_usd: float,
    fee_bps: float,
    base_slippage_bps: float,
) -> Fill:
    if market_price <= 0 or notional_usd <= 0:
        raise ValueError("market_price and notional_usd must be positive")

    slippage = estimate_slippage_bps(
        notional_usd=notional_usd,
        liquidity_usd=liquidity_usd,
        base_slippage_bps=base_slippage_bps,
    )
    fill_price = market_price * (1 + slippage / 10_000.0)
    fee = notional_usd * fee_bps / 10_000.0
    spend_after_fee = max(0.0, notional_usd - fee)
    qty = spend_after_fee / fill_price

    return Fill(market_price, fill_price, qty, notional_usd, fee, slippage)


def simulate_sell(
    *,
    market_price: float,
    qty: float,
    liquidity_usd: float,
    fee_bps: float,
    base_slippage_bps: float,
) -> Fill:
    if market_price <= 0 or qty <= 0:
        raise ValueError("market_price and qty must be positive")

    gross = market_price * qty
    slippage = estimate_slippage_bps(
        notional_usd=gross,
        liquidity_usd=liquidity_usd,
        base_slippage_bps=base_slippage_bps,
    )
    fill_price = market_price * max(0.0, 1 - slippage / 10_000.0)
    gross_fill = fill_price * qty
    fee = gross_fill * fee_bps / 10_000.0

    return Fill(market_price, fill_price, qty, gross_fill, fee, slippage)
