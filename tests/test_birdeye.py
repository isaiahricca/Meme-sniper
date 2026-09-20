import pytest
pytest.importorskip("aiosqlite")
from app.services.birdeye import parse_top_traders, parse_wallet_summary, score_wallet, tier_for


def test_top_trader_parser():
    payload = {
        "success": True,
        "data": {
            "items": [{
                "owner": "WalletABC",
                "realizedPnl": 1200,
                "totalPnl": 1500,
                "volumeUsd": 8000,
                "trade": 12,
                "tags": ["sniper"],
            }]
        }
    }
    rows = parse_top_traders(payload)
    assert rows[0]["owner"] == "WalletABC"
    assert rows[0]["realized_pnl"] == 1200
    assert rows[0]["trade"] == 12


def test_wallet_summary_parser():
    payload = {
        "data": {
            "counts": {"total_trade": 80, "win_rate": 0.625},
            "pnl": {"realized_profit_usd": 4200, "total_usd": 5100},
        }
    }
    x = parse_wallet_summary(payload)
    assert x["win_rate_pct"] == 62.5
    assert x["total_trades"] == 80
    assert x["realized_pnl_usd"] == 4200


def test_good_wallet_beats_bad_tagged_wallet():
    good = score_wallet(
        token_realized_pnl=1000, token_total_pnl=1500, token_volume_usd=50000,
        token_trade_count=20, tags=[], win_rate_pct=68,
        wallet_realized_pnl=10000, wallet_total_pnl=12000,
        wallet_trade_count=200, discovery_count=3,
    )
    bad = score_wallet(
        token_realized_pnl=-100, token_total_pnl=-500, token_volume_usd=10000,
        token_trade_count=5, tags=["dev", "bundler"], win_rate_pct=35,
        wallet_realized_pnl=-3000, wallet_total_pnl=-5000,
        wallet_trade_count=20, discovery_count=1,
    )
    assert good > bad
    assert tier_for(good) in {"STRONG", "ELITE"}


def test_wallet_summary_nested_summary_shape():
    payload = {
        "success": True,
        "data": {
            "summary": {
                "counts": {"total_trade": 144, "win_rate": 0.6736},
                "pnl": {"realized_profit_usd": 12840.5, "total_usd": 15310.25},
            }
        },
    }
    x = parse_wallet_summary(payload)
    assert x["win_rate_pct"] == 67.36
    assert x["total_trades"] == 144
    assert x["realized_pnl_usd"] == 12840.5
    assert x["_recognized"] is True
