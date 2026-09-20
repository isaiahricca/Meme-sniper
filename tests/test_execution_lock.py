import pytest

from app.services.execution_lock import LiveTradingLocked, assert_live_trading_allowed


def test_live_trading_is_hard_locked():
    with pytest.raises(LiveTradingLocked) as exc:
        assert_live_trading_allowed()
    assert "V0.6" in str(exc.value)
    assert "Only paper trading is implemented" in str(exc.value)
