from app.services.signal_engine import SignalInputs, score_signal


def test_strong_setup_scores_above_weak_setup():
    strong = score_signal(SignalInputs(
        price_change_m5_pct=20,
        liquidity_usd=300_000,
        buys_m5=90,
        sells_m5=10,
        smart_wallet_buys=8,
        smart_wallet_sells=1,
    ))
    weak = score_signal(SignalInputs(
        price_change_m5_pct=-15,
        liquidity_usd=5_000,
        buys_m5=10,
        sells_m5=90,
        smart_wallet_buys=1,
        smart_wallet_sells=8,
    ))
    assert strong.total_score > weak.total_score


def test_concentration_penalty_reduces_score():
    base = SignalInputs(
        price_change_m5_pct=10,
        liquidity_usd=200_000,
        buys_m5=70,
        sells_m5=30,
        smart_wallet_buys=7,
        smart_wallet_sells=3,
        concentration_risk=0,
    )
    risky = SignalInputs(**{**base.__dict__, "concentration_risk": 90})
    assert score_signal(risky).total_score < score_signal(base).total_score
