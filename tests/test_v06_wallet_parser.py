from datetime import datetime, timezone, timedelta

from app.services.wallet_parser import parse_wallet_swap, WSOL, same_buy_episode

WALLET = "Wallet111111111111111111111111111111111111"
TOKEN = "Token1111111111111111111111111111111111111"
TOKEN2 = "Token2222222222222222222222222222222222222"


def bal(mint, amount):
    return {"owner": WALLET, "mint": mint, "uiTokenAmount": {"uiAmountString": str(amount)}}


def tx(pre_tokens, post_tokens, pre_sol=10.0, post_sol=10.0, fee_sol=0.0):
    return {"result": {"meta": {
        "err": None, "preTokenBalances": pre_tokens, "postTokenBalances": post_tokens,
        "preBalances": [int(pre_sol*1e9)], "postBalances": [int(post_sol*1e9)], "fee": int(fee_sol*1e9),
    }, "transaction": {"message": {"accountKeys": [{"pubkey": WALLET}]}}}}


def test_clean_native_sol_buy_adjusts_fee():
    p = tx([bal(TOKEN, 0)], [bal(TOKEN, 1000)], 10, 8.999995, .000005)
    swap, reason = parse_wallet_swap(p, WALLET)
    assert reason is None
    assert swap.action == "BUY"
    assert swap.token_mint == TOKEN
    assert swap.quote_mint == WSOL
    assert round(swap.quote_delta, 6) == -1.0


def test_fee_only_transfer_is_not_fake_buy():
    p = tx([bal(TOKEN, 0)], [bal(TOKEN, 1000)], 10, 9.999995, .000005)
    swap, reason = parse_wallet_swap(p, WALLET)
    assert swap is None
    assert reason == "not_directional_swap"


def test_clean_sell():
    p = tx([bal(TOKEN, 1000)], [bal(TOKEN, 0)], 10, 11.999995, .000005)
    swap, reason = parse_wallet_swap(p, WALLET)
    assert reason is None and swap.action == "SELL"
    assert round(swap.quote_delta, 6) == 2.0


def test_token_rotation_fails_closed():
    p = tx([bal(TOKEN, 100), bal(TOKEN2, 0)], [bal(TOKEN, 0), bal(TOKEN2, 50)])
    swap, reason = parse_wallet_swap(p, WALLET)
    assert swap is None and reason == "token_rotation_excluded"


def test_scale_in_episode_window():
    now = datetime.now(timezone.utc)
    assert same_buy_episode(now, now + timedelta(seconds=299), 300)
    assert not same_buy_episode(now, now + timedelta(seconds=300), 300)
