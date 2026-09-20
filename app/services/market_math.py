def as_float(value, default=0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def pair_base(pair: dict) -> str | None:
    return (pair.get("baseToken") or {}).get("address")


def pair_quote(pair: dict) -> str | None:
    return (pair.get("quoteToken") or {}).get("address")


def pair_price(pair: dict) -> float:
    return as_float(pair.get("priceUsd"), 0.0)


def pair_liquidity(pair: dict) -> float:
    return as_float((pair.get("liquidity") or {}).get("usd"), 0.0)


def choose_pair(mint: str, pairs: list[dict], min_liquidity_usd: float) -> dict | None:
    candidates = []
    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue
        if pair_base(pair) != mint:
            continue
        if not pair.get("pairAddress") or pair_price(pair) <= 0:
            continue
        if pair_liquidity(pair) < min_liquidity_usd:
            continue
        candidates.append(pair)
    return max(candidates, key=pair_liquidity) if candidates else None
