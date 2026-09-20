from datetime import datetime, timezone

from app.config import Settings
from app.services.performance import _range_start


def test_v073_conservative_defaults():
    s = Settings(_env_file=None)
    assert s.app_name.endswith("V0.7.3")
    assert s.paper_signal_shadow_mode is True
    assert s.paper_copy_min_profile_score >= 75
    assert s.paper_copy_min_trader_score >= 75
    assert s.paper_copy_min_copy_score >= 65
    assert s.paper_copy_min_observations >= 20
    assert s.paper_copy_daily_loss_limit_usd > 0
    assert s.live_trading_enabled is False


def test_forward_range_never_predates_verified_epoch():
    epoch = datetime(2026, 9, 15, tzinfo=timezone.utc)
    forward = datetime(2026, 9, 20, tzinfo=timezone.utc)
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert _range_start("forward", epoch, now, forward) == forward
    assert _range_start("forward", forward, now, epoch) == forward


def test_cloud_auth_is_opt_in_locally():
    s = Settings(_env_file=None)
    assert s.dashboard_auth_enabled is False
    assert s.dashboard_password == ""
