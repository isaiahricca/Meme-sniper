from pathlib import Path
from app.services.execution_lock import assert_live_trading_allowed, LiveTradingLocked
import pytest


def test_live_execution_still_hard_locked():
    with pytest.raises(LiveTradingLocked):
        assert_live_trading_allowed()


def test_verified_services_do_not_contain_signing_seed_phrase_fields():
    root=Path(__file__).parents[1]/'app'
    text='\n'.join(p.read_text(encoding='utf-8') for p in root.rglob('*.py'))
    forbidden=['SEED_PHRASE=', 'PRIVATE_KEY=', 'sendTransaction(']
    for marker in forbidden:
        assert marker not in text
