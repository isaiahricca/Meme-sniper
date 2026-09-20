from app.services.paper import simulate_buy, simulate_sell, estimate_slippage_bps


def test_small_trade_has_less_impact_than_large_trade():
    small = estimate_slippage_bps(
        notional_usd=25,
        liquidity_usd=100_000,
        base_slippage_bps=50,
    )
    large = estimate_slippage_bps(
        notional_usd=5_000,
        liquidity_usd=100_000,
        base_slippage_bps=50,
    )
    assert small < large


def test_round_trip_loses_money_when_market_flat():
    buy = simulate_buy(
        market_price=1.0,
        notional_usd=100,
        liquidity_usd=100_000,
        fee_bps=100,
        base_slippage_bps=50,
    )
    sell = simulate_sell(
        market_price=1.0,
        qty=buy.qty,
        liquidity_usd=100_000,
        fee_bps=100,
        base_slippage_bps=50,
    )
    proceeds = sell.notional_usd - sell.fee_usd
    assert proceeds < 100
