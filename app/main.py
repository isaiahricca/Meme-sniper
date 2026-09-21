import logging
import asyncio
import json
import time
import base64
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import uvicorn
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select, func

from app.config import get_settings
from app.db import init_db, SessionLocal
from app.models import (
    Event, Token, Signal, TrackedWallet, SmartWalletProfile, BirdeyeTokenScan,
    SystemState, TokenPairState, WalletSwapV06, WalletCopyabilityV06, WalletSwapMeasurementV06,
    PaperCopyTradeV06, SignalPaperTradeV06, SignalMeasurementV06,
    NansenSmartTradeV07, TraderIntelligenceV07, SmartMoneyClusterV07, TokenRiskV072,
    AIEnsembleDecisionV074, XSocialSnapshotV074, PriceCandleV074,
)
from app.services.supervisor import Supervisor
from app.services.performance import build_performance, verified_epoch
from app.services.forward_test import shadow_signal_summary, copy_daily_circuit_open
from app.services.command_center import status as command_status, traders as command_traders, clusters as command_clusters, opportunities as command_opportunities, live_feed as command_live_feed
from app.pages_v07 import COMMAND_PAGE, TRADERS_PAGE, INTEGRATIONS_PAGE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# httpx INFO logs include full request URLs. Helius authenticates in the URL,
# so suppress these logs to prevent API-key leakage in screenshots/console logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

settings = get_settings()
supervisor = Supervisor(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await supervisor.start()
    yield
    await supervisor.stop()


app = FastAPI(title=f"{settings.brand_name} V0.7.4", version="0.7.4", lifespan=lifespan)


@app.middleware("http")
async def dashboard_basic_auth(request, call_next):
    # Local installs stay frictionless. Cloud/public installs can enable Basic Auth
    # via environment variables; HTTPS is provided by the cloud platform.
    if not settings.dashboard_auth_enabled or request.url.path == "/health":
        return await call_next(request)
    if not settings.dashboard_password:
        return Response("Dashboard auth is enabled but no password is configured.", status_code=503)
    header = request.headers.get("authorization", "")
    valid = False
    if header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            username, password = decoded.split(":", 1)
            valid = secrets.compare_digest(username, settings.dashboard_username) and secrets.compare_digest(password, settings.dashboard_password)
        except Exception:
            valid = False
    if not valid:
        return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Meme Sniper"'})
    return await call_next(request)



# ---------------------------------------------------------------------------
# Token Explorer
# ---------------------------------------------------------------------------
_TOKEN_EXPLORER_CACHE = {"at": 0.0, "key": (), "rows": []}
_TOKEN_EXPLORER_LOCK = asyncio.Lock()
_TOKEN_EXPLORER_TTL_SECONDS = 20.0
_DEXSCREENER_BASE = "https://api.dexscreener.com"


def _number(value, default=None):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _int_number(value, default=0):
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _liquidity(pair: dict) -> float:
    return _number((pair.get("liquidity") or {}).get("usd"), 0.0) or 0.0


def _choose_token_pair(mint: str, pairs: list[dict], pinned_pair: str | None) -> dict | None:
    candidates = [
        p for p in pairs
        if p.get("chainId") == "solana"
        and (p.get("baseToken") or {}).get("address") == mint
    ]
    if not candidates:
        return None
    if pinned_pair:
        exact = next((p for p in candidates if p.get("pairAddress") == pinned_pair), None)
        if exact is not None:
            return exact
    return max(candidates, key=_liquidity)


def _token_origin(token, pair: dict | None) -> str:
    source = (token.source or "").lower()
    mint = token.mint or ""
    dex = ((pair or {}).get("dexId") or "").lower()
    if source == "pumpportal" or mint.lower().endswith("pump"):
        return "Pump.fun"
    if "pump" in dex:
        return "PumpSwap"
    if dex == "raydium":
        return "Raydium"
    if dex == "meteora":
        return "Meteora"
    return (pair or {}).get("dexId") or token.source or "Unknown"


def _age_seconds(pair_created_at) -> int | None:
    try:
        created = float(pair_created_at) / 1000.0
        return max(0, int(datetime.now(timezone.utc).timestamp() - created))
    except (TypeError, ValueError):
        return None


async def _fetch_token_explorer_rows(limit: int) -> list[dict]:
    limit = min(max(limit, 1), 30)

    async with SessionLocal() as session:
        tokens = list((
            await session.execute(
                select(Token).order_by(Token.discovered_at.desc()).limit(limit)
            )
        ).scalars())
        pair_states = {}
        if tokens:
            pair_states = {
                row.token_mint: row
                for row in (
                    await session.execute(
                        select(TokenPairState).where(
                            TokenPairState.token_mint.in_([t.mint for t in tokens])
                        )
                    )
                ).scalars()
            }

    if not tokens:
        return []

    cache_key = tuple(t.mint for t in tokens)
    now_mono = time.monotonic()
    if (
        _TOKEN_EXPLORER_CACHE["key"] == cache_key
        and now_mono - _TOKEN_EXPLORER_CACHE["at"] < _TOKEN_EXPLORER_TTL_SECONDS
    ):
        return _TOKEN_EXPLORER_CACHE["rows"]

    async with _TOKEN_EXPLORER_LOCK:
        now_mono = time.monotonic()
        if (
            _TOKEN_EXPLORER_CACHE["key"] == cache_key
            and now_mono - _TOKEN_EXPLORER_CACHE["at"] < _TOKEN_EXPLORER_TTL_SECONDS
        ):
            return _TOKEN_EXPLORER_CACHE["rows"]

        pairs = []
        try:
            joined = ",".join(cache_key)
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{_DEXSCREENER_BASE}/tokens/v1/solana/{joined}"
                )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, list):
                    pairs = payload
        except Exception as exc:
            logging.getLogger("token_explorer").warning(
                "DEX Screener explorer enrichment unavailable: %s", exc
            )

        by_mint = {}
        for pair in pairs:
            mint = (pair.get("baseToken") or {}).get("address")
            if mint:
                by_mint.setdefault(mint, []).append(pair)

        result = []
        for rank, token in enumerate(tokens, start=1):
            state = pair_states.get(token.mint)
            pair = _choose_token_pair(
                token.mint,
                by_mint.get(token.mint, []),
                state.pair_address if state else token.pair_address,
            )

            info = (pair or {}).get("info") or {}
            base = (pair or {}).get("baseToken") or {}
            txns = (pair or {}).get("txns") or {}
            h24_txns = txns.get("h24") or {}
            volume = (pair or {}).get("volume") or {}
            change = (pair or {}).get("priceChange") or {}

            origin = _token_origin(token, pair)
            pair_address = (pair or {}).get("pairAddress") or (
                state.pair_address if state else token.pair_address
            )
            dex_url = (pair or {}).get("url")
            if not dex_url and pair_address:
                dex_url = f"https://dexscreener.com/solana/{pair_address}"

            pump_url = (
                f"https://pump.fun/coin/{token.mint}"
                if origin in {"Pump.fun", "PumpSwap"} or token.mint.lower().endswith("pump")
                else None
            )

            result.append({
                "rank": rank,
                "mint": token.mint,
                "symbol": base.get("symbol") or token.symbol,
                "name": base.get("name") or token.name,
                "image_url": info.get("imageUrl"),
                "origin": origin,
                "dex_id": (pair or {}).get("dexId") or (state.dex_id if state else None),
                "pair_address": pair_address,
                "market_cap": _number((pair or {}).get("marketCap")),
                "fdv": _number((pair or {}).get("fdv")),
                "price_usd": _number((pair or {}).get("priceUsd"), token.price_usd),
                "liquidity_usd": _number(
                    ((pair or {}).get("liquidity") or {}).get("usd"),
                    token.liquidity_usd,
                ),
                "age_seconds": _age_seconds((pair or {}).get("pairCreatedAt")),
                "txns_h24": (
                    _int_number(h24_txns.get("buys")) + _int_number(h24_txns.get("sells"))
                    if pair else None
                ),
                "volume_h24_usd": _number(volume.get("h24"), token.volume_h24_usd),
                "change_m5_pct": _number(change.get("m5"), token.price_change_m5_pct),
                "change_h1_pct": _number(change.get("h1")),
                "change_h6_pct": _number(change.get("h6")),
                "change_h24_pct": _number(change.get("h24")),
                "dexscreener_url": dex_url,
                "solscan_url": f"https://solscan.io/token/{token.mint}",
                "pump_url": pump_url,
                "discovered_at": token.discovered_at.isoformat(),
            })

        _TOKEN_EXPLORER_CACHE.update({
            "at": time.monotonic(),
            "key": cache_key,
            "rows": result,
        })
        return result


def _brand_html(value: str) -> str:
    return value.replace("Meme Sniper", settings.brand_name)


@app.get("/health")
async def health():
    async with SessionLocal() as session:
        epoch = await verified_epoch(session)
    return {
        "status": "ok",
        "version": "0.7.4",
        "mode": "AGGRESSIVE_PAPER_CHALLENGE",
        "live_trading": False,
        "verified_epoch": epoch.isoformat() if epoch else None,
        "helius_enabled": settings.helius_enabled,
        "pumpportal_enabled": settings.pumpportal_enabled,
        "dexscreener_enabled": settings.dexscreener_enabled,
        "birdeye_enabled": settings.birdeye_enabled,
    }


