from dataclasses import dataclass


@dataclass(frozen=True)
class SignalInputs:
    price_change_m5_pct: float
    liquidity_usd: float
    buys_m5: int
    sells_m5: int
    smart_wallet_buys: int = 0
    smart_wallet_sells: int = 0
    concentration_risk: float = 0.0  # 0..100; placeholder until holder analyzer lands


@dataclass(frozen=True)
class SignalResult:
    total_score: float
    momentum_score: float
    liquidity_score: float
    flow_score: float
    wallet_score: float
    risk_penalty: float
    decision: str


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_signal(x: SignalInputs, entry_threshold: float = 82.0) -> SignalResult:
    # Deliberately simple and inspectable V0.1 scoring.
    # It is NOT presented as predictive ML.
    momentum = clamp(50 + x.price_change_m5_pct * 1.4)

    # Full liquidity score at >= $250k; near-zero under tiny pools.
    liquidity = clamp((x.liquidity_usd / 250_000.0) * 100.0)

    trades = x.buys_m5 + x.sells_m5
    if trades == 0:
        flow = 50.0
    else:
        buy_ratio = x.buys_m5 / trades
        flow = clamp(buy_ratio * 100.0)

    smart = x.smart_wallet_buys + x.smart_wallet_sells
    if smart == 0:
        wallet = 50.0
    else:
        wallet = clamp((x.smart_wallet_buys / smart) * 100.0)

    risk_penalty = clamp(x.concentration_risk) * 0.25

    weighted = (
        momentum * 0.25
        + liquidity * 0.25
        + flow * 0.25
        + wallet * 0.25
        - risk_penalty
    )
    total = clamp(weighted)
    decision = "PAPER_LONG" if total >= entry_threshold else "WATCH"

    return SignalResult(
        total_score=round(total, 2),
        momentum_score=round(momentum, 2),
        liquidity_score=round(liquidity, 2),
        flow_score=round(flow, 2),
        wallet_score=round(wallet, 2),
        risk_penalty=round(risk_penalty, 2),
        decision=decision,
    )
