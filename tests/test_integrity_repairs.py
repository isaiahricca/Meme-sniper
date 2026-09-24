import asyncio
import json
from datetime import datetime, timezone, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import Settings
from app.models import (Base, PairLatestPrice, PairPriceObservation, SignalMeasurementV06,
                        WalletSwapV06, PaperCopyTradeV06, SystemState, TokenPairState,
                        PriceCandleV074, WalletCopyabilityV06, Event)
from app.services import market_data, signal_runtime_v06, paper_copy_v06
from app.services import candle_sampler, maintenance
from app.services import helius, copyability_math
from app.services.ai_ensemble import _normalize, _ask_claude
from app.services.paper import simulate_buy, simulate_sell
from app.services.supervisor import Supervisor
from app.services.execution_lock import LiveTradingLocked


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    for module in (market_data, signal_runtime_v06, paper_copy_v06, candle_sampler, maintenance, helius):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    yield sessions
    await engine.dispose()


def pair(price=1.0, address="pair", mint="mint"):
    return {"chainId": "solana", "pairAddress": address, "baseToken": {"address": mint},
            "priceUsd": str(price), "liquidity": {"usd": 100000}}


@pytest.mark.asyncio
async def test_measurement_uses_first_timely_price_after_latest_is_overwritten(db):
    now = datetime.now(timezone.utc)
    due = now - timedelta(seconds=30)
    async with db() as session:
        session.add(SignalMeasurementV06(signal_id=1, token_mint="mint", pair_address="pair",
                    horizon_seconds=30, baseline_price=1, due_at=due))
        await market_data._upsert_exact_snapshot(session, pair(1.02), due + timedelta(seconds=2), source="dexscreener_exact_pair")
        await session.commit()
        await market_data._upsert_exact_snapshot(session, pair(1.5), now - timedelta(seconds=1), source="dexscreener_exact_pair")
        await session.commit()
    assert await signal_runtime_v06._capture_measurements(Settings(_env_file=None)) == (1, 0)
    async with db() as session:
        row = (await session.execute(select(SignalMeasurementV06))).scalar_one()
        assert row.observed_price == 1.02
        assert row.capture_lag_seconds == 2
        assert row.raw_return_pct == pytest.approx(2)


@pytest.mark.asyncio
async def test_discovery_cannot_replace_exact_price(db):
    now = datetime.now(timezone.utc)
    async with db() as session:
        await market_data._upsert_exact_snapshot(session, pair(), now, source="dexscreener_exact_pair")
        await session.commit()
        await market_data._upsert_exact_snapshot(session, pair(99), now, source="dexscreener_discovery")
        await session.commit()
        row = await session.get(PairLatestPrice, "pair")
        assert row.price_usd == 1 and row.source == "dexscreener_exact_pair"


@pytest.mark.asyncio
async def test_missing_pair_does_not_delay_or_falsely_timestamp_batch():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"pairs": [pair(), pair(address="unrequested")]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await market_data._fetch_exact_pairs(client, Settings(_env_file=None), ["pair", "missing"])
    assert len(calls) == 1
    assert [p["pairAddress"] for p in result] == ["pair"]


@pytest.mark.asyncio
async def test_critical_pairs_deduplicate_before_limit_and_ignore_future(db):
    now = datetime.now(timezone.utc)
    async with db() as session:
        for index in range(100):
            session.add(SignalMeasurementV06(signal_id=index, token_mint="mint", pair_address="same",
                        horizon_seconds=30, baseline_price=1, due_at=now))
        session.add(SignalMeasurementV06(signal_id=101, token_mint="other", pair_address="other",
                    horizon_seconds=30, baseline_price=1, due_at=now))
        session.add(SignalMeasurementV06(signal_id=102, token_mint="future", pair_address="future",
                    horizon_seconds=300, baseline_price=1, due_at=now + timedelta(minutes=5)))
        await session.commit()
    pairs, _ = await market_data._critical_pairs_and_missing(Settings(_env_file=None))
    assert set(pairs) == {"same", "other"}


@pytest.mark.asyncio
async def test_copy_seeding_advances_past_300_processed_swaps(db):
    now = datetime.now(timezone.utc)
    async with db() as session:
        session.add(SystemState(key="v06_verified_epoch", value=(now - timedelta(days=1)).isoformat()))
        for index in range(1, 302):
            session.add(WalletSwapV06(id=index, wallet="wallet", signature=str(index),
                        token_mint="mint", token_delta=1, action="BUY", ts=now,
                        pair_address="pair", copy_eligible=True, tracked_at_detection=True))
            if index <= 300:
                session.add(PaperCopyTradeV06(swap_id=index, wallet="wallet", token_mint="mint",
                            pair_address="pair", detected_at=now, entry_eligible_at=now,
                            entry_deadline_at=now, status="rejected", notional_usd=100))
        await session.commit()
    assert await paper_copy_v06._seed_new(Settings(_env_file=None)) == 1
    assert await paper_copy_v06._seed_new(Settings(_env_file=None)) == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 0])
