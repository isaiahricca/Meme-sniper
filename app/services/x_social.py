import asyncio
import json
import logging
import math
import re
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select, func

from app.config import Settings
from app.db import SessionLocal
from app.models import Token, Signal, SignalPaperTradeV06, XSocialSnapshotV074, SystemState

log = logging.getLogger("x_social")
SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
SOLANA_BASE58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])([1-9A-HJ-NP-Za-km-z]{32,44})(?![1-9A-HJ-NP-Za-km-z])")


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _social_score(posts: int, authors: int, verified: int, likes: int, reposts: int, replies: int, quotes: int, max_followers: int) -> float:
    engagement = likes + 2 * reposts + replies + 2 * quotes
    score = (
        min(30.0, posts * 2.5)
        + min(20.0, authors * 3.0)
        + min(15.0, verified * 5.0)
        + min(20.0, math.log10(max(engagement, 0) + 1) * 8.0)
        + min(15.0, math.log10(max(max_followers, 0) + 1) * 3.0)
    )
    return round(min(100.0, score), 2)


async def _searches_last_hour() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    async with SessionLocal() as session:
        return int((await session.execute(
            select(func.count(XSocialSnapshotV074.id))
            .where(XSocialSnapshotV074.fetched_at >= cutoff)
        )).scalar_one() or 0)


async def _candidate_tokens(settings: Settings) -> list[Token]:
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        open_mints = list((await session.execute(
            select(SignalPaperTradeV06.token_mint)
            .where(SignalPaperTradeV06.status == "open")
            .order_by(SignalPaperTradeV06.opened_at.desc())
            .limit(settings.x_candidate_limit)
        )).scalars())

        recent_signals = list((await session.execute(
            select(Signal)
            .where(
                Signal.ts >= now - timedelta(minutes=10),
                Signal.total_score >= 58.0,
            )
            .order_by(Signal.total_score.desc(), Signal.ts.desc())
            .limit(settings.x_candidate_limit * 4)
        )).scalars())

        mints = []
        seen = set()
        for mint in open_mints + [s.token_mint for s in recent_signals]:
            if mint and mint not in seen:
                seen.add(mint)
                mints.append(mint)
            if len(mints) >= settings.x_candidate_limit:
                break

        if not mints:
            return []
        tokens = list((await session.execute(
            select(Token).where(Token.mint.in_(mints))
        )).scalars())
        by_mint = {t.mint: t for t in tokens}
        return [by_mint[m] for m in mints if m in by_mint]


async def _needs_refresh(mint: str, settings: Settings) -> bool:
    async with SessionLocal() as session:
        row = (await session.execute(
            select(XSocialSnapshotV074)
            .where(XSocialSnapshotV074.token_mint == mint)
            .order_by(XSocialSnapshotV074.fetched_at.desc())
            .limit(1)
        )).scalar_one_or_none()
        if row is None:
            return True
        ts = aware(row.fetched_at)
        return ts is None or (datetime.now(timezone.utc) - ts).total_seconds() >= settings.x_snapshot_ttl_seconds


def _query_for(token: Token) -> str:
    parts = [token.mint]
    if token.symbol and len(token.symbol) >= 2:
        clean = re.sub(r"[^A-Za-z0-9_]", "", token.symbol)[:20]
        if clean:
            parts.insert(0, f"${clean}")
            parts.insert(1, clean)
    if token.name:
        name = token.name.replace('"', "")[:40].strip()
        if name:
            parts.insert(0, f'"{name}"')
    return "(" + " OR ".join(dict.fromkeys(parts)) + ") -is:retweet lang:en"