@app.get("/api/performance")
async def performance(
    strategy: str = Query("all", pattern="^(all|copy|signal)$"),
    range: str = Query("all", pattern="^(forward|1h|today|7d|30d|all)$"),
):
    return await build_performance(strategy, range)


@app.get("/api/overview")
async def overview():
    historical = await build_performance("all", "all")
    forward = await build_performance("all", "forward")
    today = await build_performance("all", "today")
    shadow = await shadow_signal_summary(settings)
    circuit_open, circuit_pnl, _ = await copy_daily_circuit_open(settings)
    async with SessionLocal() as session:
        gate_row = await session.get(SystemState, "v074_challenge_gate_stats")
        try:
            challenge_gate = json.loads(gate_row.value) if gate_row and gate_row.value else {}
        except Exception:
            challenge_gate = {}
        event_count = (await session.execute(select(func.count(Event.id)))).scalar_one()
        token_count = (await session.execute(select(func.count(Token.mint)))).scalar_one()
        enabled_wallets = (await session.execute(
            select(func.count(TrackedWallet.address)).where(TrackedWallet.enabled.is_(True))
        )).scalar_one()
        invalid_copy = (await session.execute(
            select(func.count(PaperCopyTradeV06.id)).where(PaperCopyTradeV06.status == "invalid")
        )).scalar_one()
        invalid_signal = (await session.execute(
            select(func.count(SignalPaperTradeV06.id)).where(SignalPaperTradeV06.status == "invalid")
        )).scalar_one()
        excluded_measurements = (await session.execute(
            select(func.count(WalletSwapMeasurementV06.id)).where(
                WalletSwapMeasurementV06.captured_at.is_not(None),
                WalletSwapMeasurementV06.valid_for_score.is_(False),
            )
        )).scalar_one()
        valid_measurements = (await session.execute(
            select(func.count(WalletSwapMeasurementV06.id)).where(
                WalletSwapMeasurementV06.valid_for_score.is_(True)
            )
        )).scalar_one()
    return {
        "mode": "VERIFIED PAPER",
        "events": event_count,
        "tokens": token_count,
        "tracked_wallets": enabled_wallets,
        "realized_pnl_usd": historical["realized_pnl_usd"],
        "forward_pnl_usd": forward["realized_pnl_usd"],
        "today_pnl_usd": today["realized_pnl_usd"],
        "win_rate_pct": forward["win_rate_pct"],
        "max_drawdown_usd": forward["max_drawdown_usd"],
        "profit_factor": forward["profit_factor"],
        "verified_trades": forward["trades"],
        "open_verified_trades": forward["open_trades"],
        "forward_epoch": forward.get("forward_epoch"),
        "shadow_signal": shadow,
        "challenge_gate": challenge_gate,
        "daily_loss_circuit_open": circuit_open,
        "daily_copy_pnl_usd": circuit_pnl,
        "invalid_trades": invalid_copy + invalid_signal,
        "valid_measurements": valid_measurements,
        "excluded_measurements": excluded_measurements,
        "epoch": historical["epoch"],
    }


@app.get("/api/tokens")
async def tokens(limit: int = 30):
    limit = min(max(limit, 1), 100)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(Token).order_by(Token.discovered_at.desc()).limit(limit)
        )).scalars())
        return [{
            "mint": r.mint, "symbol": r.symbol, "name": r.name,
            "price_usd": r.price_usd, "liquidity_usd": r.liquidity_usd,
            "volume_h24_usd": r.volume_h24_usd,
            "price_change_m5_pct": r.price_change_m5_pct,
            "buys_m5": r.buys_m5, "sells_m5": r.sells_m5,
            "pair_address": r.pair_address,
        } for r in rows]



@app.get("/api/token-explorer")
async def token_explorer(limit: int = 25):
    return await _fetch_token_explorer_rows(limit)


@app.get("/api/signals")
async def signals(limit: int = 30):
    limit = min(max(limit, 1), 100)
    async with SessionLocal() as session:
        epoch = await verified_epoch(session)
        stmt = select(Signal)
        if epoch:
            stmt = stmt.where(Signal.ts >= epoch)
        rows = list((await session.execute(stmt.order_by(Signal.ts.desc()).limit(limit))).scalars())
        return [{
            "ts": r.ts.isoformat(), "mint": r.token_mint,
            "score": r.total_score, "decision": r.decision,
            "momentum": r.momentum_score, "liquidity": r.liquidity_score,
            "flow": r.flow_score, "wallet": r.wallet_score,
        } for r in rows]


@app.get("/api/smart-wallets")
async def smart_wallets(limit: int = 30):
    limit = min(max(limit, 1), 100)
    async with SessionLocal() as session:
        profiles = list((await session.execute(
            select(SmartWalletProfile).order_by(SmartWalletProfile.score.desc()).limit(limit)
        )).scalars())
        tracked = {x.address: x for x in (await session.execute(select(TrackedWallet))).scalars()}
        copies = {x.wallet: x for x in (await session.execute(select(WalletCopyabilityV06))).scalars()}
        out = []
        for r in profiles:
            c = copies.get(r.wallet)
            t = tracked.get(r.wallet)
            out.append({
                "wallet": r.wallet, "birdeye_score": round(r.score, 1), "birdeye_tier": r.tier,
                "win_rate_pct": None if r.win_rate_pct is None else round(r.win_rate_pct, 1),
                "realized_pnl_usd_30d": r.realized_pnl_usd_30d,
                "total_trades_30d": r.total_trades_30d,
                "seen": r.discovery_count,
                "tracked": bool(t and t.enabled),
                "policy_score": None if not t else round(t.score, 1),
                "copy_score": None if not c else round(c.copyability_score, 1),
                "copy_tier": None if not c else c.copyability_tier,
                "observations": 0 if not c else c.eligible_observations,
                "hft_penalty": 0 if not c else round(c.hft_penalty, 1),
            })
        return out


@app.get("/api/copyability")
async def copyability(limit: int = 40):
    limit = min(max(limit, 1), 100)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(WalletCopyabilityV06)
            .order_by(WalletCopyabilityV06.copyability_score.desc()).limit(limit)
        )).scalars())
        return [{
            "wallet": r.wallet, "score": round(r.copyability_score, 1),
            "tier": r.copyability_tier, "observations": r.eligible_observations,
            "excluded": r.excluded_measurements, "edge": round(r.observed_edge_score, 1),
            "hft_penalty": round(r.hft_penalty, 1),
            "r10": None if r.median_return_10s_pct is None else round(r.median_return_10s_pct, 2),
            "r30": None if r.median_return_30s_pct is None else round(r.median_return_30s_pct, 2),
            "r60": None if r.median_return_60s_pct is None else round(r.median_return_60s_pct, 2),
            "r300": None if r.median_return_300s_pct is None else round(r.median_return_300s_pct, 2),
            "target_hit": None if r.target_hit_rate_pct is None else round(r.target_hit_rate_pct, 1),
            "lead": r.median_target_horizon_seconds,
        } for r in rows]


@app.get("/api/wallet-swaps")
async def wallet_swaps(limit: int = 40):
    limit = min(max(limit, 1), 200)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(WalletSwapV06).order_by(WalletSwapV06.ts.desc()).limit(limit)
        )).scalars())
        return [{
            "ts": r.ts.isoformat(), "wallet": r.wallet, "action": r.action,
            "mint": r.token_mint, "token_delta": r.token_delta,
            "quote_mint": r.quote_mint, "quote_delta": r.quote_delta,
            "classification": r.classification, "copy_eligible": r.copy_eligible,
            "integrity": r.integrity_status, "invalid_reason": r.invalid_reason,
        } for r in rows]


@app.get("/api/paper-copy-trades")
async def paper_copy_trades(limit: int = 50):
    limit = min(max(limit, 1), 200)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(PaperCopyTradeV06).order_by(PaperCopyTradeV06.id.desc()).limit(limit)
        )).scalars())
        return [{
            "id": r.id, "wallet": r.wallet, "mint": r.token_mint, "status": r.status,
            "integrity": r.integrity_status, "reason": r.exit_reason or r.reject_reason,
            "copy_score": r.wallet_copy_score, "copy_tier": r.wallet_copy_tier,
            "delay_s": r.detection_to_entry_seconds,
            "entry": r.entry_fill_price, "exit": r.exit_fill_price,
            "pnl_usd": r.pnl_usd, "pnl_pct": r.pnl_pct,
            "mfe": r.max_favourable_pct, "mae": r.max_adverse_pct,
        } for r in rows]