def test_fill_rejects_invalid_liquidity(bad):
    with pytest.raises(ValueError):
        simulate_buy(market_price=1, notional_usd=100, liquidity_usd=bad, fee_bps=100, base_slippage_bps=50)
    with pytest.raises(ValueError):
        simulate_sell(market_price=1, qty=100, liquidity_usd=bad, fee_bps=100, base_slippage_bps=50)


def decision():
    return dict(verdict="BUY", confidence=80, expected_edge_pct=10,
                suggested_stop_loss_pct=5, suggested_take_profit_pct=10,
                suggested_max_hold_seconds=180, thesis="Evidence", risks=[])


@pytest.mark.parametrize("obj", [{"verdict": "BUY"}, decision() | {"confidence": float("nan")},
                                 decision() | {"expected_edge_pct": float("inf")},
                                 decision() | {"confidence": "90"}])
def test_malformed_ai_decisions_fail_closed(obj):
    with pytest.raises(ValueError):
        _normalize(obj)


@pytest.mark.asyncio
async def test_claude_error_carries_diagnostic_and_redacts_key():
    def handler(request):
        body = json.loads(request.content)
        assert "system" in body and "messages" in body
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": "bad SECRET"},
                                        "request_id": "req-test"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status, data, _ = await _ask_claude(client, Settings(_env_file=None, anthropic_api_key="SECRET"), {})
    assert status == "http_400"
    assert data["error_code"] == "invalid_request_error"
    assert data["request_id"] == "req-test"
    assert "SECRET" not in json.dumps(data)


