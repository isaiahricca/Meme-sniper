def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def hft_penalty(trades_30d: int | None, soft: int, hard: int) -> float:
    if trades_30d is None or trades_30d <= soft:
        return 0.0
    if hard <= soft:
        return 40.0
    ratio = (trades_30d - soft) / (hard - soft)
    return round(clamp(ratio, 0.0, 1.0) * 40.0, 2)


def copyability_tier(score: float, observations: int, minimum: int) -> str:
    if observations < minimum:
        return "UNPROVEN"
    if score >= 85:
        return "A"
    if score >= 72:
        return "B"
    if score >= 60:
        return "C"
    return "AVOID"
