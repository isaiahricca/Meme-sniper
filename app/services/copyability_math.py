from app.services.smart_money import clamp
import math


def return_component(value: float | None) -> float:
    if value is None:
        return 50.0
    return clamp(50.0 + max(-100.0, min(200.0, value)) * 1.5)


def observed_edge_score(
    med10: float | None,
    med30: float | None,
    med60: float | None,
    med300: float | None,
    positive30: float | None,
    target_hit: float | None,
) -> float:
    components: list[tuple[float, float]] = []
    for value, weight in [(med10, .10), (med30, .25), (med60, .25), (med300, .15)]:
        if value is not None:
            components.append((return_component(value), weight))
    if positive30 is not None:
        components.append((clamp(positive30), .15))
    if target_hit is not None:
        components.append((clamp(target_hit), .10))
    if not components:
        return 50.0
    total = sum(w for _, w in components)
    return round(sum(v * w for v, w in components) / total, 2)


def combine_score(
    historical_score: float,
    live_edge: float,
    observations: int,
    minimum: int,
    penalty: float,
) -> float:
    historical_weight = .65 if observations < minimum else .30
    return round(clamp(historical_score * historical_weight + live_edge * (1 - historical_weight) - penalty), 2)


def classify_return(raw_return_pct: float, extreme_threshold_pct: float) -> tuple[bool, str]:
    if not math.isfinite(raw_return_pct):
        return False, "nonfinite_return"
    if abs(raw_return_pct) > extreme_threshold_pct:
        return False, "return_exceeds_integrity_threshold"
    return True, "valid"
