import json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Event, Token, TrackedWallet


async def store_event(
    session: AsyncSession,
    *,
    source: str,
    event_type: str,
    payload: dict,
    token_mint: str | None = None,
    wallet: str | None = None,
    signature: str | None = None,
    commit: bool = True,
) -> Event:
    row = Event(
        source=source,
        event_type=event_type,
        token_mint=token_mint,
        wallet=wallet,
        signature=signature,
        payload_json=json.dumps(payload, separators=(",", ":"), default=str),
    )
    session.add(row)
    if commit:
        await session.commit()
    return row


async def upsert_token(
    session: AsyncSession,
    mint: str,
    *,
    symbol: str | None = None,
    name: str | None = None,
    source: str = "unknown",
) -> Token:
    token = await session.get(Token, mint)
    if token is None:
        token = Token(mint=mint, symbol=symbol, name=name, source=source)
        session.add(token)
    else:
        if symbol:
            token.symbol = symbol
        if name:
            token.name = name
    await session.commit()
    return token


async def ensure_wallets(session: AsyncSession, addresses: list[str]) -> None:
    for address in addresses:
        if await session.get(TrackedWallet, address) is None:
            session.add(TrackedWallet(address=address))
    await session.commit()
