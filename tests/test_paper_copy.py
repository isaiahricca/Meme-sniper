from app.services.paper import simulate_buy, simulate_sell

def test_paper_copy_flat_market_loses_after_costs():
    buy = simulate_buy(
        market_price=1.0,
        notional_usd=25,
        liquidity_usd=100000,
        fee_bps=100,
        base_slippage_bps=75,
    )
    sell = simulate_sell(
        market_price=1.0,
        qty=buy.qty,
        liquidity_usd=100000,
        fee_bps=100,
        base_slippage_bps=75,
    )
    assert (sell.notional_usd - sell.fee_usd) < 25