@app.get("/api/signal-trades")
async def signal_trades(limit: int = 50):
    limit = min(max(limit, 1), 200)
    async with SessionLocal() as session:
        stmt = select(SignalPaperTradeV06)
        epoch_row = await session.get(SystemState, settings.forward_epoch_key)
        if epoch_row is not None:
            try:
                epoch = datetime.fromisoformat(epoch_row.value)
                if epoch.tzinfo is None:
                    epoch = epoch.replace(tzinfo=timezone.utc)
                stmt = stmt.where(SignalPaperTradeV06.signal_at >= epoch)
            except Exception:
                pass
        rows = list((await session.execute(
            stmt.order_by(SignalPaperTradeV06.id.desc()).limit(limit)
        )).scalars())
        return [{
            "id": r.id, "mint": r.token_mint, "status": r.status,
            "integrity": r.integrity_status, "score": r.entry_score,
            "reason": r.exit_reason, "entry": r.entry_fill_price, "exit": r.exit_fill_price,
            "pnl_usd": r.pnl_usd, "pnl_pct": r.pnl_pct,
            "mfe": r.max_favourable_pct, "mae": r.max_adverse_pct,
            "notional_usd": r.notional_usd,
        } for r in rows]


@app.get("/api/challenge-curve")
async def challenge_curve():
    async with SessionLocal() as session:
        epoch = None
        epoch_row = await session.get(SystemState, settings.forward_epoch_key)
        if epoch_row is not None:
            try:
                epoch = datetime.fromisoformat(epoch_row.value)
                if epoch.tzinfo is None:
                    epoch = epoch.replace(tzinfo=timezone.utc)
            except Exception:
                epoch = None
        stmt = select(SignalPaperTradeV06).where(
            SignalPaperTradeV06.status == "closed",
            SignalPaperTradeV06.integrity_status == "shadow_closed",
        )
        if epoch is not None:
            stmt = stmt.where(SignalPaperTradeV06.closed_at >= epoch)
        rows = list((await session.execute(
            stmt.order_by(SignalPaperTradeV06.closed_at.asc(), SignalPaperTradeV06.id.asc())
        )).scalars())
        running = 0.0
        curve = []
        for row in rows:
            running += float(row.pnl_usd or 0.0)
            curve.append({
                "ts": row.closed_at.isoformat() if row.closed_at else None,
                "cumulative_pnl": round(running, 4),
                "trade_pnl": round(float(row.pnl_usd or 0.0), 4),
                "pnl_pct": row.pnl_pct,
                "mint": row.token_mint,
                "reason": row.exit_reason,
            })
        return {"curve": curve, "closed": len(rows), "realized_pnl": round(running, 4)}


@app.get("/api/candle-watchlist")
async def candle_watchlist(limit: int = 24):
    limit = min(max(limit, 1), 50)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(PriceCandleV074.token_mint, func.max(PriceCandleV074.bucket_at).label("latest"))
            .group_by(PriceCandleV074.token_mint)
            .order_by(func.max(PriceCandleV074.bucket_at).desc())
            .limit(limit)
        )).all())
        mints = [r[0] for r in rows]
        tokens = {}
        if mints:
            token_rows = list((await session.execute(select(Token).where(Token.mint.in_(mints)))).scalars())
            tokens = {t.mint: t for t in token_rows}
        return [{
            "mint": mint,
            "symbol": tokens[mint].symbol if mint in tokens else None,
            "name": tokens[mint].name if mint in tokens else None,
            "latest": latest.isoformat() if latest else None,
        } for mint, latest in rows]


@app.get("/api/candles")
async def candles(mint: str = Query(..., min_length=20, max_length=100), limit: int = 120):
    limit = min(max(limit, 10), 180)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(PriceCandleV074)
            .where(PriceCandleV074.token_mint == mint)
            .order_by(PriceCandleV074.bucket_at.desc())
            .limit(limit)
        )).scalars())
        rows.reverse()
        return [{
            "t": r.bucket_at.isoformat(),
            "o": r.open, "h": r.high, "l": r.low, "c": r.close,
            "liquidity": r.liquidity_usd, "samples": r.samples,
        } for r in rows]


@app.get("/api/x-social-status")
async def x_social_status():
    async with SessionLocal() as session:
        latest = (await session.execute(
            select(XSocialSnapshotV074)
            .where(XSocialSnapshotV074.token_mint != "__GLOBAL__")
            .order_by(XSocialSnapshotV074.fetched_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        covered = int((await session.execute(
            select(func.count(func.distinct(XSocialSnapshotV074.token_mint)))
            .where(XSocialSnapshotV074.token_mint != "__GLOBAL__")
        )).scalar_one() or 0)
        return {
            "configured": bool(settings.x_bearer_token),
            "enabled": settings.x_enabled,
            "covered_tokens": covered,
            "latest_at": latest.fetched_at.isoformat() if latest else None,
            "latest_score": latest.social_score if latest else None,
        }


@app.get("/api/ai-ensemble")
async def ai_ensemble(limit: int = 25):
    limit = min(max(limit, 1), 100)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(AIEnsembleDecisionV074)
            .order_by(AIEnsembleDecisionV074.created_at.desc())
            .limit(limit)
        )).scalars())
        signal_ids = [x.signal_id for x in rows]
        trades = {}
        if signal_ids:
            trade_rows = list((await session.execute(
                select(SignalPaperTradeV06)
                .where(SignalPaperTradeV06.signal_id.in_(signal_ids))
                .order_by(SignalPaperTradeV06.id.desc())
            )).scalars())
            for t in trade_rows:
                trades.setdefault(t.signal_id, t)

        total = (await session.execute(select(func.count(AIEnsembleDecisionV074.signal_id)))).scalar_one()
        pre_entry = (await session.execute(
            select(func.count(AIEnsembleDecisionV074.signal_id))
            .where(AIEnsembleDecisionV074.pre_entry.is_(True))
        )).scalar_one()
        both_ok = (await session.execute(
            select(func.count(AIEnsembleDecisionV074.signal_id))
            .where(
                AIEnsembleDecisionV074.openai_status == "ok",
                AIEnsembleDecisionV074.claude_status == "ok",
            )
        )).scalar_one()

        return {
            "enabled": settings.ai_ensemble_enabled,
            "openai_configured": bool(settings.openai_api_key),
            "claude_configured": bool(settings.anthropic_api_key),
            "x_configured": bool(settings.x_bearer_token),
            "ai_trade_gate": bool(settings.ai_trade_gate_enabled),
            "openai_model": settings.openai_model,
            "claude_model": settings.anthropic_model,
            "total_decisions": int(total or 0),
            "pre_entry_decisions": int(pre_entry or 0),
            "both_models_ok": int(both_ok or 0),
            "hourly_cap": settings.ai_max_analyses_per_hour,
            "rows": [{
                "signal_id": r.signal_id,
                "ts": r.created_at.isoformat() if r.created_at else None,
                "mint": r.token_mint,
                "pre_entry": r.pre_entry,
                "signal_age_s": r.signal_age_seconds,
                "openai_status": r.openai_status,
                "openai_verdict": r.openai_verdict,
                "openai_confidence": r.openai_confidence,
                "openai_edge": r.openai_expected_edge_pct,
                "claude_status": r.claude_status,
                "claude_verdict": r.claude_verdict,
                "claude_confidence": r.claude_confidence,
                "claude_edge": r.claude_expected_edge_pct,
                "consensus": r.consensus,
                "consensus_confidence": r.consensus_confidence,
                "trade_status": trades[r.signal_id].status if r.signal_id in trades else None,
                "trade_pnl_pct": trades[r.signal_id].pnl_pct if r.signal_id in trades else None,
                "trade_reason": trades[r.signal_id].exit_reason if r.signal_id in trades else None,
            } for r in rows],
        }


@app.get("/api/birdeye-status")
async def birdeye_status():
    async with SessionLocal() as session:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        scans = (await session.execute(
            select(func.count(BirdeyeTokenScan.token_mint)).where(BirdeyeTokenScan.scanned_at >= start)
        )).scalar_one()
        cu = (await session.execute(
            select(func.coalesce(func.sum(BirdeyeTokenScan.cu_estimated), 0)).where(BirdeyeTokenScan.scanned_at >= start)
        )).scalar_one()
    return {"enabled": settings.birdeye_enabled, "scans_today": scans, "scan_limit": settings.birdeye_token_scan_limit_per_day, "estimated_cu_today": int(cu or 0)}



def _integration_state(enabled: bool, configured: bool = True) -> str:
    if not enabled:
        return "OFF"
    if not configured:
        return "NEEDS KEY"
    return "ACTIVE"


