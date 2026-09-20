import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timezone

import httpx

from app.config import Settings
from app.db import SessionLocal
from app.models import NansenSmartTradeV07, SmartWalletProfile, Token, SystemState

log = logging.getLogger("nansen")
BASE = "https://api.nansen.ai"
QUOTE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USD1"}


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(v)))


def parse_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def candidate_prior(trade_value_usd: float | None) -> float:
    value = max(float(trade_value_usd or 0.0), 0.0)
    return round(clamp(72.0 + min(math.log10(value + 1.0) * 2.0, 12.0), 60.0, 84.0), 2)


def event_key(item: dict) -> str:
    raw = "|".join([
        str(item.get("transaction_hash") or ""),
        str(item.get("trader_address") or ""),
        str(item.get("token_bought_address") or ""),
        str(item.get("token_sold_address") or ""),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()


async def _state(session, key: str, value: str):
    row = await session.get(SystemState, key)
    if row is None:
        session.add(SystemState(key=key, value=value))
    else:
        row.value = value


async def fetch_smart_money(settings: Settings, client: httpx.AsyncClient):
    filters = {
        "include_smart_money_labels": settings.nansen_label_list,
        "trade_value_usd": {"min": settings.nansen_min_trade_value_usd},
        "token_bought_age_days": {"max": settings.nansen_max_token_age_days},
    }
    if settings.nansen_max_market_cap_usd > 0:
        filters["token_bought_market_cap"] = {"max": settings.nansen_max_market_cap_usd}
    r = await client.post(
        f"{BASE}/api/v1/smart-money/dex-trades",
        headers={"accept":"application/json","content-type":"application/json","apikey":settings.nansen_api_key},
        json={
            "chains":["solana"],
            "filters":filters,
            "pagination":{"page":1,"per_page":max(1,min(settings.nansen_per_page,1000))},
            "order_by":[{"field":"block_timestamp","direction":"DESC"}],
        },
        timeout=25,
    )
    r.raise_for_status()
    payload = r.json()
    return (payload.get("data") or []), {
        "credits_remaining": r.headers.get("X-Nansen-Credits-Remaining"),
        "credits_used": r.headers.get("X-Nansen-Credits-Used"),
    }


async def ingest(settings: Settings, items: list[dict], meta: dict) -> int:
    now = datetime.now(timezone.utc)
    added = 0
    async with SessionLocal() as session:
        for item in items:
            wallet = str(item.get("trader_address") or "").strip()
            bought = str(item.get("token_bought_address") or "").strip()
            tx = str(item.get("transaction_hash") or "").strip()
            if not wallet or not bought or not tx:
                continue
            key = event_key(item)
            if await session.get(NansenSmartTradeV07, key):
                continue
            value = float(item.get("trade_value_usd") or 0.0)
            session.add(NansenSmartTradeV07(
                event_key=key, ts=parse_timestamp(item.get("block_timestamp")), transaction_hash=tx,
                wallet=wallet, wallet_label=item.get("trader_address_label") or None,
                token_bought_address=bought, token_bought_symbol=item.get("token_bought_symbol") or None,
                token_sold_address=item.get("token_sold_address") or None,
                token_sold_symbol=item.get("token_sold_symbol") or None,
                token_bought_age_days=int(item["token_bought_age_days"]) if item.get("token_bought_age_days") is not None else None,
                token_bought_market_cap=float(item["token_bought_market_cap"]) if item.get("token_bought_market_cap") is not None else None,
                trade_value_usd=value,
            ))
            added += 1

            prior = candidate_prior(value)
            profile = await session.get(SmartWalletProfile, wallet)
            if profile is None:
                profile = SmartWalletProfile(wallet=wallet, score=prior, tier="STRONG", source="nansen", tags_json=json.dumps(["nansen_smart_money"]))
                session.add(profile)
            else:
                old_sources = set((profile.source or "").split("+"))
                had_birdeye = "birdeye" in old_sources
                old_sources.add("nansen")
                profile.source = "+".join(sorted(x for x in old_sources if x))[:30]
                try: tags = set(json.loads(profile.tags_json or "[]"))
                except Exception: tags = set()
                tags.add("nansen_smart_money")
                profile.tags_json = json.dumps(sorted(tags))
                if not had_birdeye:
                    profile.score = max(profile.score or 0.0, prior)
                    profile.tier = "STRONG" if profile.score >= 72 else "TESTING"
                profile.updated_at = now

            symbol = str(item.get("token_bought_symbol") or "").upper()
            if symbol not in QUOTE_SYMBOLS and await session.get(Token, bought) is None:
                session.add(Token(mint=bought, symbol=item.get("token_bought_symbol"), source="nansen_smart_money", discovered_at=now))

        await _state(session, "nansen_last_poll", now.isoformat())
        await _state(session, "nansen_last_new_trades", str(added))
        await _state(session, "nansen_last_error", "")
        if meta.get("credits_remaining") is not None:
            await _state(session, "nansen_credits_remaining", str(meta["credits_remaining"]))
        if meta.get("credits_used") is not None:
            await _state(session, "nansen_last_credits_used", str(meta["credits_used"]))
        await session.commit()
    return added


async def run_nansen(settings: Settings, stop: asyncio.Event):
    if not settings.nansen_enabled:
        log.info("Nansen Trader Intelligence disabled")
        return
    if not settings.nansen_api_key:
        log.warning("Nansen enabled but NANSEN_API_KEY is empty")
        return

    base_interval=max(settings.nansen_poll_seconds,settings.nansen_min_poll_seconds)
    interval=base_interval
    log.info("Nansen Smart Money active; credit-aware base poll %.0fs",base_interval)
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                items, meta = await fetch_smart_money(settings, client)
                added = await ingest(settings, items, meta)
                credits=None
                try: credits=int(float(meta.get("credits_remaining"))) if meta.get("credits_remaining") is not None else None
                except (TypeError,ValueError): credits=None
                if credits is not None and credits <= 0:
                    interval=settings.nansen_zero_credit_poll_seconds
                    reason="zero_credits"
                elif credits is not None and credits <= settings.nansen_low_credit_threshold:
                    interval=max(settings.nansen_low_credit_poll_seconds,base_interval)
                    reason="low_credits"
                else:
                    interval=base_interval; reason="normal"
                async with SessionLocal() as session:
                    await _state(session,"nansen_poll_mode",reason)
                    await _state(session,"nansen_next_poll_seconds",str(int(interval)))
                    await session.commit()
                if added:
                    log.info("Nansen ingested %d new Smart Money trade(s)", added)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as exc:
                status=exc.response.status_code if exc.response is not None else None
                safe=f"HTTP {status} from Nansen" if status else "Nansen HTTP error"
                if status in {402,403,429}:
                    interval=settings.nansen_zero_credit_poll_seconds if status in {402,403} else max(settings.nansen_error_backoff_seconds,base_interval)
                else:
                    interval=max(settings.nansen_error_backoff_seconds,base_interval)
                log.warning("Nansen poll paused/backed off: %s; next attempt %.0fs",safe,interval)
                async with SessionLocal() as session:
                    await _state(session,"nansen_last_error",safe)
                    await _state(session,"nansen_poll_mode",f"backoff_http_{status}")
                    await _state(session,"nansen_next_poll_seconds",str(int(interval)))
                    await session.commit()
            except Exception as exc:
                safe = str(exc).replace(settings.nansen_api_key, "***")
                interval=max(settings.nansen_error_backoff_seconds,base_interval)
                log.warning("Nansen poll failed: %s", safe)
                async with SessionLocal() as session:
                    await _state(session, "nansen_last_error", safe[:500])
                    await _state(session,"nansen_poll_mode","error_backoff")
                    await _state(session,"nansen_next_poll_seconds",str(int(interval)))
                    await session.commit()
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(interval, 15.0))
            except asyncio.TimeoutError:
                pass
