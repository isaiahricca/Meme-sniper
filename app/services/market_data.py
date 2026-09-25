import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select, func

from app.config import Settings
from app.services.market_math import pair_base as _pair_base, pair_quote as _pair_quote, pair_price as _pair_price, pair_liquidity as _pair_liquidity, choose_pair
from app.db import SessionLocal
from app.models import (
    Token, TokenPairState, PairLatestPrice, PairPriceObservation,
    WalletSwapV06, WalletSwapMeasurementV06,
    PaperCopyTradeV06, SignalMeasurementV06, SignalPaperTradeV06,
)

log = logging.getLogger("market")
BASE = "https://api.dexscreener.com"
_request_lock = asyncio.Lock()
_snapshot_lock = asyncio.Lock()
_last_request_at = 0.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _f(value, default=0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _i(value, default=0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


async def _paced_get(client: httpx.AsyncClient, settings: Settings, url: str) -> httpx.Response:
    global _last_request_at
    async with _request_lock:
        loop = asyncio.get_running_loop()
        wait = settings.market_min_request_interval_seconds - (loop.time() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = loop.time()
    response = await client.get(url, timeout=12)
    if response.status_code == 429:
        try:
            retry = max(1.0, min(float(response.headers.get("retry-after", "30")), 3600.0))
        except ValueError:
            retry = 30.0
        async with _request_lock:
            _last_request_at = max(_last_request_at, loop.time() + retry)
    return response



async def _fetch_token_pairs(client: httpx.AsyncClient, settings: Settings, mint: str) -> list[dict]:
    response = await _paced_get(client, settings, f"{BASE}/token-pairs/v1/solana/{mint}")
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, list) else (payload.get("pairs") or [])


async def _fetch_exact_pairs(
    client: httpx.AsyncClient,
    settings: Settings,
    pair_addresses: list[str],
) -> list[dict]:
    if not pair_addresses:
        return []
    joined = ",".join(pair_addresses)
    response = await _paced_get(client, settings, f"{BASE}/latest/dex/pairs/solana/{joined}")
    response.raise_for_status()
    payload = response.json()
    pairs = payload.get("pairs") if isinstance(payload, dict) else None
    pairs = pairs or []

    # Retry omissions on the next scheduled refresh. Serial fallbacks used to
    # delay every returned snapshot and falsely timestamp it after those waits.
    requested = set(pair_addresses)
    return [p for p in pairs if p.get("chainId") == "solana"
            and p.get("pairAddress") in requested]


async def _pin_pair(session, mint: str, pair: dict, now: datetime) -> None:
    address = pair.get("pairAddress")
    base = _pair_base(pair)
    if not address or base != mint:
        return
    state = await session.get(TokenPairState, mint)
    if state is None:
        state = TokenPairState(
            token_mint=mint,
            pair_address=address,
            dex_id=pair.get("dexId"),
            base_mint=base,
            quote_mint=_pair_quote(pair),
            pinned_at=now,
            last_verified_at=now,
            status="active",
        )
        session.add(state)
    elif state.status in {"needs_repin", "invalid"}:
        state.pair_address = address
        state.dex_id = pair.get("dexId")
        state.base_mint = base
        state.quote_mint = _pair_quote(pair)
        state.pinned_at = now
        state.last_verified_at = now
        state.status = "active"
        state.invalid_reason = None
    elif state.pair_address == address:
        state.last_verified_at = now

    await _upsert_exact_snapshot(session, pair, now, source="dexscreener_discovery")


async def _upsert_exact_snapshot(
    session,
    pair: dict,
    now: datetime,
    *,
    source: str,
) -> None:
    address = pair.get("pairAddress")
    base = _pair_base(pair)
    price = _pair_price(pair)
    if pair.get("chainId") != "solana" or not address or not base or price <= 0:
        return

    row = await session.get(PairLatestPrice, address)
    if row is not None:
        previous_ts = row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=timezone.utc)
        if now < previous_ts:
            return
    if row is not None and row.source == "dexscreener_exact_pair" and source != "dexscreener_exact_pair":
        return
    if row is not None and row.base_mint != base:
        log.warning("Rejected changed base mint for pair %s", address)
        return
    if source == "dexscreener_exact_pair":
        session.add(PairPriceObservation(
            pair_address=address, base_mint=base, price_usd=price,
            liquidity_usd=_pair_liquidity(pair) or None, ts=now, source=source,
        ))
    if row is None:
        row = PairLatestPrice(
            pair_address=address,
            token_mint=base,
            base_mint=base,
            quote_mint=_pair_quote(pair),
            dex_id=pair.get("dexId"),
            price_usd=price,
            ts=now,
            source=source,
        )
        session.add(row)
    row.token_mint = base
    row.base_mint = base
    row.quote_mint = _pair_quote(pair)
    row.dex_id = pair.get("dexId")
    row.price_usd = price
    row.liquidity_usd = _pair_liquidity(pair) or None
    row.volume_h24_usd = _f((pair.get("volume") or {}).get("h24"), 0.0) or None
    row.price_change_m5_pct = _f((pair.get("priceChange") or {}).get("m5"), 0.0)
    m5 = (pair.get("txns") or {}).get("m5") or {}
    row.buys_m5 = _i(m5.get("buys"), 0)
    row.sells_m5 = _i(m5.get("sells"), 0)
    row.ts = now
    row.source = source

    state = await session.get(TokenPairState, base)
    if state and state.status == "active" and state.pair_address == address:
        state.last_verified_at = now
        token = await session.get(Token, base)
        if token is None:
            token = Token(mint=base, source="market")
            session.add(token)
        token.pair_address = address
        token.price_usd = price
        token.liquidity_usd = row.liquidity_usd
        token.volume_h24_usd = row.volume_h24_usd
        token.price_change_m5_pct = row.price_change_m5_pct
        token.buys_m5 = row.buys_m5
        token.sells_m5 = row.sells_m5


async def _critical_pairs_and_missing(settings: Settings) -> tuple[list[str], list[str]]:
    async with SessionLocal() as session:
        pairs: list[str] = []
        missing: list[str] = []

        now = utcnow()
        queries = [
            select(PaperCopyTradeV06.pair_address).where(
                PaperCopyTradeV06.status.in_(["pending", "open"])
            ).distinct().limit(80),
            select(SignalPaperTradeV06.pair_address).where(
                SignalPaperTradeV06.status.in_(["pending", "open"])
            ).distinct().limit(80),
        ]
        for model in (WalletSwapMeasurementV06, SignalMeasurementV06):
            queries.append(select(model.pair_address).where(
                model.captured_at.is_(None),
                model.due_at >= now - timedelta(seconds=settings.copyability_max_capture_lag_seconds),
                model.due_at <= now + timedelta(seconds=settings.market_critical_refresh_seconds),
            ).group_by(model.pair_address).order_by(func.min(model.due_at)).limit(80))
        for stmt in queries:
            vals = list((await session.execute(stmt)).scalars())
            pairs.extend([v for v in vals if v])

        pending_swaps = list((await session.execute(
            select(WalletSwapV06.token_mint).where(
                WalletSwapV06.action == "BUY",
                WalletSwapV06.copy_eligible.is_(True),
                WalletSwapV06.integrity_status == "pending",
            ).order_by(WalletSwapV06.ts.asc()).limit(60)
        )).scalars())
        for mint in pending_swaps:
            state = await session.get(TokenPairState, mint)
            if state and state.status == "active":
                pairs.append(state.pair_address)
            else:
                missing.append(mint)

        return list(dict.fromkeys(pairs))[:90], list(dict.fromkeys(missing))[:20]


async def _broad_pairs_and_missing(settings: Settings) -> tuple[list[str], list[str]]:
    async with SessionLocal() as session:
        # Refresh the existing pinned universe as well as newly discovered tokens.
        # Previously we only refreshed the newest PumpPortal rows, so older liquid
        # migrated coins quietly fell out of the challenge scanner.
        active_states = list((await session.execute(
            select(TokenPairState)
            .where(TokenPairState.status == "active")
            .order_by(TokenPairState.last_verified_at.asc())
            .limit(settings.market_max_active_pairs)
        )).scalars())
        pairs: list[str] = [s.pair_address for s in active_states if s.pair_address]

        tokens = list((await session.execute(
            select(Token).order_by(Token.discovered_at.desc()).limit(settings.market_recent_token_limit)
        )).scalars())
        missing: list[str] = []
        for token in tokens:
            state = await session.get(TokenPairState, token.mint)
            if state and state.status == "active":
                pairs.append(state.pair_address)
            elif state is None or state.status in {"needs_repin", "invalid"}:
                missing.append(token.mint)
        return list(dict.fromkeys(pairs))[:settings.market_max_active_pairs], list(dict.fromkeys(missing))[:40]


async def _discover_missing(client: httpx.AsyncClient, settings: Settings, mints: list[str]) -> int:
    found = 0
    for mint in mints[:20]:
        try:
            pairs = await _fetch_token_pairs(client, settings, mint)
            pair = choose_pair(mint, pairs, settings.pair_discovery_min_liquidity_usd)
            if pair:
                now = utcnow()  # timestamp after response, never before request
                async with _snapshot_lock, SessionLocal() as session:
                    await _pin_pair(session, mint, pair, now)
                    await session.commit()
                found += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Pair discovery failed %s…: %s", mint[:10], exc)
    return found


async def _refresh_exact(client: httpx.AsyncClient, settings: Settings, pair_addresses: list[str]) -> int:
    if not pair_addresses:
        return 0
    refreshed = 0
    batch_size = max(1, min(settings.market_exact_pair_batch_size, 30))
    for start in range(0, len(pair_addresses), batch_size):
        batch = pair_addresses[start:start + batch_size]
        try:
            pairs = await _fetch_exact_pairs(client, settings, batch)
            fetched_at = utcnow()
            async with _snapshot_lock, SessionLocal() as session:
                for pair in pairs:
                    await _upsert_exact_snapshot(
                        session, pair, fetched_at, source="dexscreener_exact_pair"
                    )
                    refreshed += 1
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Exact-pair refresh failed: %s", exc)
    return refreshed


async def mark_pair_for_repin(token_mint: str, reason: str = "migration") -> None:
    async with SessionLocal() as session:
        state = await session.get(TokenPairState, token_mint)
        if state:
            state.status = "needs_repin"
            state.invalid_reason = reason
            await session.commit()


async def run_market_data(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.dexscreener_enabled:
        log.info("DEX Screener market service disabled")
        return

    log.info("Verified pair market service active")
    async with httpx.AsyncClient(headers={"User-Agent": "MemeSniperV06/0.6"}) as client:
        async def cycle(kind, interval):
            while not stop.is_set():
                started = asyncio.get_running_loop().time()
                try:
                    critical, missing = await _critical_pairs_and_missing(settings)
                    if kind == "critical":
                        await _refresh_exact(client, settings, critical)
                    elif kind == "broad":
                        broad, _ = await _broad_pairs_and_missing(settings)
                        await _refresh_exact(client, settings, [p for p in broad if p not in set(critical)])
                    else:
                        _, broad_missing = await _broad_pairs_and_missing(settings)
                        await _discover_missing(client, settings, list(dict.fromkeys(missing + broad_missing)))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Market %s cycle failed", kind)
                delay = max(0.5, interval - (asyncio.get_running_loop().time() - started))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

        # Separate bounded workers; all requests still share the rate limiter.
        tasks = [asyncio.create_task(cycle(kind, interval)) for kind, interval in (
            ("critical", max(settings.market_critical_refresh_seconds, 1.0)),
            ("broad", max(settings.market_broad_refresh_seconds, 2.0)),
            ("discovery", max(settings.market_discovery_refresh_seconds, 2.0)),
        )]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
