from app.services.copyability_math import classify_return, observed_edge_score, combine_score
from app.services.smart_money import hft_penalty, copyability_tier


def test_extreme_return_is_excluded_not_clamped_into_score():
    assert classify_return(347695, 1000) == (False, "return_exceeds_integrity_threshold")
    assert classify_return(42, 1000) == (True, "valid")


def test_hft_wallet_gets_max_penalty():
    assert hft_penalty(915388, 10000, 100000) == 40


def test_good_forward_edge_can_graduate_after_enough_observations():
    edge=observed_edge_score(2,5,9,14,70,55)
    score=combine_score(78,edge,12,10,0)
    assert score >= 60
    assert copyability_tier(score,12,10) in {"A","B","C"}


def test_unproven_before_ten_observations():
    assert copyability_tier(95,9,10) == "UNPROVEN"
