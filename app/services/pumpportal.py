import asyncio
import json
import logging
import websockets

from app.config import Settings
from app.db import SessionLocal
from app.models import TokenPairState
from app.services.event_store import store_event, upsert_token

log = logging.getLogger("pumpportal")


def _extract_mint(payload: dict) -> str | None:
    return (
        payload.get("mint")
        or payload.get("token")
        or payload.get("tokenAddress")
        or payload.get("address")
    )


def _safe_error(exc: Exception, settings: Settings) -> str:
    text = str(exc)
    if settings.pumpportal_api_key:
        text = text.replace(settings.pumpportal_api_key, "***")
    return text


async def run_pumpportal(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.pumpportal_enabled:
        log.info("PumpPortal disabled")
        return
    if not settings.pumpportal_api_key:
        log.warning("PumpPortal enabled but PUMPPORTAL_API_KEY is empty")
        return

    uri = f"wss://pumpportal.fun/api/data?api-key={settings.pumpportal_api_key}"
    backoff = 1

    while not stop.is_set():
        try:
            log.info("Connecting to PumpPortal")
            async with websockets.connect(
                uri,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=8_000_000,
            ) as ws:
                # One WebSocket, multiple subscriptions. Do not create one socket per token.
                if settings.pumpportal_subscribe_new_tokens:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                if settings.pumpportal_subscribe_migrations:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                if settings.pumpportal_subscribe_token_trades and settings.pumpportal_token_list:
                    await ws.send(json.dumps({
                        "method": "subscribeTokenTrade",
                        "keys": settings.pumpportal_token_list,
                    }))
                if settings.pumpportal_subscribe_account_trades and settings.tracked_wallet_list:
                    await ws.send(json.dumps({
                        "method": "subscribeAccountTrade",
                        "keys": settings.tracked_wallet_list,
                    }))

                backoff = 1
                async for raw in ws:
                    if stop.is_set():
                        return
                    payload = json.loads(raw)
                    mint = _extract_mint(payload)
                    tx_type = str(payload.get("txType") or payload.get("type") or "").lower()

                    if tx_type in {"create", "newtoken", "tokencreation"}:
                        event_type = "new_token"
                    elif "migration" in tx_type:
                        event_type = "migration"
                    elif tx_type in {"buy", "sell"}:
                        event_type = f"token_trade_{tx_type}"
                    else:
                        event_type = "pumpportal_message"

                    async with SessionLocal() as session:
                        if mint and event_type == "new_token":
                            await upsert_token(
                                session,
                                mint,
                                symbol=payload.get("symbol"),
                                name=payload.get("name"),
                                source="pumpportal",
                            )
                        if mint and event_type == "migration":
                            state = await session.get(TokenPairState, mint)
                            if state:
                                state.status = "needs_repin"
                                state.invalid_reason = "pumpportal_migration"

                        await store_event(
                            session,
                            source="pumpportal",
                            event_type=event_type,
                            payload=payload,
                            token_mint=mint,
                            wallet=payload.get("traderPublicKey") or payload.get("trader"),
                            signature=payload.get("signature"),
                            commit=False,
                        )
                        await session.commit()

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "PumpPortal stream error: %s; reconnecting in %ss",
                _safe_error(exc, settings), backoff
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30)
