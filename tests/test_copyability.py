import pytest
pytest.importorskip("aiosqlite")
from app.services.copyability import (
    hft_penalty, observed_edge_score, combine_score, tier
)

def test_hft_penalty():
    assert hft_penalty(5000, 10000, 100000) == 0
    assert hft_penalty(100000, 10000, 100000) == 40

def test_good_live_edge():
    edge = observed_edge_score(3, 8, 15, 18, 75, 60)
    score = combine_score(75, edge, 8, 3, 0)
    assert score > 70

def test_hft_wallet_can_be_uncopyable():
    score = combine_score(95, 70, 10, 3, 40)
    assert score < 60
    assert tier(score, 10, 3) == "AVOID"

def test_unproven():
    assert tier(90, 1, 3) == "UNPROVEN"