async def _x_search(client: httpx.AsyncClient, settings: Settings, query: str) -> dict:
    response = await client.get(
        SEARCH_URL,
        headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
        params={
            "query": query,
            "max_results": max(10, min(int(settings.x_search_results), 100)),
            "tweet.fields": "created_at,author_id,public_metrics,lang",
            "expansions": "author_id",
            "user.fields": "username,verified,verified_type,public_metrics",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        try:
            body = response.json()
            detail = str(body.get("detail") or body.get("title") or body)[:240]
        except Exception:
            detail = response.text[:240]
        raise RuntimeError(f"X HTTP {response.status_code}: {detail}")
    return response.json()


async def _save_snapshot(token: Token, query: str, payload: dict) -> None:
    posts = payload.get("data") or []
    users = {str(u.get("id")): u for u in (payload.get("includes") or {}).get("users", [])}
    authors = set()
    verified = 0
    likes = reposts = replies = quotes = 0
    max_followers = 0
    ranked = []

    for post in posts:
        author_id = str(post.get("author_id") or "")
        user = users.get(author_id) or {}
        authors.add(author_id)
        if user.get("verified") or user.get("verified_type") not in {None, "none"}:
            verified += 1
        upm = user.get("public_metrics") or {}
        followers = int(upm.get("followers_count") or 0)
        max_followers = max(max_followers, followers)

        pm = post.get("public_metrics") or {}
        l = int(pm.get("like_count") or 0)
        r = int(pm.get("retweet_count") or 0)
        rp = int(pm.get("reply_count") or 0)
        q = int(pm.get("quote_count") or 0)
        likes += l; reposts += r; replies += rp; quotes += q
        rank = l + 2*r + rp + 2*q + min(followers // 1000, 1000)
        # X text is untrusted external content. Store a bounded excerpt only.
        ranked.append((rank, {
            "id": str(post.get("id") or ""),
            "text": str(post.get("text") or "")[:280],
            "created_at": post.get("created_at"),
            "username": user.get("username"),
            "followers": followers,
            "verified": bool(user.get("verified")),
            "likes": l,
            "reposts": r,
            "replies": rp,
            "quotes": q,
        }))

    ranked.sort(key=lambda x: x[0], reverse=True)
    score = _social_score(len(posts), len(authors), verified, likes, reposts, replies, quotes, max_followers)

    async with SessionLocal() as session:
        session.add(XSocialSnapshotV074(
            token_mint=token.mint,
            query=query,
            status="ok",
            post_count=len(posts),
            unique_authors=len(authors),
            verified_authors=verified,
            total_likes=likes,
            total_reposts=reposts,
            total_replies=replies,
            total_quotes=quotes,
            max_author_followers=max_followers,
            social_score=score,
            top_posts_json=json.dumps([x[1] for x in ranked[:6]], separators=(",", ":")),
        ))
        await session.commit()


async def _global_discovery(client: httpx.AsyncClient, settings: Settings) -> int:
    query = '(solana OR SOL) (memecoin OR "meme coin" OR pumpfun OR "pump.fun") -is:retweet lang:en'
    payload = await _x_search(client, settings, query)
    found = []
    for post in payload.get("data") or []:
        for mint in SOLANA_BASE58.findall(str(post.get("text") or "")):
            if mint not in found:
                found.append(mint)
            if len(found) >= 8:
                break
        if len(found) >= 8:
            break

    async with SessionLocal() as session:
        added = 0
        for mint in found:
            if await session.get(Token, mint) is None:
                session.add(Token(mint=mint, source="x_social"))
                added += 1
        session.add(XSocialSnapshotV074(
            token_mint="__GLOBAL__",
            query=query,
            status="discovery",
            post_count=len(payload.get("data") or []),
            unique_authors=0,
            social_score=0.0,
            top_posts_json=json.dumps({"candidate_mints": found}, separators=(",", ":")),
        ))
        await session.commit()
        return added


async def run_x_social(settings: Settings, stop: asyncio.Event) -> None:
    if not settings.x_enabled:
        log.info("X social intelligence disabled")
        return
    if not settings.x_bearer_token:
        log.warning("X social intelligence enabled but X_BEARER_TOKEN is missing; waiting")
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
        return

    log.info("X social intelligence ACTIVE; recent-search enrichment + discovery")
    global_counter = 0
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                used = await _searches_last_hour()
                budget = max(int(settings.x_max_searches_per_hour) - used, 0)
                if budget > 0:
                    candidates = await _candidate_tokens(settings)
                    for token in candidates:
                        if budget <= 0 or stop.is_set():
                            break
                        if not await _needs_refresh(token.mint, settings):
                            continue
                        query = _query_for(token)
                        payload = await _x_search(client, settings, query)
                        await _save_snapshot(token, query, payload)
                        budget -= 1
                        log.info("X social refreshed %s", token.mint[:10])

                    global_counter += 1
                    if settings.x_discovery_enabled and budget > 0 and global_counter % 4 == 0:
                        added = await _global_discovery(client, settings)
                        log.info("X discovery found %d new mint candidate(s)", added)

                async with SessionLocal() as session:
                    state = await session.get(SystemState, "v074_x_social_status")
                    payload = json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "searches_last_hour": await _searches_last_hour(),
                        "hourly_cap": settings.x_max_searches_per_hour,
                    }, separators=(",", ":"))
                    if state is None:
                        session.add(SystemState(key="v074_x_social_status", value=payload))
                    else:
                        state.value = payload
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("X social cycle error: %s", str(exc)[:300])

            try:
                await asyncio.wait_for(stop.wait(), timeout=max(settings.x_poll_seconds, 15.0))
            except asyncio.TimeoutError:
                pass