@app.get("/api/integrations")
async def integrations():
    """Operational status. Never returns secret values."""
    async with SessionLocal() as session:
        states = {x.key: x.value for x in (await session.execute(select(SystemState))).scalars()}

        tracked_wallets = (await session.execute(
            select(func.count(TrackedWallet.address)).where(TrackedWallet.enabled.is_(True))
        )).scalar_one()
        pump_tokens = (await session.execute(
            select(func.count(Token.mint)).where(Token.source == "pumpportal")
        )).scalar_one()
        active_pairs = (await session.execute(
            select(func.count(TokenPairState.token_mint)).where(TokenPairState.status == "active")
        )).scalar_one()

        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        birdeye_scans = (await session.execute(
            select(func.count(BirdeyeTokenScan.token_mint)).where(BirdeyeTokenScan.scanned_at >= start)
        )).scalar_one()
        birdeye_cu = (await session.execute(
            select(func.coalesce(func.sum(BirdeyeTokenScan.cu_estimated), 0)).where(
                BirdeyeTokenScan.scanned_at >= start
            )
        )).scalar_one()

        nansen_trades = (await session.execute(
            select(func.count(NansenSmartTradeV07.event_key))
        )).scalar_one()
        trader_universe = (await session.execute(
            select(func.count(TraderIntelligenceV07.wallet))
        )).scalar_one()
        clusters = (await session.execute(
            select(func.count(SmartMoneyClusterV07.cluster_key))
        )).scalar_one()

        valid_meas = (await session.execute(
            select(func.count(WalletSwapMeasurementV06.id)).where(
                WalletSwapMeasurementV06.valid_for_score.is_(True)
            )
        )).scalar_one()
        excluded_meas = (await session.execute(
            select(func.count(WalletSwapMeasurementV06.id)).where(
                WalletSwapMeasurementV06.captured_at.is_not(None),
                WalletSwapMeasurementV06.valid_for_score.is_(False),
            )
        )).scalar_one()

        copy_open = (await session.execute(
            select(func.count(PaperCopyTradeV06.id)).where(PaperCopyTradeV06.status == "open")
        )).scalar_one()
        copy_closed = (await session.execute(
            select(func.count(PaperCopyTradeV06.id)).where(PaperCopyTradeV06.status == "closed")
        )).scalar_one()
        copy_invalid = (await session.execute(
            select(func.count(PaperCopyTradeV06.id)).where(PaperCopyTradeV06.status == "invalid")
        )).scalar_one()

        sig_open = (await session.execute(
            select(func.count(SignalPaperTradeV06.id)).where(SignalPaperTradeV06.status == "open")
        )).scalar_one()
        sig_closed = (await session.execute(
            select(func.count(SignalPaperTradeV06.id)).where(SignalPaperTradeV06.status == "closed")
        )).scalar_one()
        sig_shadow_closed = (await session.execute(
            select(func.count(SignalPaperTradeV06.id)).where(
                SignalPaperTradeV06.status == "closed",
                SignalPaperTradeV06.integrity_status == "shadow_closed",
            )
        )).scalar_one()

        risk_total = (await session.execute(select(func.count(TokenRiskV072.token_mint)))).scalar_one()
        risk_blocked = (await session.execute(select(func.count(TokenRiskV072.token_mint)).where(TokenRiskV072.status == "BLOCK"))).scalar_one()
        risk_pass = (await session.execute(select(func.count(TokenRiskV072.token_mint)).where(TokenRiskV072.status == "PASS"))).scalar_one()

        token_count = (await session.execute(select(func.count(Token.mint)))).scalar_one()
        event_count = (await session.execute(select(func.count(Event.id)))).scalar_one()

    nansen_error = states.get("nansen_last_error") or ""
    nansen_state = _integration_state(settings.nansen_enabled, bool(settings.nansen_api_key))
    if nansen_state == "ACTIVE" and nansen_error:
        nansen_state = "WARNING"

    modules = [
        {"name":"Helius","kind":"DATA SOURCE","added":"V0.1",
         "state":_integration_state(settings.helius_enabled, bool(settings.helius_api_key)),
         "purpose":"Solana RPC and live monitoring of selected smart wallets.",
         "metrics":[["Live tracked wallets",tracked_wallets],["Transaction workers",settings.helius_tx_worker_count],["Commitment",settings.helius_commitment]]},

        {"name":"PumpPortal","kind":"DATA SOURCE","added":"V0.1",
         "state":_integration_state(settings.pumpportal_enabled, bool(settings.pumpportal_api_key)),
         "purpose":"New Pump.fun token and migration discovery.",
         "metrics":[["PumpPortal tokens stored",pump_tokens],["New-token stream","ON" if settings.pumpportal_subscribe_new_tokens else "OFF"],["Migration stream","ON" if settings.pumpportal_subscribe_migrations else "OFF"]]},

        {"name":"DEX Screener","kind":"MARKET DATA","added":"V0.1 / verified V0.6",
         "state":_integration_state(settings.dexscreener_enabled),
         "purpose":"Market discovery plus pinned exact-pair price, liquidity and volume verification.",
         "metrics":[["Active pinned pairs",active_pairs],["Market capacity",settings.market_max_active_pairs],["Critical refresh",f"{settings.market_critical_refresh_seconds:g}s"]]},

        {"name":"Birdeye","kind":"TRADER DATA","added":"V0.3",
         "state":_integration_state(settings.birdeye_enabled, bool(settings.birdeye_api_key)),
         "purpose":"Wallet P/L, win-rate and smart-wallet discovery/verification.",
         "metrics":[["Scans today",f"{birdeye_scans}/{settings.birdeye_token_scan_limit_per_day}"],["Estimated CU today",int(birdeye_cu or 0)],["Minimum wallet score",settings.birdeye_min_wallet_score]]},

        {"name":"Nansen Smart Money","kind":"TRADER DATA","added":"V0.7",
         "state":nansen_state,
         "purpose":"Professional Smart Money discovery: what labelled high-performing traders are buying.",
         "metrics":[["Trades stored",nansen_trades],["New last poll",int(states.get("nansen_last_new_trades") or 0)],["Credits remaining",states.get("nansen_credits_remaining") or "—"],["Last poll",states.get("nansen_last_poll") or "—"],["Poll mode",states.get("nansen_poll_mode") or "—"],["Next poll",(states.get("nansen_next_poll_seconds") or "—") + ("s" if states.get("nansen_next_poll_seconds") else "")]],
         "warning":nansen_error[:180] if nansen_error else None},

        {"name":"GoPlus + Rug Shield","kind":"SECURITY / HARD VETO","added":"V0.7.2",
         "state":("WARNING" if (settings.goplus_enabled and (states.get("goplus_last_error") or "")) else ("ACTIVE" if settings.rug_shield_enabled else "OFF")),
         "purpose":"Pre-entry token security screening plus continuous liquidity/rug monitoring. BLOCK has veto power over every strategy signal.",
         "metrics":[["Tokens screened",risk_total],["PASS",risk_pass],["BLOCKED",risk_blocked],["GoPlus","ON" if settings.goplus_enabled else "OFF"],["Fail closed","YES" if settings.rug_shield_require_pass else "NO"]],
         "warning":(states.get("goplus_last_error") or "")[:180] or None},

        {"name":"Trader Intelligence","kind":"INTERNAL INTELLIGENCE","added":"V0.7",
         "state":"ACTIVE",
         "purpose":"Ranks traders using history, Smart Money activity, copyability and our own paper results.",
         "metrics":[["Trader universe",f"{trader_universe}/{settings.trader_universe_max}"],["Smart-money clusters",clusters],["Cluster window",f"{settings.trader_cluster_window_seconds}s"]]},

        {"name":"Verified Copyability","kind":"INTERNAL ANALYTICS","added":"V0.6",
         "state":"ACTIVE",
         "purpose":"Measures whether a wallet remains profitable to follow after our real detection delay.",
         "metrics":[["Valid measurements",valid_meas],["Excluded",excluded_meas],["Minimum observations",settings.copyability_min_observations],["Horizons","/".join(str(x)+"s" for x in settings.copyability_horizons)]]},

        {"name":"Verified Paper Copy","kind":"EXECUTION SIMULATOR","added":"V0.5 / verified V0.6",
         "state":"ACTIVE" if settings.paper_copy_enabled else "OFF",
         "purpose":"Conservative qualified-wallet simulation with measured forward-edge gates, Rug Shield and a daily loss circuit breaker.",
         "metrics":[["Open",copy_open],["Closed",copy_closed],["Invalid",copy_invalid],["Position",f"${settings.paper_copy_position_usd:g}"],["Min copy score",max(settings.paper_copy_min_copy_score,65)],["Daily stop",f"-${abs(settings.paper_copy_daily_loss_limit_usd):g}"]]},

        {"name":"Aggressive Challenge Engine","kind":"PAPER RESEARCH / SHADOW","added":"V0.7.4",
         "state":"SHADOW" if settings.paper_signal_shadow_mode else "ACTIVE",
         "purpose":"Smart-wallet-confirmed momentum/liquidity/flow entries with cluster confirmation. Real fills, fees and slippage are simulated; results stay separate from verified P/L.",
         "metrics":[["Open challenge trades",sig_open],["Challenge closed",sig_shadow_closed],["All closed records",sig_closed],["Entry threshold",max(settings.paper_entry_score,68)]]},

        {"name":"Local Intelligence Database","kind":"STORAGE","added":"V0.1",
         "state":"ACTIVE",
         "purpose":"Persistent research history used by scoring and the learning pipeline.",
         "metrics":[["Tokens",token_count],["Events",event_count],["Database","SQLite"],["Verified epoch",states.get("v06_verified_epoch") or "ACTIVE"],["V0.7.4 epoch",states.get(settings.forward_epoch_key) or "STARTING"]]},

        {"name":"Real-money Execution","kind":"SAFETY","added":"V0.1",
         "state":"LOCKED",
         "purpose":"Transaction signing and live-money execution are physically disabled in this release.",
         "metrics":[["Live trading","FALSE"],["Private keys stored","NO"],["Transaction signer","NOT IMPLEMENTED"]]},
    ]

    return {
        "version":"0.7.3",
        "brand":settings.brand_name,
        "active":sum(1 for x in modules if x["state"] in {"ACTIVE","SHADOW"}),
        "warning":sum(1 for x in modules if x["state"] in {"WARNING","NEEDS KEY"}),
        "off":sum(1 for x in modules if x["state"]=="OFF"),
        "modules":modules,
    }


