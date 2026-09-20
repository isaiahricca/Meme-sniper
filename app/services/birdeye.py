import asyncio
import json
import logging
import math
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, func

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    Token, Signal, TrackedWallet, BirdeyeTokenScan,
    WalletDiscovery, SmartWalletProfile, SystemState
)
from app.services.event_store import store_event

log = logging.getLogger("birdeye")
BASE = "https://public-api.birdeye.so"


def _num(value, default=0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_win_rate(value) -> float | None:
    if value is None:
        return None
    x = _num(value, -1)
    if x < 0:
        return None
    if x <= 1.0:
        x *= 100.0
    return max(0.0, min(100.0, x))


def _data_block(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def parse_top_traders(payload: dict) -> list[dict]:
    data = _data_block(payload)
    items = data.get("items") or []
    out = []
    seen_owners = set()
    if not isinstance(items, list):
        return out
    for i, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") or item.get("wallet") or item.get("address")
        if not owner or str(owner) in seen_owners:
            continue
        seen_owners.add(str(owner))
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        out.append({
            "owner": str(owner),
            "rank": i,
            "realized_pnl": _num(item.get("realizedPnl") or item.get("realized_pnl")),
            "total_pnl": _num(item.get("totalPnl") or item.get("total_pnl")),
            "volume_usd": _num(item.get("volumeUsd") or item.get("volumeUSD") or item.get("volume_usd")),
            "trade": _integer(item.get("trade") or item.get("trades")),
            "tags": [str(x).lower() for x in tags],
        })
    return out


def _wallet_summary_blocks(payload: dict) -> list[dict]:
    """Return plausible summary blocks across Birdeye response-shape versions."""
    blocks: list[dict] = []
    if not isinstance(payload, dict):
        return blocks

    def add(value):
        if isinstance(value, dict) and value not in blocks:
            blocks.append(value)

    add(payload)
    data = payload.get("data")
    add(data)
    if isinstance(data, dict):
        add(data.get("summary"))
        add(data.get("result"))
        nested = data.get("data")
        add(nested)
        if isinstance(nested, dict):
            add(nested.get("summary"))
    add(payload.get("summary"))
    add(payload.get("result"))
    return blocks


def _first_present(blocks: list[dict], nested_key: str, keys: tuple[str, ...]):
    for block in blocks:
        nested = block.get(nested_key)
        if isinstance(nested, dict):
            for key in keys:
                if key in nested and nested[key] is not None:
                    return nested[key]
        for key in keys:
            if key in block and block[key] is not None:
                return block[key]
    return None


def parse_wallet_summary(payload: dict) -> dict:
    blocks = _wallet_summary_blocks(payload)

    win_rate = _first_present(
        blocks, "counts", ("win_rate", "winRate", "winning_rate")
    )
    total_trade = _first_present(
        blocks, "counts", ("total_trade", "totalTrade", "total_trades", "trades")
    )
    realized = _first_present(
        blocks, "pnl",
        ("realized_profit_usd", "realizedProfitUsd", "realized_pnl_usd", "realizedPnlUsd")
    )
    total = _first_present(
        blocks, "pnl",
        ("total_usd", "totalUsd", "total_pnl_usd", "totalPnlUsd")
    )

    recognized = any(v is not None for v in (win_rate, total_trade, realized, total))
    return {
        "win_rate_pct": _normalize_win_rate(win_rate),
        "total_trades": _integer(total_trade),
        "realized_pnl_usd": _num(realized),
        "total_pnl_usd": _num(total),
        "_recognized": recognized,
    }


def _log_bonus(value: float, scale: float, cap: float) -> float:
    if value <= 0:
        return 0.0
    return min(math.log10(value + 1.0) * scale, cap)


def score_wallet(
    *,
    token_realized_pnl: float,
    token_total_pnl: float,
    token_volume_usd: float,
    token_trade_count: int,
    tags: list[str],
    win_rate_pct: float | None,
    wallet_realized_pnl: float,
    wallet_total_pnl: float,
    wallet_trade_count: int,
    discovery_count: int,
) -> float:
    score = 42.0

    # Repeated discovery across independent tokens matters more than one lucky token.
    score += min(max(discovery_count - 1, 0) * 4.0, 16.0)

    if win_rate_pct is not None:
        score += (win_rate_pct - 50.0) * 0.28

    if wallet_realized_pnl >= 0:
        score += _log_bonus(wallet_realized_pnl, 4.0, 18.0)
    else:
        score -= _log_bonus(abs(wallet_realized_pnl), 5.0, 22.0)

    if wallet_total_pnl < 0:
        score -= _log_bonus(abs(wallet_total_pnl), 3.0, 12.0)

    if token_realized_pnl >= 0:
        score += _log_bonus(token_realized_pnl, 2.5, 10.0)
    else:
        score -= _log_bonus(abs(token_realized_pnl), 3.0, 12.0)

    score += _log_bonus(token_volume_usd, 1.2, 7.0)
    score += min(max(token_trade_count, 0) / 20.0, 4.0)
    score += min(max(wallet_trade_count, 0) / 100.0, 4.0)

    tagset = {t.lower() for t in tags}
    if any("bundler" in t for t in tagset):
        score -= 18.0
    if any(t == "dev" or "developer" in t for t in tagset):
        score -= 18.0
    if any("insider" in t for t in tagset):
        score -= 14.0
    if any("wash" in t for t in tagset):
        score -= 20.0
    # A sniper tag is not automatically bad for this use case; it is evidence,
    # not a moral diagnosis. We leave it neutral.

    return round(max(0.0, min(100.0, score)), 2)


def tier_for(score: float) -> str:
    if score >= 85:
        return "ELITE"
    if score >= 72:
        return "STRONG"
    if score >= 60:
        return "TESTING"
    return "IGNORE"


class BirdeyeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self.headers = {
            "accept": "application/json",
            "X-API-KEY": settings.birdeye_api_key,
            "x-chain": "solana",
        }

    async def _wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self.settings.birdeye_min_request_interval_seconds - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    async def validate(self, client: httpx.AsyncClient) -> bool:
        await self._wait_turn()
        r = await client.get(f"{BASE}/defi/networks", headers={
            "accept": "application/json",
            "X-API-KEY": self.settings.birdeye_api_key,
        }, timeout=12)
        r.raise_for_status()
        payload = r.json()
        return bool(payload.get("success"))

    async def top_traders(self, client: httpx.AsyncClient, mint: str) -> list[dict]:
        await self._wait_turn()
        r = await client.get(
            f"{BASE}/defi/v2/tokens/top_traders",
            headers=self.headers,
            params={
                "address": mint,
                "time_frame": "24h",
                "sort_by": "realized_pnl",
                "sort_type": "desc",
                "limit": 10,
                "min_trade": 2,
            },
            timeout=15,
        )
        r.raise_for_status()
        return parse_top_traders(r.json())

    async def wallet_summary(self, client: httpx.AsyncClient, wallet: str) -> dict:
        await self._wait_turn()
        r = await client.get(
            f"{BASE}/wallet/v2/pnl/summary",
            headers=self.headers,
            params={
                "wallet": wallet,
                "duration": "30d",
                "position_scope": "duration_only",
                "pnl_method": "wac",
            },
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        parsed = parse_wallet_summary(payload)
        if not parsed.get("_recognized"):
            data = payload.get("data") if isinstance(payload, dict) else None
            data_keys = list(data.keys())[:20] if isinstance(data, dict) else []
            log.warning(
                "Birdeye wallet summary schema not recognised for %s; data keys=%s",
                wallet[:8], data_keys
            )
        return parsed


async def _scans_today(session) -> int:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        await session.execute(
            select(func.count(BirdeyeTokenScan.token_mint))
            .where(BirdeyeTokenScan.scanned_at >= start)
        )
    ).scalar_one()


async def _next_candidate_token(settings: Settings):
    async with SessionLocal() as session:
        if await _scans_today(session) >= settings.birdeye_token_scan_limit_per_day:
            return None

        # Latest VERIFIED-epoch high-quality signals only. Legacy V0.5 rows are
        # intentionally excluded from new smart-money discovery.
        stmt = select(Signal).where(Signal.total_score >= settings.birdeye_discovery_score)
        state = await session.get(SystemState, "v06_verified_epoch")
        if state is not None:
            epoch = datetime.fromisoformat(state.value)
            if epoch.tzinfo is None:
                epoch = epoch.replace(tzinfo=timezone.utc)
            stmt = stmt.where(Signal.ts >= epoch)
        result = await session.execute(stmt.order_by(Signal.ts.desc()).limit(100))
        for sig in result.scalars():
            token = await session.get(Token, sig.token_mint)
            if not token or not token.liquidity_usd:
                continue
            if token.liquidity_usd < settings.birdeye_min_liquidity_usd:
                continue
            if await session.get(BirdeyeTokenScan, token.mint) is not None:
                continue
            return token.mint, sig.total_score, token.liquidity_usd
    return None


async def _discovery_count(session, wallet: str) -> int:
    return (
        await session.execute(
            select(func.count(func.distinct(WalletDiscovery.token_mint)))
            .where(WalletDiscovery.wallet == wallet)
        )
    ).scalar_one()


async def _tracked_count(session) -> int:
    return (
        await session.execute(
            select(func.count(TrackedWallet.address))
            .where(TrackedWallet.enabled.is_(True))
        )
    ).scalar_one()


async def _profile_and_store(
    settings: Settings,
    client_api: BirdeyeClient,
    http: httpx.AsyncClient,
    mint: str,
    trader: dict,
) -> tuple[float, str]:
    wallet = trader["owner"]

    summary = await client_api.wallet_summary(http, wallet)

    async with SessionLocal() as session:
        # WalletDiscovery for this token was inserted before profiling, so the
        # current discovery is already included. Do not add one twice.
        discoveries = await _discovery_count(session, wallet)
        discovery_count = max(int(discoveries or 0), 1)
        score = score_wallet(
            token_realized_pnl=trader["realized_pnl"],
            token_total_pnl=trader["total_pnl"],
            token_volume_usd=trader["volume_usd"],
            token_trade_count=trader["trade"],
            tags=trader["tags"],
            win_rate_pct=summary["win_rate_pct"],
            wallet_realized_pnl=summary["realized_pnl_usd"],
            wallet_total_pnl=summary["total_pnl_usd"],
            wallet_trade_count=summary["total_trades"],
            discovery_count=discovery_count,
        )
        tier = tier_for(score)

        profile = await session.get(SmartWalletProfile, wallet)
        if profile is None:
            profile = SmartWalletProfile(wallet=wallet)
            session.add(profile)
        profile.updated_at = datetime.now(timezone.utc)
        profile.score = score
        profile.tier = tier
        profile.win_rate_pct = summary["win_rate_pct"]
        profile.realized_pnl_usd_30d = summary["realized_pnl_usd"]
        profile.total_pnl_usd_30d = summary["total_pnl_usd"]
        profile.total_trades_30d = summary["total_trades"]
        profile.discovery_count = discovery_count
        profile.tags_json = json.dumps(trader["tags"])

        tracked = await session.get(TrackedWallet, wallet)
        if tracked is None and score >= settings.birdeye_min_wallet_score:
            # wallet_policy.py is the ONLY service allowed to decide enabled state.
            session.add(TrackedWallet(
                address=wallet,
                label="Policy pending",
                enabled=False,
                score=0.0,
            ))

        await session.commit()
        return score, tier


async def scan_token(
    settings: Settings,
    api: BirdeyeClient,
    http: httpx.AsyncClient,
    mint: str,
    signal_score: float,
    liquidity_usd: float,
) -> None:
    scan = BirdeyeTokenScan(
        token_mint=mint,
        signal_score=signal_score,
        liquidity_usd=liquidity_usd,
        status="running",
        cu_estimated=0,
    )
    async with SessionLocal() as session:
        session.add(scan)
        await session.commit()

    try:
        traders = await api.top_traders(http, mint)
        cu_used = 30

        # Save all top-trader discoveries even if we only profile the best few.
        async with SessionLocal() as session:
            for trader in traders:
                session.add(WalletDiscovery(
                    wallet=trader["owner"],
                    token_mint=mint,
                    rank=trader["rank"],
                    token_realized_pnl_usd=trader["realized_pnl"],
                    token_total_pnl_usd=trader["total_pnl"],
                    token_volume_usd=trader["volume_usd"],
                    token_trade_count=trader["trade"],
                    tags_json=json.dumps(trader["tags"]),
                ))
            await session.commit()

        # Profile only a few candidates to preserve Birdeye credits.
        profiled = 0
        for trader in traders[:settings.birdeye_wallet_profiles_per_token]:
            try:
                score, tier = await _profile_and_store(
                    settings, api, http, mint, trader
                )
                profiled += 1
                cu_used += 20
                log.info(
                    "Smart-wallet candidate %s score %.1f (%s)",
                    trader["owner"][:8], score, tier
                )
            except httpx.HTTPStatusError as exc:
                # Some plans can expose top traders but not every wallet endpoint.
                log.warning(
                    "Birdeye wallet profile unavailable for %s: HTTP %s",
                    trader["owner"][:8], exc.response.status_code
                )
            except Exception as exc:
                log.warning("Wallet profile failed for %s: %s", trader["owner"][:8], exc)

        async with SessionLocal() as session:
            row = await session.get(BirdeyeTokenScan, mint)
            row.status = "complete"
            row.candidates_found = len(traders)
            row.profiles_requested = profiled
            row.cu_estimated = cu_used
            await store_event(
                session,
                source="birdeye",
                event_type="smart_money_scan",
                payload={
                    "mint": mint,
                    "signal_score": signal_score,
                    "traders_found": len(traders),
                    "profiled": profiled,
                    "cu_estimated": cu_used,
                },
                token_mint=mint,
                commit=False,
            )
            await session.commit()

    except Exception as exc:
        async with SessionLocal() as session:
            row = await session.get(BirdeyeTokenScan, mint)
            if row:
                row.status = "error"
                row.error_text = str(exc)[:1000]
            await session.commit()
        raise



async def _refresh_sparse_profiles(
    settings: Settings,
    api: BirdeyeClient,
    http: httpx.AsyncClient,
    limit: int = 12,
) -> None:
    """Refresh older profiles whose wallet-wide 30d metrics were missing/zero.

    This is deliberately bounded so restarting the app cannot chew through
    the API allowance.
    """
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        rows = list((
            await session.execute(
                select(SmartWalletProfile)
                .where(
                    (SmartWalletProfile.total_trades_30d.is_(None))
                    | (SmartWalletProfile.total_trades_30d == 0)
                )
                .order_by(SmartWalletProfile.updated_at.asc())
                .limit(limit)
            )
        ).scalars())

    if not rows:
        return

    log.info("Refreshing %d sparse smart-wallet profile(s)", len(rows))

    for profile_stub in rows:
        if profile_stub.updated_at:
            updated = profile_stub.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            # Avoid repeating a zero-result refresh every time the user restarts.
            if (now - updated).total_seconds() < 6 * 3600:
                continue

        try:
            summary = await api.wallet_summary(http, profile_stub.wallet)
        except Exception as exc:
            log.warning(
                "Smart-wallet refresh failed for %s: %s",
                profile_stub.wallet[:8], exc
            )
            continue

        async with SessionLocal() as session:
            profile = await session.get(SmartWalletProfile, profile_stub.wallet)
            if profile is None:
                continue

            latest_discovery = (
                await session.execute(
                    select(WalletDiscovery)
                    .where(WalletDiscovery.wallet == profile.wallet)
                    .order_by(WalletDiscovery.discovered_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            token_realized = latest_discovery.token_realized_pnl_usd if latest_discovery else 0.0
            token_total = latest_discovery.token_total_pnl_usd if latest_discovery else 0.0
            token_volume = latest_discovery.token_volume_usd if latest_discovery else 0.0
            token_trades = latest_discovery.token_trade_count if latest_discovery else 0
            try:
                tags = json.loads(latest_discovery.tags_json) if latest_discovery else []
            except Exception:
                tags = []

            profile.win_rate_pct = summary["win_rate_pct"]
            profile.realized_pnl_usd_30d = summary["realized_pnl_usd"]
            profile.total_pnl_usd_30d = summary["total_pnl_usd"]
            profile.total_trades_30d = summary["total_trades"]
            profile.updated_at = datetime.now(timezone.utc)

            # Only recompute score when Birdeye actually supplied wallet-wide metrics.
            if summary.get("_recognized"):
                profile.score = score_wallet(
                    token_realized_pnl=_num(token_realized),
                    token_total_pnl=_num(token_total),
                    token_volume_usd=_num(token_volume),
                    token_trade_count=_integer(token_trades),
                    tags=tags,
                    win_rate_pct=summary["win_rate_pct"],
                    wallet_realized_pnl=summary["realized_pnl_usd"],
                    wallet_total_pnl=summary["total_pnl_usd"],
                    wallet_trade_count=summary["total_trades"],
                    discovery_count=profile.discovery_count,
                )
                profile.tier = tier_for(profile.score)

                # TrackedWallet policy is reconciled exclusively by wallet_policy.py.

            await session.commit()

            log.info(
                "Wallet refresh %s: trades=%s win=%s pnl=$%.2f score=%.1f %s",
                profile.wallet[:8],
                profile.total_trades_30d,
                "—" if profile.win_rate_pct is None else f"{profile.win_rate_pct:.1f}%",
                profile.realized_pnl_usd_30d or 0.0,
                profile.score,
                profile.tier,
            )


async def run_birdeye(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.birdeye_enabled:
        log.info("Birdeye disabled")
        return
    if not settings.birdeye_api_key:
        log.warning("Birdeye enabled but BIRDEYE_API_KEY is empty")
        return

    api = BirdeyeClient(settings)
    async with httpx.AsyncClient() as http:
        try:
            valid = await api.validate(http)
            if valid:
                log.info("Birdeye API key validated; smart-money discovery active")
                await _refresh_sparse_profiles(settings, api, http)
            else:
                log.warning("Birdeye key validation returned unsuccessful response")
        except Exception as exc:
            log.warning("Birdeye validation failed: %s", exc)
            return

        while not stop.is_set():
            try:
                candidate = await _next_candidate_token(settings)
                if candidate:
                    mint, signal_score, liquidity_usd = candidate
                    log.info(
                        "Scanning smart money for %s (signal %.1f, liquidity $%.0f)",
                        mint[:10], signal_score, liquidity_usd
                    )
                    await scan_token(
                        settings, api, http, mint, signal_score, liquidity_usd
                    )
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                log.warning(
                    "Birdeye HTTP %s: %s",
                    exc.response.status_code,
                    exc.response.text[:250],
                )
            except Exception as exc:
                log.exception("Birdeye discovery error: %s", exc)

            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
