import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import websockets
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import Token, TrackedWallet, WalletSwapV06
from app.services.event_store import store_event, ensure_wallets
from app.services.wallet_parser import parse_wallet_swap, same_buy_episode

log = logging.getLogger("helius")
_rpc_lock = asyncio.Lock()
_last_rpc_at = 0.0


def _signature_from_standard(payload: dict) -> str | None:
    try:
        return payload["params"]["result"]["value"]["signature"]
    except (KeyError, TypeError):
        return None


def _safe_error(exc: Exception, settings: Settings) -> str:
    text = str(exc)
    if settings.helius_api_key:
        text = text.replace(settings.helius_api_key, "***")
    return text


async def _desired_wallets(settings: Settings) -> list[str]:
    async with SessionLocal() as session:
        result = await session.execute(
            select(TrackedWallet)
            .where(TrackedWallet.enabled.is_(True))
            .order_by(TrackedWallet.score.desc())
            .limit(settings.smart_wallet_max_tracked)
        )
        db_wallets = [x.address for x in result.scalars()]

    merged: list[str] = []
    seen = set()
    for address in settings.tracked_wallet_list + db_wallets:
        if address and address not in seen:
            seen.add(address)
            merged.append(address)
    return merged[:settings.smart_wallet_max_tracked]


async def _rpc_post(client: httpx.AsyncClient, settings: Settings, payload: dict) -> httpx.Response:
    global _last_rpc_at
    async with _rpc_lock:
        loop = asyncio.get_running_loop()
        wait = settings.helius_rpc_min_interval_seconds - (loop.time() - _last_rpc_at)
        if wait > 0:
            await asyncio.sleep(wait)
        response = await client.post(settings.helius_rpc_url, json=payload, timeout=12)
        _last_rpc_at = loop.time()
        return response


async def _fetch_transaction(
    client: httpx.AsyncClient,
    settings: Settings,
    signature: str,
) -> dict:
    response = await _rpc_post(
        client,
        settings,
        {
            "jsonrpc": "2.0",
            "id": signature[:8],
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    # Solana/Agave v1 transactions are live in 2026. Helius now
                    # recommends declaring version 1 for getTransaction.
                    "maxSupportedTransactionVersion": 1,
                },
            ],
        },
    )
    response.raise_for_status()
    return response.json()


async def _record_wallet_swap(
    settings: Settings,
    client: httpx.AsyncClient,
    wallet: str,
    signature: str,
    detected_at: datetime,
) -> None:
    # A processed logs notification can arrive before a confirmed transaction is
    # queryable. Retry briefly, but never invent a transaction if it remains absent.
    payload: dict = {}
    for delay in (0.20, 0.55, 1.0, 1.8):
        await asyncio.sleep(delay)
        try:
            payload = await _fetch_transaction(client, settings, signature)
            if payload.get("result"):
                break
        except asyncio.CancelledError:
            raise
        except Exception:
            payload = {}
    if not payload.get("result"):
        return

    parsed, reason = parse_wallet_swap(payload, wallet)
    async with SessionLocal() as session:
        await store_event(
            session,
            source="helius",
            event_type="wallet_tx_parsed" if parsed else "wallet_tx_ignored",
            payload={
                "wallet": wallet,
                "signature": signature,
                "reason": reason,
                "action": parsed.action if parsed else None,
                "token": parsed.token_mint if parsed else None,
            },
            token_mint=parsed.token_mint if parsed else None,
            wallet=wallet,
            signature=signature,
            commit=False,
        )

        tracked = await session.get(TrackedWallet, wallet)
        if tracked:
            tracked.observed_events += 1

        if parsed is None:
            await session.commit()
            return

        existing = (
            await session.execute(
                select(WalletSwapV06.id)
                .where(
                    WalletSwapV06.wallet == wallet,
                    WalletSwapV06.signature == signature,
                    WalletSwapV06.token_mint == parsed.token_mint,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            await session.commit()
            return

        token = await session.get(Token, parsed.token_mint)
        if token is None:
            token = Token(mint=parsed.token_mint, source="tracked_wallet")
            session.add(token)

        copy_eligible = parsed.action == "BUY"
        episode_parent_id = None
        if parsed.action == "BUY":
            previous = (
                await session.execute(
                    select(WalletSwapV06)
                    .where(
                        WalletSwapV06.wallet == wallet,
                        WalletSwapV06.token_mint == parsed.token_mint,
                        WalletSwapV06.action == "BUY",
                    )
                    .order_by(WalletSwapV06.ts.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if previous is not None:
                if same_buy_episode(previous.ts, detected_at, settings.wallet_buy_episode_seconds):
                    copy_eligible = False
                    episode_parent_id = previous.episode_parent_id or previous.id

        row = WalletSwapV06(
            ts=detected_at,
            wallet=wallet,
            signature=signature,
            action=parsed.action,
            token_mint=parsed.token_mint,
            token_delta=parsed.token_delta,
            quote_mint=parsed.quote_mint,
            quote_delta=parsed.quote_delta,
            input_mint=parsed.input_mint,
            input_delta=parsed.input_delta,
            classification=parsed.classification,
            copy_eligible=copy_eligible,
            episode_parent_id=episode_parent_id,
            tracked_at_detection=True,
            integrity_status=(
                "pending" if parsed.action == "BUY" and copy_eligible
                else "scale_in_ignored" if parsed.action == "BUY"
                else "not_measured"
            ),
        )
        session.add(row)
        await session.commit()



async def _tx_worker(
    worker_id: int,
    queue: asyncio.Queue,
    settings: Settings,
    client: httpx.AsyncClient,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            wallet, signature, detected_at = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        try:
            await _record_wallet_swap(settings, client, wallet, signature, detected_at)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "Helius tx worker %d failed %s…: %s",
                worker_id, signature[:10], _safe_error(exc, settings)
            )
        finally:
            queue.task_done()


async def _sync_subscriptions(
    ws,
    settings: Settings,
    subscription_to_wallet: dict[int, str],
    wallet_to_subscription: dict[str, int],
    pending_subscribe: dict[int, str],
    next_id: int,
) -> int:
    desired = set(await _desired_wallets(settings))
    current = set(wallet_to_subscription)
    pending = set(pending_subscribe.values())

    # Standard Solana logsSubscribe has logsUnsubscribe, so central policy can
    # actually demote a wallet instead of merely changing a dashboard flag.
    for wallet in current - desired:
        sub_id = wallet_to_subscription.pop(wallet, None)
        if sub_id is None:
            continue
        subscription_to_wallet.pop(sub_id, None)
        try:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": next_id,
                "method": "logsUnsubscribe", "params": [sub_id],
            }))
            next_id += 1
            log.info("Helius unsubscribed wallet %s…", wallet[:10])
        except Exception as exc:
            log.warning("Helius unsubscribe failed %s…: %s", wallet[:10], _safe_error(exc, settings))

    for wallet in desired - current - pending:
        req_id = next_id
        next_id += 1
        pending_subscribe[req_id] = wallet
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [wallet]},
                {"commitment": settings.helius_commitment},
            ],
        }))
        log.info("Helius subscribing smart wallet %s…", wallet[:10])
    return next_id