@app.get("/api/v07/status")
async def v07_status():
    return await command_status(settings)

@app.get("/api/v07/traders")
async def v07_traders(limit: int = 100):
    return await command_traders(limit)

@app.get("/api/v07/clusters")
async def v07_clusters(limit: int = 50):
    return await command_clusters(limit)

@app.get("/api/v07/opportunities")
async def v07_opportunities(limit: int = 30):
    return await command_opportunities(limit)

@app.get("/api/v07/live-feed")
async def v07_live_feed(limit: int = 80):
    return await command_live_feed(limit)


DASHBOARD = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meme Sniper V0.7.4</title>
<style>
:root{--bg:#080b11;--panel:#10151e;--panel2:#0d121a;--line:#26303d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--good:#3fb950;--bad:#f85149;--warn:#d29922}
*{box-sizing:border-box}body{font-family:Inter,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text);margin:0}.wrap{max-width:1440px;margin:auto;padding:26px}h1{margin:0;font-size:29px}.sub{color:var(--muted);margin-top:5px}.verified{color:var(--good);font-weight:700}.grid{display:grid;grid-template-columns:repeat(9,1fr);gap:10px;margin:20px 0}.card,.section{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}.label{font-size:12px;color:var(--muted)}.big{font-size:23px;font-weight:750;margin-top:7px}.chartbox{position:relative;height:320px;margin-top:12px;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px}.chartbox canvas{width:100%;height:100%}.tooltip{display:none;position:absolute;pointer-events:none;background:#161b22;border:1px solid #30363d;padding:8px 10px;border-radius:7px;font-size:12px;z-index:4}.toolbar{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;align-items:center}.buttons{display:flex;gap:5px;flex-wrap:wrap}.buttons button{background:#161b22;color:var(--muted);border:1px solid #30363d;border-radius:7px;padding:6px 9px;cursor:pointer}.buttons button.active{color:var(--text);border-color:var(--accent);background:#10233d}.section{margin-top:18px;overflow:auto}.section h3{margin:2px 0 10px}table{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}th,td{text-align:left;padding:9px;border-bottom:1px solid #222a35}th{color:var(--muted);font-weight:650}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.note{color:var(--muted);font-size:12px;margin-top:5px}select{background:#161b22;color:var(--text);border:1px solid #30363d;border-radius:7px;padding:7px 9px;max-width:360px}
@media(max-width:1100px){.grid{grid-template-columns:repeat(4,1fr)}}@media(max-width:650px){.grid{grid-template-columns:repeat(2,1fr)}.wrap{padding:14px}.chartbox{height:270px}}
</style></head><body><div class="wrap">
<div class="toolbar"><div><h1>Meme Sniper V0.7.4</h1></div><div class="buttons"><a href="/tokens" style="text-decoration:none"><button type="button">Token Explorer</button></a><a href="/command" style="text-decoration:none"><button type="button">Command Centre</button></a><a href="/traders" style="text-decoration:none"><button type="button">Top Traders</button></a></div></div><div class="sub"><span class="verified">● PAPER ONLY</span> · V0.7.4 AGGRESSIVE CHALLENGE · smart-wallet + cluster-confirmed signal lane · real-money execution physically disabled</div>
<div class="grid" id="cards"></div>
<div class="note" id="integrityLine" style="margin:-8px 0 6px 2px"></div>
<div class="note" id="aiStatusLine" style="margin:0 0 12px 2px"></div>
<div class="card">
 <div class="toolbar"><div><b>Aggressive challenge cumulative P/L</b><div class="note">Actual V0.7.4 challenge curve. The chart below is the strict Verified lane, which is why it was blank at $0.</div></div><div class="note" id="challengeChartStatus"></div></div>
 <div class="chartbox"><canvas id="challengeChart"></canvas></div>
</div>
<div class="card" style="margin-top:18px">
 <div class="toolbar"><div><b>Live market candles</b><div class="note">1-minute OHLC candles sampled from the exact DEX pair used by the simulator.</div></div><div><select id="candleToken"><option value="">Collecting candle data…</option></select></div></div>
 <div class="chartbox"><canvas id="candleChart"></canvas></div>
 <div class="note" id="candleStatus">Collecting exact-pair samples.</div>
</div>
<div class="card">
 <div class="toolbar"><div><b>Verified lane cumulative P/L</b><div class="note">Verified results remain deliberately separate from the aggressive paper challenge. Historical data is preserved under All.</div></div>
 <div><div class="buttons" id="strategyButtons"><button data-v="all" class="active">Overall</button><button data-v="copy">Smart wallet</button><button data-v="signal">Signals</button></div><div class="buttons" id="rangeButtons" style="margin-top:5px"><button data-v="forward" class="active">V0.7.4</button><button data-v="1h">1H</button><button data-v="today">Today</button><button data-v="7d">7D</button><button data-v="30d">30D</button><button data-v="all">All</button></div></div></div>
 <div class="chartbox"><canvas id="plChart"></canvas><div id="chartTip" class="tooltip"></div></div>
</div>
<div class="section"><h3>Verified paper-copy execution</h3><table><thead><tr><th>ID</th><th>Wallet</th><th>Token</th><th>Status</th><th>Integrity</th><th>Tier</th><th>Delay</th><th>Entry</th><th>Exit</th><th>P/L</th><th>MFE</th><th>MAE</th><th>Reason</th></tr></thead><tbody id="copyTrades"></tbody></table></div>
<div class="section"><h3>Aggressive challenge trades <span class="warn">(CURRENT V0.7.4 EPOCH · PAPER ONLY)</span></h3><table><thead><tr><th>ID</th><th>Token</th><th>Status</th><th>Integrity</th><th>Score</th><th>Size</th><th>Entry</th><th>Exit</th><th>P/L</th><th>MFE</th><th>MAE</th><th>Reason</th></tr></thead><tbody id="signalTrades"></tbody></table></div>
<div class="section"><h3>AI committee <span class="warn">(OpenAI + Claude · SHADOW ONLY)</span></h3><div class="note">Both models independently receive the same market packet. Their decisions are logged for forward calibration and do not control trades yet.</div><table><thead><tr><th>Signal</th><th>Token</th><th>Pre-entry</th><th>OpenAI</th><th>Conf.</th><th>Edge</th><th>Claude</th><th>Conf.</th><th>Edge</th><th>Consensus</th><th>Actual P/L</th><th>Exit</th></tr></thead><tbody id="aiDecisions"></tbody></table></div>
<div class="section"><h3>Wallet copyability</h3><div class="note">Medians from same-pair forward measurements. Extreme, late or pair-mismatched observations are excluded rather than averaged.</div><table><thead><tr><th>Wallet</th><th>Copy score</th><th>Tier</th><th>Obs</th><th>Excluded</th><th>Edge</th><th>HFT penalty</th><th>10s median</th><th>30s median</th><th>60s median</th><th>5m median</th><th>+10% hit</th><th>Lead</th></tr></thead><tbody id="copyability"></tbody></table></div>
<div class="section"><h3>Smart-wallet leaderboard</h3><div class="note" id="birdeyeStatus"></div><table><thead><tr><th>Wallet</th><th>Policy</th><th>Tracked</th><th>Birdeye</th><th>Tier</th><th>30d win</th><th>30d realised</th><th>Trades</th><th>Seen</th><th>Copy</th><th>Copy tier</th></tr></thead><tbody id="wallets"></tbody></table></div>
<div class="section"><h3>Verified wallet swaps</h3><table><thead><tr><th>Time</th><th>Wallet</th><th>Action</th><th>Token</th><th>Token Δ</th><th>Quote Δ</th><th>Copy eligible</th><th>Integrity</th></tr></thead><tbody id="swaps"></tbody></table></div>
<div class="section"><h3>Latest verified-epoch signals</h3><table><thead><tr><th>Time</th><th>Token</th><th>Score</th><th>Decision</th><th>Momentum</th><th>Liquidity</th><th>Flow</th><th>Wallet</th></tr></thead><tbody id="signals"></tbody></table></div>
<div class="section"><div class="toolbar"><div><h3>Latest discovered tokens</h3><div class="note">Quick view. Open Token Explorer for logos, market cap, age, volume and market links.</div></div><div class="buttons"><a href="/tokens" style="text-decoration:none"><button type="button">Open Token Explorer</button></a></div></div><table><thead><tr><th>Symbol</th><th>Mint</th><th>Price</th><th>Liquidity</th><th>5m</th><th>5m B/S</th><th>Pair</th></tr></thead><tbody id="tokens"></tbody></table></div>
</div><script>
const short=x=>x?x.slice(0,5)+'…'+x.slice(-5):'—';
const money=x=>{if(x==null)return '—';const n=Number(x);return (n<0?'-$':'$')+Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:4})};
const pct=x=>x==null?'—':(Number(x)>=0?'+':'')+Number(x).toFixed(2)+'%';
const perth=t=>new Date(t).toLocaleTimeString('en-AU',{timeZone:'Australia/Perth',hour12:false});
let strategy='all', range='forward', chartData=[], challengeData=[], candleData=[], candleMint='';
function setButtons(id, value){document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('active',b.dataset.v===value));}
document.querySelectorAll('#strategyButtons button').forEach(b=>b.onclick=()=>{strategy=b.dataset.v;setButtons('strategyButtons',strategy);refreshPerformance()});
document.querySelectorAll('#rangeButtons button').forEach(b=>b.onclick=()=>{range=b.dataset.v;setButtons('rangeButtons',range);refreshPerformance()});

