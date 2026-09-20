import asyncio
import logging
import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import Token, WalletTradeMeasurement, PaperCopyTrade

log = logging.getLogger("dexscreener")

BASE = "https://api.dexscreener.com"


def _best_pair(pairs: list[dict]) -> dict | None:
    if not pairs:
        return None
    sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
    candidates = sol_pairs or pairs
    return max(
        candidates,
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
    )


async def refresh_token(client: httpx.AsyncClient, token: Token) -> None:
    url = f"{BASE}/token-pairs/v1/solana/{token.mint}"
    response = await client.get(url, timeout=10)
    response.raise_for_status()
    payload = response.json()
    pairs = payload if isinstance(payload, list) else payload.get("pairs", [])
    pair = _best_pair(pairs)
    if not pair:
        return

    token.price_usd = float(pair.get("priceUsd") or 0) or None
    token.liquidity_usd = float((pair.get("liquidity") or {}).get("usd") or 0) or None
    token.volume_h24_usd = float((pair.get("volume") or {}).get("h24") or 0) or None
    token.price_change_m5_pct = float((pair.get("priceChange") or {}).get("m5") or 0)

    m5 = (pair.get("txns") or {}).get("m5") or {}
    token.buys_m5 = int(m5.get("buys") or 0)
    token.sells_m5 = int(m5.get("sells") or 0)
    token.pair_address = pair.get("pairAddress")


async def run_dexscreener(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.dexscreener_enabled:
        log.info("DEX Screener disabled")
        return

    async with httpx.AsyncClient(headers={"User-Agent": "MemeSniperV01/0.1"}) as client:
        while not stop.is_set():
            try:
                async with SessionLocal() as session:
                    # Prioritise tokens currently needed by the wallet-copyability
                    # engine, then fill the remaining slots with newly discovered tokens.
                    pending_mints = list((
                        await session.execute(
                            select(WalletTradeMeasurement.token_mint)
                            .where(WalletTradeMeasurement.captured_at.is_(None))
                            .order_by(WalletTradeMeasurement.due_at.asc())
                            .limit(20)
                        )
                    ).scalars())

                    copy_mints = list((
                        await session.execute(
                            select(PaperCopyTrade.token_mint)
                            .where(PaperCopyTrade.status.in_(["pending", "open"]))
                            .order_by(PaperCopyTrade.entry_due_at.asc())
                            .limit(20)
                        )
                    ).scalars())

                    recent_tokens = list((
                        await session.execute(
                            select(Token).order_by(Token.discovered_at.desc()).limit(25)
                        )
                    ).scalars())

                    tokens = []
                    seen = set()
                    for mint in pending_mints + copy_mints:
                        if mint in seen:
                            continue
                        token = await session.get(Token, mint)
                        if token:
                            seen.add(mint)
                            tokens.append(token)
                    for token in recent_tokens:
                        if token.mint in seen:
                            continue
                        seen.add(token.mint)
                        tokens.append(token)
                    tokens = tokens[:35]

                    for token in tokens:
                        try:
                            await refresh_token(client, token)
                        except Exception as exc:
                            log.debug("DEX snapshot failed for %s: %s", token.mint, exc)
                    await session.commit()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("DEX Screener loop error: %s", exc)

            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=max(settings.dexscreener_refresh_seconds, 2)
                )
            except asyncio.TimeoutError:
                pass
