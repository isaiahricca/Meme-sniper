from dataclasses import dataclass
import math


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
    # Inspectable heuristic score. V0.7.4 deliberately avoids the old linear
    # saturation where ordinary setups could print 98-100 despite weak outcomes.

    m5 = float(x.price_change_m5_pct or 0.0)
    if m5 <= -10.0:
        momentum = 10.0
    elif m5 < 0.0:
        momentum = 30.0 + (m5 * 2.0)       # -10 -> 10, 0 -> 30
    elif m5 <= 12.0:
        momentum = 45.0 + (m5 * 3.5)       # constructive acceleration
    elif m5 <= 30.0:
        momentum = 87.0 - ((m5 - 12.0) * 1.0)  # avoid blindly chasing vertical candles
    else:
        momentum = max(35.0, 69.0 - ((m5 - 30.0) * 0.7))
    momentum = clamp(momentum)

    # Log liquidity score: deeper pools improve executable quality without
    # automatically turning every liquid token into a near-perfect score.
    liq = max(float(x.liquidity_usd or 0.0), 1.0)
    liquidity = clamp(35.0 + 30.0 * math.log10(liq / 25_000.0))

    trades = max(int(x.buys_m5 or 0), 0) + max(int(x.sells_m5 or 0), 0)
    if trades == 0:
        flow = 35.0
    else:
        buy_ratio = max(int(x.buys_m5 or 0), 0) / trades
        flow = clamp(50.0 + (buy_ratio - 0.5) * 100.0)

    smart_buys = max(int(x.smart_wallet_buys or 0), 0)
    smart_sells = max(int(x.smart_wallet_sells or 0), 0)
    smart = smart_buys + smart_sells
    if smart == 0:
        wallet = 45.0
    else:
        direction = clamp(50.0 + ((smart_buys - smart_sells) / smart) * 35.0)
        confidence = min(smart / 3.0, 1.0)
        wallet = 45.0 * (1.0 - confidence) + direction * confidence

    risk_penalty = clamp(x.concentration_risk) * 0.25

    weighted = (
        momentum * 0.30
        + liquidity * 0.20
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