async def run_helius(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.helius_enabled:
        log.info("Helius disabled")
        return
    if not settings.helius_api_key:
        log.warning("Helius enabled but HELIUS_API_KEY is empty")
        return

    async with SessionLocal() as session:
        await ensure_wallets(session, settings.tracked_wallet_list)

    queue: asyncio.Queue = asyncio.Queue(maxsize=max(settings.helius_tx_queue_size, 10))
    backoff = 1

    async with httpx.AsyncClient() as client:
        workers = [
            asyncio.create_task(_tx_worker(i + 1, queue, settings, client, stop))
            for i in range(max(settings.helius_tx_worker_count, 1))
        ]
        try:
            while not stop.is_set():
                try:
                    log.info("Connecting to Helius verified wallet stream")
                    async with websockets.connect(
                        settings.helius_wss_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        max_size=16_000_000,
                    ) as ws:
                        backoff = 1
                        subscription_to_wallet: dict[int, str] = {}
                        wallet_to_subscription: dict[str, int] = {}
                        pending_subscribe: dict[int, str] = {}
                        unsubscribe_ids: set[int] = set()
                        next_id = 1
                        loop = asyncio.get_running_loop()
                        last_sync = 0.0

                        while not stop.is_set():
                            if loop.time() - last_sync >= max(settings.smart_wallet_poll_seconds, 2):
                                before = next_id
                                next_id = await _sync_subscriptions(
                                    ws, settings,
                                    subscription_to_wallet,
                                    wallet_to_subscription,
                                    pending_subscribe,
                                    next_id,
                                )
                                # IDs used for unsubscribe have no wallet pending mapping.
                                unsubscribe_ids.update(range(before, next_id))
                                unsubscribe_ids.difference_update(pending_subscribe.keys())
                                last_sync = loop.time()

                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            except asyncio.TimeoutError:
                                continue
                            payload = json.loads(raw)

                            if "id" in payload:
                                req_id = payload.get("id")
                                wallet = pending_subscribe.pop(req_id, None)
                                if wallet is not None:
                                    if "error" in payload:
                                        log.warning("Helius subscribe failed %s…: %s", wallet[:10], payload.get("error"))
                                        continue
                                    sub_id = payload.get("result")
                                    if isinstance(sub_id, int):
                                        subscription_to_wallet[sub_id] = wallet
                                        wallet_to_subscription[wallet] = sub_id
                                    continue
                                if req_id in unsubscribe_ids:
                                    unsubscribe_ids.discard(req_id)
                                    continue

                            if "params" not in payload:
                                continue

                            sub_id = payload.get("params", {}).get("subscription")
                            wallet = subscription_to_wallet.get(sub_id)
                            signature = _signature_from_standard(payload)
                            if wallet and signature:
                                item = (wallet, signature, datetime.now(timezone.utc))
                                try:
                                    queue.put_nowait(item)
                                except asyncio.QueueFull:
                                    # Fail visibly rather than exhausting memory. A dropped
                                    # transaction is an integrity loss, so log it loudly.
                                    log.error(
                                        "Helius parse queue FULL (%d); dropped %s… for %s…",
                                        queue.qsize(), signature[:10], wallet[:10]
                                    )

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "Helius stream error: %s; reconnecting in %ss",
                        _safe_error(exc, settings), backoff
                    )
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, 30)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