@pytest.mark.asyncio
async def test_claude_truncated_response_is_not_healthy():
    def handler(request):
        return httpx.Response(200, json={"stop_reason": "max_tokens", "content": [
            {"type": "text", "text": json.dumps(decision())}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status, _, _ = await _ask_claude(client, Settings(_env_file=None, anthropic_api_key="SECRET"), {})
    assert status == "incomplete"


@pytest.mark.asyncio
async def test_true_live_flag_fails_before_services_start():
    supervisor = Supervisor(Settings(_env_file=None, live_trading_enabled=True))
    with pytest.raises(LiveTradingLocked):
        await supervisor.start()
    assert not supervisor.tasks


@pytest.mark.asyncio
async def test_pending_backlog_does_not_count_as_open_capacity(db, monkeypatch):
    now = datetime.now(timezone.utc)
    async def circuit(_settings):
        return False, 0, now
    monkeypatch.setattr(paper_copy_v06, "copy_daily_circuit_open", circuit)
    async with db() as session:
        session.add(WalletCopyabilityV06(wallet="wallet", median_return_300s_pct=50))
        await market_data._upsert_exact_snapshot(session, pair(), now, source="dexscreener_exact_pair")
        for index in range(3):
            session.add(PaperCopyTradeV06(swap_id=index, wallet="wallet", token_mint="mint",
                        pair_address="pair", detected_at=now-timedelta(seconds=5),
                        entry_eligible_at=now-timedelta(seconds=1), entry_deadline_at=now+timedelta(seconds=30),
                        status="pending", notional_usd=100))
        await session.commit()
    entered, _ = await paper_copy_v06._enter_due(Settings(_env_file=None, rug_shield_enabled=False,
                                                        paper_copy_max_open_trades=1))
    assert entered == 1


@pytest.mark.asyncio
async def test_candles_deduplicate_receipts_and_separate_pairs(db, monkeypatch):
    async def watched(_settings):
        return ["mint"]
    monkeypatch.setattr(candle_sampler, "_watch_mints", watched)
    now = datetime.now(timezone.utc)
    async with db() as session:
        session.add(TokenPairState(token_mint="mint", base_mint="mint", pair_address="pair", status="active"))
        await session.commit()
        await market_data._upsert_exact_snapshot(session, pair(), now, source="dexscreener_exact_pair")
        await session.commit()
    settings = Settings(_env_file=None)
    assert await candle_sampler._sample(settings) == 1
    assert await candle_sampler._sample(settings) == 0
    async with db() as session:
        state = await session.get(TokenPairState, "mint")
        state.pair_address = "new"
        await market_data._upsert_exact_snapshot(session, pair(2, address="new"), now, source="dexscreener_exact_pair")
        await session.commit()
    assert await candle_sampler._sample(settings) == 1
    async with db() as session:
        candles = (await session.execute(select(PriceCandleV074))).scalars().all()
        assert {c.pair_address for c in candles} == {"pair", "new"}
        assert all(c.samples == 1 for c in candles)
    from app import main
    monkeypatch.setattr(main, "SessionLocal", db)
    returned = await main.candles(mint="mint", limit=120)
    assert len(returned) == 1 and returned[0]["pair_address"] == "new"


@pytest.mark.asyncio
async def test_pruning_preserves_research_and_recent_receipts(db):
    now = datetime.now(timezone.utc)
    async with db() as session:
        session.add(Event(source="test", event_type="telemetry", payload_json="{}", ts=now-timedelta(days=1)))
        session.add(SignalMeasurementV06(signal_id=1, token_mint="mint", pair_address="pair",
                    horizon_seconds=30, baseline_price=1, due_at=now-timedelta(days=1)))
        for age in (20, 1):
            session.add(PairPriceObservation(pair_address="pair", base_mint="mint", price_usd=1,
                        source="dexscreener_exact_pair", ts=now-timedelta(minutes=age)))
        await session.commit()
    assert await maintenance.prune_telemetry() == {"events": 1, "pair_price_observations": 1}
    async with db() as session:
        assert (await session.execute(select(SignalMeasurementV06))).scalar_one()
        assert (await session.execute(select(PairPriceObservation))).scalar_one()


@pytest.mark.asyncio
async def test_critical_refresh_runs_while_discovery_is_blocked(monkeypatch):
    stop = asyncio.Event()
    discovery_started = asyncio.Event()
    refreshed = asyncio.Event()
    async def critical(_):
        return ["critical"], ["mint"]
    async def broad(_):
        return [], ["mint"]
    async def discover(*_):
        discovery_started.set()
        await stop.wait()
    async def refresh(*args):
        if args[-1] == ["critical"]:
            await discovery_started.wait()
            refreshed.set()
    monkeypatch.setattr(market_data, "_critical_pairs_and_missing", critical)
    monkeypatch.setattr(market_data, "_broad_pairs_and_missing", broad)
    monkeypatch.setattr(market_data, "_discover_missing", discover)
    monkeypatch.setattr(market_data, "_refresh_exact", refresh)
    task = asyncio.create_task(market_data.run_market_data(Settings(_env_file=None), stop))
    try:
        await asyncio.wait_for(refreshed.wait(), timeout=2)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_helius_worker_counts_failed_transaction(monkeypatch):
    stop = asyncio.Event()
    queue = asyncio.Queue()
    await queue.put(("wallet", "signature", datetime.now(timezone.utc)-timedelta(seconds=2)))
    counters = {"processed": 0, "failed": 0}
    async def missing(*args):
        stop.set()
        raise RuntimeError("confirmed_transaction_unavailable_after_retries")
    monkeypatch.setattr(helius, "_record_wallet_swap", missing)
    await helius._tx_worker(1, queue, Settings(_env_file=None), None, stop, counters)
    await asyncio.wait_for(queue.join(), timeout=1)
    assert counters["failed"] == 1 and counters["processed"] == 0
    assert counters["last_queue_lag_seconds"] >= 2


@pytest.mark.parametrize("raw", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_markouts_never_count_as_valid(raw):
    assert copyability_math.classify_return(raw, 1000) == (False, "nonfinite_return")


@pytest.mark.asyncio
async def test_integrity_endpoint_preserves_auth_and_reports_invalid_exposure(db, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "SessionLocal", db)
    monkeypatch.setattr(main, "settings", Settings(_env_file=None, dashboard_auth_enabled=True,
                                                  dashboard_username="test", dashboard_password="test"))
    now = datetime.now(timezone.utc)
    async with db() as session:
        session.add(PaperCopyTradeV06(swap_id=1, wallet="wallet", token_mint="mint", pair_address="pair",
                    detected_at=now, entry_eligible_at=now, entry_deadline_at=now,
                    status="invalid", qty=10, notional_usd=100))
        session.add(SignalMeasurementV06(signal_id=1, token_mint="mint", pair_address="pair",
                    horizon_seconds=30, baseline_price=1, due_at=now-timedelta(seconds=30),
                    captured_at=now, integrity_status="invalid", invalid_reason="late_snapshot"))
        await session.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/api/data-integrity")).status_code == 401
        response = await client.get("/api/data-integrity", auth=("test", "test"))
        assert response.status_code == 200
        data = response.json()
        assert data["unresolved_exposure"]["wallet"]["original_notional_usd"] == 100
        assert data["measurements"]["signal"]["valid_pct_of_resolved"] == 0


@pytest.mark.asyncio
async def test_sqlite_candle_migration_is_idempotent_and_preserves_history(tmp_path, monkeypatch):
    from app import db as app_db
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.exec_driver_sql("ALTER TABLE price_candles_v074 DROP COLUMN last_observed_at")
            await conn.exec_driver_sql("CREATE TABLE legacy_marker (value TEXT)")
            await conn.exec_driver_sql("INSERT INTO legacy_marker VALUES ('keep')")
        monkeypatch.setattr(app_db, "engine", engine)
        monkeypatch.setattr(app_db, "settings", Settings(_env_file=None))
        await app_db.init_db()
        await app_db.init_db()
        async with engine.connect() as conn:
            columns = (await conn.exec_driver_sql("PRAGMA table_info(price_candles_v074)")).all()
            assert "last_observed_at" in {row[1] for row in columns}
            assert (await conn.exec_driver_sql("SELECT value FROM legacy_marker")).scalar_one() == "keep"
    finally:
        await engine.dispose()
