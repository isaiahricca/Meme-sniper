from types import SimpleNamespace
import time

from app.main import _choose_token_pair, _token_origin, _age_seconds


def test_explorer_prefers_pinned_pair():
    mint = "MINT"
    pairs = [
        {"chainId": "solana", "pairAddress": "A", "baseToken": {"address": mint}, "liquidity": {"usd": 500}},
        {"chainId": "solana", "pairAddress": "B", "baseToken": {"address": mint}, "liquidity": {"usd": 5000}},
    ]
    assert _choose_token_pair(mint, pairs, "A")["pairAddress"] == "A"


def test_explorer_falls_back_to_most_liquid_pair():
    mint = "MINT"
    pairs = [
        {"chainId": "solana", "pairAddress": "A", "baseToken": {"address": mint}, "liquidity": {"usd": 500}},
        {"chainId": "solana", "pairAddress": "B", "baseToken": {"address": mint}, "liquidity": {"usd": 5000}},
    ]
    assert _choose_token_pair(mint, pairs, None)["pairAddress"] == "B"


def test_origin_detects_pump():
    token = SimpleNamespace(source="pumpportal", mint="abc")
    assert _token_origin(token, None) == "Pump.fun"


def test_age_nonnegative():
    assert 0 <= _age_seconds(int((time.time() - 60) * 1000)) <= 120