function drawChallengeChart(){
 const canvas=document.getElementById('challengeChart'),box=canvas.parentElement,dpr=window.devicePixelRatio||1;
 const w=box.clientWidth-16,h=box.clientHeight-16;canvas.width=w*dpr;canvas.height=h*dpr;canvas.style.width=w+'px';canvas.style.height=h+'px';
 const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);const pad={l:58,r:18,t:18,b:34};
 const vals=challengeData.map(x=>Number(x.cumulative_pnl));vals.push(0);let ymin=Math.min(...vals),ymax=Math.max(...vals);if(ymin===ymax){ymin-=1;ymax+=1}const span=ymax-ymin;ymin-=span*.08;ymax+=span*.08;
 ctx.font='11px Segoe UI';ctx.strokeStyle='#26303d';ctx.fillStyle='#8b949e';ctx.lineWidth=1;
 for(let i=0;i<=4;i++){const yy=pad.t+(h-pad.t-pad.b)*i/4;ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();const v=ymax-(ymax-ymin)*i/4;ctx.fillText((v<0?'-$':'$')+Math.abs(v).toFixed(0),5,yy+4)}
 if(!challengeData.length){ctx.fillText('Waiting for current-epoch closed challenge trades.',pad.l+20,h/2);return}
 const x=i=>challengeData.length===1?(pad.l+w-pad.r)/2:pad.l+(w-pad.l-pad.r)*i/(challengeData.length-1),y=v=>pad.t+(ymax-v)/(ymax-ymin)*(h-pad.t-pad.b);
 ctx.strokeStyle=challengeData[challengeData.length-1].cumulative_pnl>=0?'#3fb950':'#f85149';ctx.lineWidth=2;ctx.beginPath();challengeData.forEach((p,i)=>{const xx=x(i),yy=y(Number(p.cumulative_pnl));i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy)});ctx.stroke();
}

function drawCandles(){
 const canvas=document.getElementById('candleChart'),box=canvas.parentElement,dpr=window.devicePixelRatio||1;
 const w=box.clientWidth-16,h=box.clientHeight-16;canvas.width=w*dpr;canvas.height=h*dpr;canvas.style.width=w+'px';canvas.style.height=h+'px';
 const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);const pad={l:72,r:18,t:18,b:34};
 if(!candleData.length){ctx.fillStyle='#8b949e';ctx.font='14px Segoe UI';ctx.fillText('Collecting 1-minute OHLC samples…',pad.l+20,h/2);return}
 let lo=Math.min(...candleData.map(x=>Number(x.l))),hi=Math.max(...candleData.map(x=>Number(x.h)));if(lo===hi){lo*=.999;hi*=1.001}const span=hi-lo;lo-=span*.05;hi+=span*.05;
 const y=v=>pad.t+(hi-v)/(hi-lo)*(h-pad.t-pad.b),step=(w-pad.l-pad.r)/Math.max(candleData.length,1),body=Math.max(2,Math.min(9,step*.58));
 ctx.font='10px Segoe UI';ctx.strokeStyle='#26303d';ctx.fillStyle='#8b949e';
 for(let i=0;i<=4;i++){const yy=pad.t+(h-pad.t-pad.b)*i/4;ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();const v=hi-(hi-lo)*i/4;ctx.fillText(v<0.001?v.toExponential(2):v.toFixed(6),4,yy+3)}
 candleData.forEach((p,i)=>{const x=pad.l+step*(i+.5),up=Number(p.c)>=Number(p.o),col=up?'#3fb950':'#f85149';ctx.strokeStyle=col;ctx.fillStyle=col;ctx.beginPath();ctx.moveTo(x,y(Number(p.h)));ctx.lineTo(x,y(Number(p.l)));ctx.stroke();const top=y(Math.max(Number(p.o),Number(p.c))),bottom=y(Math.min(Number(p.o),Number(p.c)));ctx.fillRect(x-body/2,top,body,Math.max(1,bottom-top))});
 ctx.fillStyle='#8b949e';if(candleData.length){ctx.fillText(perth(candleData[0].t),pad.l,h-9);const last=perth(candleData[candleData.length-1].t);ctx.fillText(last,w-pad.r-ctx.measureText(last).width,h-9)}
}

async function refreshChallengeChart(){
 const p=await fetch('/api/challenge-curve').then(r=>r.json());challengeData=p.curve||[];drawChallengeChart();document.getElementById('challengeChartStatus').textContent=(p.closed||0)+' closed · '+money(p.realized_pnl||0);
}

async function refreshCandleChart(){
 const watch=await fetch('/api/candle-watchlist?limit=24').then(r=>r.json());
 const sel=document.getElementById('candleToken'),previous=candleMint||sel.value;
 if(watch.length){
   sel.innerHTML=watch.map(x=>`<option value="${x.mint}">${x.symbol||short(x.mint)} · ${short(x.mint)}</option>`).join('');
   candleMint=watch.some(x=>x.mint===previous)?previous:watch[0].mint;sel.value=candleMint;
   candleData=await fetch('/api/candles?mint='+encodeURIComponent(candleMint)+'&limit=120').then(r=>r.json());
   document.getElementById('candleStatus').textContent=`${candleData.length} one-minute candles · exact-pair sampled market data · ${short(candleMint)}`;
 }else{candleData=[];document.getElementById('candleStatus').textContent='Collecting the first exact-pair candles now.'}
 drawCandles();
}

function drawChart(){
 const canvas=document.getElementById('plChart'), box=canvas.parentElement, dpr=window.devicePixelRatio||1;
 const w=box.clientWidth-16,h=box.clientHeight-16; canvas.width=w*dpr;canvas.height=h*dpr;canvas.style.width=w+'px';canvas.style.height=h+'px';
 const c=canvas.getContext('2d');c.scale(dpr,dpr);c.clearRect(0,0,w,h);const pad={l:58,r:18,t:18,b:34};
 const vals=chartData.map(x=>x.cumulative_pnl); vals.push(0); let ymin=Math.min(...vals),ymax=Math.max(...vals); if(ymin===ymax){ymin-=1;ymax+=1} const span=ymax-ymin; ymin-=span*.12;ymax+=span*.12;
 c.font='11px Segoe UI';c.strokeStyle='#26303d';c.fillStyle='#8b949e';c.lineWidth=1;
 for(let i=0;i<=4;i++){let y=pad.t+(h-pad.t-pad.b)*i/4;c.beginPath();c.moveTo(pad.l,y);c.lineTo(w-pad.r,y);c.stroke();let v=ymax-(ymax-ymin)*i/4;c.fillText((v<0?'-$':'$')+Math.abs(v).toFixed(0),5,y+4)}
 const y0=pad.t+(ymax/(ymax-ymin))*(h-pad.t-pad.b);c.strokeStyle='#4b5563';c.beginPath();c.moveTo(pad.l,y0);c.lineTo(w-pad.r,y0);c.stroke();
 if(!chartData.length){c.fillStyle='#8b949e';c.font='14px Segoe UI';c.fillText('No verified closed trades in this view yet.',pad.l+20,h/2);return}
 const x=i=>chartData.length===1?(pad.l+w-pad.r)/2:pad.l+(w-pad.l-pad.r)*i/(chartData.length-1); const y=v=>pad.t+(ymax-v)/(ymax-ymin)*(h-pad.t-pad.b);
 c.strokeStyle='#58a6ff';c.lineWidth=2;c.beginPath();chartData.forEach((p,i)=>{let xx=x(i),yy=y(p.cumulative_pnl);if(i===0)c.moveTo(xx,yy);else c.lineTo(xx,yy)});c.stroke();
 c.fillStyle='#8b949e';c.font='11px Segoe UI';c.fillText(new Date(chartData[0].ts).toLocaleString('en-AU',{timeZone:'Australia/Perth'}),pad.l,h-9);let last=new Date(chartData[chartData.length-1].ts).toLocaleString('en-AU',{timeZone:'Australia/Perth'});let tw=c.measureText(last).width;c.fillText(last,w-pad.r-tw,h-9);
 canvas._geom={x,y,w,h,pad};
}
const canvas=document.getElementById('plChart'),tip=document.getElementById('chartTip');
canvas.addEventListener('mousemove',e=>{if(!chartData.length||!canvas._geom)return;const r=canvas.getBoundingClientRect(),mx=e.clientX-r.left,g=canvas._geom;let idx=chartData.length===1?0:Math.round((mx-g.pad.l)/(g.w-g.pad.l-g.pad.r)*(chartData.length-1));idx=Math.max(0,Math.min(chartData.length-1,idx));const p=chartData[idx];tip.style.display='block';tip.style.left=Math.min(mx+12,r.width-210)+'px';tip.style.top=Math.max(8,e.clientY-r.top-60)+'px';tip.innerHTML=`<b>${money(p.cumulative_pnl)}</b> cumulative<br>${p.strategy}: ${p.trade_pnl>=0?'+':''}${money(p.trade_pnl)}<br>${short(p.token)} · ${new Date(p.ts).toLocaleString('en-AU',{timeZone:'Australia/Perth'})}`});canvas.addEventListener('mouseleave',()=>tip.style.display='none');window.addEventListener('resize',()=>{drawChart();drawChallengeChart();drawCandles()});document.getElementById('candleToken').addEventListener('change',e=>{candleMint=e.target.value;refreshCandleChart()});

