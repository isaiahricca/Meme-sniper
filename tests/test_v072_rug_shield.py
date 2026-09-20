from app.config import Settings
from app.services.rug_shield import analyse_goplus, classify


def test_safe_token_passes():
    s=Settings(_env_file=None)
    data={"mintable":{"status":"0"},"freezable":{"status":"0"},"closable":{"status":"0"},"non_transferable":"0","balance_mutable_authority":{"status":"0"},"holders":[{"percent":"0.05"},{"percent":"0.04"}]}
    r=analyse_goplus(data,s)
    assert classify(r["score"],r["hard"],s)=="PASS"


def test_malicious_creator_hard_blocks():
    s=Settings(_env_file=None)
    data={"creator":[{"address":"x","malicious_address":"1"}]}
    r=analyse_goplus(data,s)
    assert r["hard"] is True
    assert classify(r["score"],r["hard"],s)=="BLOCK"


def test_concentrated_unlocked_holders_block():
    s=Settings(_env_file=None)
    data={"holders":[{"percent":"0.50"},{"percent":"0.25"}]}
    r=analyse_goplus(data,s)
    assert r["top10_unlocked_pct"]==75.0
    assert classify(r["score"],r["hard"],s)=="BLOCK"


def test_burn_holder_not_counted_as_concentration():
    s=Settings(_env_file=None)
    data={"holders":[{"percent":"0.80","tag":"Burn Address"},{"percent":"0.05"}]}
    r=analyse_goplus(data,s)
    assert r["top10_unlocked_pct"]==5.0
