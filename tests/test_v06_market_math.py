from app.services.market_math import choose_pair

MINT="MINT"

def pair(addr, base, liq, price="1"):
    return {"chainId":"solana","pairAddress":addr,"baseToken":{"address":base},"quoteToken":{"address":"Q"},"priceUsd":price,"liquidity":{"usd":liq}}


def test_choose_pair_requires_target_as_base_and_liquidity():
    pairs=[pair("wrong","OTHER",999999),pair("low",MINT,500),pair("good",MINT,50000),pair("better",MINT,80000)]
    assert choose_pair(MINT,pairs,1000)["pairAddress"] == "better"


def test_choose_pair_fails_closed_without_valid_pair():
    assert choose_pair(MINT,[pair("wrong","OTHER",999999)],1000) is None
