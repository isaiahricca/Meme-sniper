from app.services.nansen import candidate_prior, event_key
from app.services.trader_intelligence import combined_score, cluster_score, tier_for

def test_nansen_event_key_stable():
    x={"transaction_hash":"abc","trader_address":"w","token_bought_address":"t","token_sold_address":"s"}
    assert event_key(x)==event_key(dict(x))

def test_prior_bounded():
    assert 72 <= candidate_prior(1000) <= 84
    assert candidate_prior(10**12) <= 84

def test_copyability_can_dominate():
    assert combined_score(70,75,90,20,70,10) > combined_score(70,75,40,20,45,10)

def test_cluster_rewards_consensus():
    assert cluster_score(4,80,20000) > cluster_score(2,80,20000)

def test_tiers():
    assert tier_for(90)=="ELITE"
    assert tier_for(75)=="STRONG"
    assert tier_for(65)=="TESTING"
    assert tier_for(50)=="AVOID"