async function refreshPerformance(){const p=await fetch(`/api/performance?strategy=${strategy}&range=${range}`).then(r=>r.json());chartData=p.curve||[];drawChart();}
async function refreshAll(){
 const [o,t,s,w,c,sw,pc,st,bs,ai,xs]=await Promise.all([
 fetch('/api/overview').then(r=>r.json()),fetch('/api/tokens?limit=18').then(r=>r.json()),fetch('/api/signals?limit=18').then(r=>r.json()),fetch('/api/smart-wallets?limit=25').then(r=>r.json()),fetch('/api/copyability?limit=25').then(r=>r.json()),fetch('/api/wallet-swaps?limit=25').then(r=>r.json()),fetch('/api/paper-copy-trades?limit=25').then(r=>r.json()),fetch('/api/signal-trades?limit=25').then(r=>r.json()),fetch('/api/birdeye-status').then(r=>r.json()),fetch('/api/ai-ensemble?limit=25').then(r=>r.json()),fetch('/api/x-social-status').then(r=>r.json())]);
 document.getElementById('cards').innerHTML=[['Challenge equity P/L',money(o.shadow_signal.equity_pnl_usd)],['Realized',money(o.shadow_signal.pnl_usd)],['Open challenge',o.shadow_signal.open_trades],['Deployed',money(o.shadow_signal.open_notional_usd)],['Closed trades',o.shadow_signal.trades],['Challenge win',o.shadow_signal.win_rate_pct+'%'],['Challenge PF',o.shadow_signal.profit_factor??'—'],['Verified P/L',money(o.forward_pnl_usd)],['Wallets',o.tracked_wallets]].map(x=>`<div class="card"><div class="label">${x[0]}</div><div class="big">${x[1]}</div></div>`).join('');
 const g=o.challenge_gate||{}; document.getElementById('integrityLine').textContent=`AGGRESSIVE PAPER CHALLENGE: ${o.shadow_signal.open_trades} open / ${o.shadow_signal.trades} closed · realized ${money(o.shadow_signal.pnl_usd)} · unrealized ${money(o.shadow_signal.unrealized_pnl_usd)} · equity P/L ${money(o.shadow_signal.equity_pnl_usd)} · gates: ${g.market_eligible??0} market-ready, ${g.queued??0} queued, ${g.no_confirmation??0} no-confirm, ${g.score_below_entry??0} low-score, ${g.token_cooldown??0} cooldown · real-money OFF.`;
 const aiReady=(ai.openai_configured?'OpenAI '+ai.openai_model:'OpenAI NEEDS KEY')+' · '+(ai.claude_configured?'Claude '+ai.claude_model:'Claude NEEDS KEY');const xReady=xs.configured?'X SOCIAL ACTIVE':'X SOCIAL NEEDS BEARER TOKEN'; document.getElementById('aiStatusLine').textContent=`AI COMMITTEE: ${aiReady} · ${ai.total_decisions} decisions · ${ai.both_models_ok} dual-model responses · AI ENTRY GATE ${ai.ai_trade_gate?'ON':'OFF'} · ${xReady} · ${xs.covered_tokens||0} tokens enriched.`;
 document.getElementById('copyTrades').innerHTML=pc.map(x=>`<tr><td>${x.id}</td><td class="mono">${short(x.wallet)}</td><td class="mono">${short(x.mint)}</td><td>${x.status}</td><td>${x.integrity}</td><td>${x.copy_tier||'—'}</td><td>${x.delay_s==null?'—':Number(x.delay_s).toFixed(1)+'s'}</td><td>${money(x.entry)}</td><td>${money(x.exit)}</td><td class="${(x.pnl_usd||0)>=0?'good':'bad'}">${x.pnl_pct==null?'—':pct(x.pnl_pct)}</td><td>${x.mfe==null?'—':pct(x.mfe)}</td><td>${x.mae==null?'—':pct(x.mae)}</td><td>${x.reason||'—'}</td></tr>`).join('');
 document.getElementById('signalTrades').innerHTML=st.map(x=>`<tr><td>${x.id}</td><td class="mono">${short(x.mint)}</td><td>${x.status}</td><td>${x.integrity}</td><td>${x.score}</td><td>${money(x.notional_usd)}</td><td>${money(x.entry)}</td><td>${money(x.exit)}</td><td class="${(x.pnl_usd||0)>=0?'good':'bad'}">${x.pnl_pct==null?'—':pct(x.pnl_pct)}</td><td>${x.mfe==null?'—':pct(x.mfe)}</td><td>${x.mae==null?'—':pct(x.mae)}</td><td>${x.reason||'—'}</td></tr>`).join('');
 document.getElementById('aiDecisions').innerHTML=(ai.rows||[]).map(x=>`<tr><td>${x.signal_id}</td><td class="mono">${short(x.mint)}</td><td>${x.pre_entry?'YES':'NO'}</td><td>${x.openai_status==='ok'?(x.openai_verdict||'—'):x.openai_status}</td><td>${x.openai_confidence==null?'—':Number(x.openai_confidence).toFixed(0)+'%'}</td><td>${x.openai_edge==null?'—':pct(x.openai_edge)}</td><td>${x.claude_status==='ok'?(x.claude_verdict||'—'):x.claude_status}</td><td>${x.claude_confidence==null?'—':Number(x.claude_confidence).toFixed(0)+'%'}</td><td>${x.claude_edge==null?'—':pct(x.claude_edge)}</td><td>${x.consensus||'—'}</td><td class="${(x.trade_pnl_pct||0)>=0?'good':'bad'}">${x.trade_pnl_pct==null?'—':pct(x.trade_pnl_pct)}</td><td>${x.trade_reason||x.trade_status||'—'}</td></tr>`).join('');
 document.getElementById('copyability').innerHTML=c.map(x=>`<tr><td class="mono">${short(x.wallet)}</td><td>${x.score}</td><td>${x.tier}</td><td>${x.observations}</td><td>${x.excluded}</td><td>${x.edge}</td><td>${x.hft_penalty}</td><td>${pct(x.r10)}</td><td>${pct(x.r30)}</td><td>${pct(x.r60)}</td><td>${pct(x.r300)}</td><td>${x.target_hit==null?'—':x.target_hit+'%'}</td><td>${x.lead==null?'—':x.lead+'s'}</td></tr>`).join('');
 document.getElementById('birdeyeStatus').textContent=bs.enabled?`Birdeye active · scans today ${bs.scans_today}/${bs.scan_limit} · estimated ${bs.estimated_cu_today} CU`:'Birdeye disabled';
 document.getElementById('wallets').innerHTML=w.map(x=>`<tr><td class="mono">${short(x.wallet)}</td><td>${x.policy_score??'—'}</td><td>${x.tracked?'YES':'NO'}</td><td>${x.birdeye_score}</td><td>${x.birdeye_tier}</td><td>${x.win_rate_pct==null?'—':x.win_rate_pct+'%'}</td><td>${money(x.realized_pnl_usd_30d)}</td><td>${x.total_trades_30d??'—'}</td><td>${x.seen}</td><td>${x.copy_score??'—'}</td><td>${x.copy_tier??'—'}</td></tr>`).join('');
 document.getElementById('swaps').innerHTML=sw.map(x=>`<tr><td>${perth(x.ts)}</td><td class="mono">${short(x.wallet)}</td><td>${x.action}</td><td class="mono">${short(x.mint)}</td><td>${Number(x.token_delta).toLocaleString(undefined,{maximumFractionDigits:4})}</td><td>${x.quote_delta==null?'—':Number(x.quote_delta).toFixed(4)}</td><td>${x.copy_eligible?'YES':'NO'}</td><td>${x.integrity}${x.invalid_reason?' · '+x.invalid_reason:''}</td></tr>`).join('');
 document.getElementById('signals').innerHTML=s.map(x=>`<tr><td>${perth(x.ts)}</td><td class="mono">${short(x.mint)}</td><td>${x.score}</td><td>${x.decision}</td><td>${x.momentum}</td><td>${x.liquidity}</td><td>${x.flow}</td><td>${x.wallet}</td></tr>`).join('');
 document.getElementById('tokens').innerHTML=t.map(x=>`<tr><td>${x.symbol||'—'}</td><td class="mono">${short(x.mint)}</td><td>${money(x.price_usd)}</td><td>${money(x.liquidity_usd)}</td><td>${x.price_change_m5_pct==null?'—':pct(x.price_change_m5_pct)}</td><td>${x.buys_m5??'—'} / ${x.sells_m5??'—'}</td><td class="mono">${short(x.pair_address)}</td></tr>`).join('');
 await Promise.all([refreshPerformance(),refreshChallengeChart(),refreshCandleChart()]);
}
refreshAll();setInterval(refreshAll,5000);
</script></body></html>'''



TOKEN_EXPLORER_PAGE = r'''<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meme Sniper V0.7.4 - Token Explorer</title>
<style>
:root{--bg:#14161c;--panel:#1e2128;--head:#3a3c43;--line:#30343c;--text:#f1f3f5;--muted:#8f939b;--good:#39d98a;--bad:#ff5c64;--accent:#4fb3ff;--orange:#ff9d18}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif}
.wrap{max-width:1780px;margin:auto;padding:18px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:15px}
h1{font-size:27px;margin:0}.sub{color:var(--muted);margin-top:5px;font-size:13px}.actions{display:flex;gap:8px}
.btn{display:inline-flex;align-items:center;text-decoration:none;background:#242832;border:1px solid #3a404b;color:#d7dce3;padding:8px 12px;border-radius:8px;font-size:13px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
table{width:100%;border-collapse:collapse;table-layout:auto}thead{background:var(--head);position:sticky;top:0;z-index:3}
th{font-size:12px;text-align:right;padding:13px 11px;color:#f4f5f6;white-space:nowrap}
th:first-child{text-align:left}td{border-top:1px solid var(--line);padding:9px 11px;text-align:right;white-space:nowrap;font-size:13px}
td:first-child{text-align:left}.tokenCell{display:flex;align-items:center;gap:10px;min-width:310px}.rank{width:26px;color:#70747d;text-align:right}
.logo{width:38px;height:38px;border-radius:7px;object-fit:cover;background:#2d323c;border:1px solid #414752;flex:0 0 38px}
.logoFallback{width:38px;height:38px;border-radius:7px;background:#2d323c;border:1px solid #414752;display:flex;align-items:center;justify-content:center;font-weight:800;color:#9ca3af;flex:0 0 38px}
.tokenText{min-width:0}.tokenLine{display:flex;align-items:center;gap:7px;min-width:0}.symbol{font-weight:800;color:var(--orange);font-size:14px}.name{color:#f0f1f2;overflow:hidden;text-overflow:ellipsis;max-width:210px}
.meta{font-size:11px;color:#737780;margin-top:3px}.chip{padding:2px 5px;border-radius:4px;background:#2b3039;color:#9da3ad;font-size:10px}
.numBlue{color:#55b7ff}.good{color:var(--good)}.bad{color:var(--bad)}.muted{color:var(--muted)}
.links{display:flex;justify-content:flex-end;gap:5px}.link{font-size:10px;text-decoration:none;padding:4px 6px;border-radius:5px;background:#292e37;border:1px solid #3a404a;color:#cdd2d8}
.note{font-size:12px;color:var(--muted);margin:10px 2px 14px}.status{font-size:12px;color:var(--muted)}
@media(max-width:1050px){.wrap{padding:8px}.panel{overflow-x:auto}table{min-width:1250px}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Meme Sniper V0.7.4 - Token Explorer</h1><div class="sub">Live market context for coins Meme Sniper has discovered. Trading remains VERIFIED PAPER ONLY.</div></div><div class="actions"><a class="btn" href="/">Dashboard</a><a class="btn" href="/command">Command Centre</a><a class="btn" href="/traders">Top Traders</a><a class="btn" href="https://dexscreener.com/solana" target="_blank" rel="noopener noreferrer">DEX Screener</a></div></div>
<div class="note">Rows are ordered by Meme Sniper discovery time. Market metadata is enrichment only and cannot override the verified pair used by the trading engine. TXNS = 24h buys + sells. Unique trader counts are intentionally not fabricated because this DEX Screener endpoint does not provide them.</div>
<div class="panel"><table><thead><tr>
<th>TOKEN</th><th>SOURCE</th><th>MCAP</th><th>PRICE</th><th>AGE</th><th>TXNS</th><th>VOLUME</th><th>LIQUIDITY</th><th>5M</th><th>1H</th><th>6H</th><th>LINKS</th>
</tr></thead><tbody id="explorer"><tr><td colspan="12" class="muted">Loading discovered tokens...</td></tr></tbody></table></div>
<div class="status" id="status" style="margin-top:10px"></div>
</div><script>
const compact=n=>{if(n==null)return '—';const v=Number(n);return '$'+Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:1}).format(v)};
const price=n=>{if(n==null)return '—';const v=Number(n);if(v===0)return '$0';if(v<0.0001)return '$'+v.toExponential(3);if(v<1)return '$'+v.toFixed(6);return '$'+v.toLocaleString(undefined,{maximumFractionDigits:6})};
const pct=n=>n==null?'—':(Number(n)>=0?'+':'')+Number(n).toFixed(2)+'%';
const pctClass=n=>n==null?'muted':Number(n)>=0?'good':'bad';
const age=s=>{if(s==null)return '—';s=Number(s);if(s<60)return Math.max(1,Math.floor(s))+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';if(s<86400*60)return Math.floor(s/86400)+'d';return Math.floor(s/(86400*30))+'mo'};
const short=x=>x?x.slice(0,5)+'…'+x.slice(-5):'—';
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]))}
function tokenLogo(x){
 const initial=esc((x.symbol||x.name||'?').slice(0,1).toUpperCase());
 if(!x.image_url)return `<div class="logoFallback">${initial}</div>`;
 return `<img class="logo" src="${esc(x.image_url)}" referrerpolicy="no-referrer" loading="lazy" onerror="this.outerHTML='<div class=&quot;logoFallback&quot;>${initial}</div>'">`;
}
function links(x){
 let a=[];
 if(x.dexscreener_url)a.push(`<a class="link" target="_blank" rel="noopener noreferrer" href="${esc(x.dexscreener_url)}">DEX</a>`);
 if(x.pump_url)a.push(`<a class="link" target="_blank" rel="noopener noreferrer" href="${esc(x.pump_url)}">PUMP</a>`);
 if(x.solscan_url)a.push(`<a class="link" target="_blank" rel="noopener noreferrer" href="${esc(x.solscan_url)}">SOLSCAN</a>`);
 return a.join('');
}
async function refresh(){
 try{
   const rows=await fetch('/api/token-explorer?limit=30').then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()});
   document.getElementById('explorer').innerHTML=rows.length?rows.map(x=>`<tr>
   <td><div class="tokenCell"><span class="rank">#${x.rank}</span>${tokenLogo(x)}<div class="tokenText"><div class="tokenLine"><span class="symbol">${esc(x.symbol||'—')}</span><span class="name">${esc(x.name||'Unknown token')}</span></div><div class="meta">${esc(short(x.mint))} · ${esc(x.dex_id||'pair pending')}</div></div></div></td>
   <td><span class="chip">${esc(x.origin||'Unknown')}</span></td>
   <td class="numBlue">${compact(x.market_cap??x.fdv)}</td>
   <td>${price(x.price_usd)}</td>
   <td class="${x.age_seconds!=null&&x.age_seconds<86400?'good':''}">${age(x.age_seconds)}</td>
   <td>${x.txns_h24==null?'—':Number(x.txns_h24).toLocaleString()}</td>
   <td>${compact(x.volume_h24_usd)}</td>
   <td>${compact(x.liquidity_usd)}</td>
   <td class="${pctClass(x.change_m5_pct)}">${pct(x.change_m5_pct)}</td>
   <td class="${pctClass(x.change_h1_pct)}">${pct(x.change_h1_pct)}</td>
   <td class="${pctClass(x.change_h6_pct)}">${pct(x.change_h6_pct)}</td>
   <td><div class="links">${links(x)}</div></td>
   </tr>`).join(''):'<tr><td colspan="12" class="muted">No discovered tokens yet.</td></tr>';
   document.getElementById('status').textContent=`${rows.length} recent tokens · updated ${new Date().toLocaleTimeString()}`;
 }catch(e){document.getElementById('status').textContent='Explorer refresh failed: '+e.message}
}
refresh();setInterval(refresh,15000);
</script></body></html>'''



@app.get("/integrations", response_class=HTMLResponse)
async def integrations_page():
    return HTMLResponse(_brand_html(INTEGRATIONS_PAGE))


@app.get("/command", response_class=HTMLResponse)
async def command_page():
    return _brand_html(COMMAND_PAGE)

@app.get("/traders", response_class=HTMLResponse)
async def traders_page():
    return _brand_html(TRADERS_PAGE)


@app.get("/tokens", response_class=HTMLResponse)
async def token_explorer_page():
    return _brand_html(TOKEN_EXPLORER_PAGE)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return _brand_html(DASHBOARD)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
