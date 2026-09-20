import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    TokenRiskV072, PairLatestPrice, TokenPairState, PaperCopyTradeV06,
    SignalPaperTradeV06, SystemState,
)

log = logging.getLogger("rug_shield")
GOPLUS_SOLANA_URL = "https://api.gopluslabs.io/api/v1/solana/token_security"
GOPLUS_TOKEN_URL = "https://api.gopluslabs.io/api/v1/token"


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _on(value) -> bool:
    if isinstance(value, dict):
        value = value.get("status")
    return str(value).strip().lower() in {"1", "true", "yes"}


def _num(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value, default=None):
    x = _num(value, default)
    if x is None:
        return None
    # GoPlus holder percentages are normally 0..1, but tolerate already-percent values.
    return x * 100.0 if 0 <= x <= 1.000001 else x


def _is_burn_or_lock(holder: dict) -> bool:
    tag = str(holder.get("tag") or "").lower()
    return bool(holder.get("is_locked") in {1, "1", True}) or any(k in tag for k in ("burn", "locker", "locked", "raydium authority"))


def _holder_concentration(holders) -> float | None:
    if not isinstance(holders, list):
        return None
    vals=[]
    for h in holders:
        if not isinstance(h, dict) or _is_burn_or_lock(h):
            continue
        v=_pct(h.get("percent"))
        if v is not None and v >= 0:
            vals.append(v)
    return sum(vals) if vals else 0.0


def analyse_goplus(data: dict, settings: Settings) -> dict:
    """Conservative risk score. Hard flags veto every strategy signal."""
    score=0.0; reasons=[]; hard=False
    mintable=_on(data.get("mintable"))
    freezable=_on(data.get("freezable"))
    closable=_on(data.get("closable"))
    non_transferable=_on(data.get("non_transferable"))
    balance_mutable=_on(data.get("balance_mutable_authority"))
    default_frozen=str(data.get("default_account_state") or "")=="2"

    creators=data.get("creator") or []
    if isinstance(creators, dict): creators=[creators]
    creator_mal=any(_on(x.get("malicious_address")) for x in creators if isinstance(x,dict))

    hook=data.get("transfer_hook") or {}
    hook_present=bool(hook.get("address")) if isinstance(hook,dict) else False
    hook_mal=_on(hook.get("malicious_address")) if isinstance(hook,dict) else False
    hook_risky=hook_present or hook_mal

    top10=_holder_concentration(data.get("holders"))
    dex=data.get("dex") or data.get("dex_info") or []
    if isinstance(dex,dict): dex=[dex]
    lp_vals=[]
    for d in dex:
        if isinstance(d,dict):
            v=_holder_concentration(d.get("lp_holders"))
            if v is not None: lp_vals.append(v)
    lp_top=max(lp_vals) if lp_vals else None

    fee=None
    tf=data.get("transfer_fee") or {}
    if isinstance(tf,dict):
        cur=tf.get("current_fee_rate") or {}
        raw=cur.get("fee_rate") if isinstance(cur,dict) else cur
        n=_num(raw)
        if n is not None: fee=n/100.0  # basis points / 100 = percent

    if non_transferable:
        hard=True; score=max(score,100); reasons.append("non_transferable")
    if default_frozen:
        hard=True; score=max(score,100); reasons.append("default_accounts_frozen")
    if creator_mal:
        hard=True; score=max(score,100); reasons.append("malicious_creator")
    if hook_mal:
        hard=True; score=max(score,100); reasons.append("malicious_transfer_hook")
    if balance_mutable:
        hard=True; score=max(score,90); reasons.append("balance_mutable_authority")
    if closable:
        score+=45; reasons.append("closable_token_program")
    if freezable:
        score+=32; reasons.append("freeze_authority_active")
    if mintable:
        score+=22; reasons.append("mint_authority_active")
    if hook_present and not hook_mal:
        score+=20; reasons.append("transfer_hook_present")
    if fee is not None and fee >= 15:
        score+=40; reasons.append(f"high_transfer_fee_{fee:.1f}%")
    elif fee is not None and fee >= 5:
        score+=20; reasons.append(f"transfer_fee_{fee:.1f}%")
    if top10 is not None:
        if top10 >= settings.rug_shield_top10_block_pct:
            hard=True; score=max(score,85); reasons.append(f"top10_unlocked_{top10:.1f}%")
        elif top10 >= settings.rug_shield_top10_caution_pct:
            score+=28; reasons.append(f"top10_unlocked_{top10:.1f}%")
        elif top10 >= 30:
            score+=12; reasons.append(f"holder_concentration_{top10:.1f}%")
    if lp_top is not None:
        if lp_top >= 80:
            hard=True; score=max(score,90); reasons.append(f"lp_unlocked_concentration_{lp_top:.1f}%")
        elif lp_top >= 50:
            score+=30; reasons.append(f"lp_unlocked_concentration_{lp_top:.1f}%")

    score=min(100.0,score)
    return dict(score=score, hard=hard, reasons=reasons, mintable=mintable, freezable=freezable,
                closable=closable, non_transferable=non_transferable, balance_mutable=balance_mutable,
                creator_malicious=creator_mal, transfer_hook_risky=hook_risky,
                top10_unlocked_pct=top10, lp_top_unlocked_pct=lp_top, transfer_fee_pct=fee)


def classify(score: float, hard: bool, settings: Settings) -> str:
    if hard or score >= settings.rug_shield_block_min_score:
        return "BLOCK"
    if score <= settings.rug_shield_pass_max_score:
        return "PASS"
    return "CAUTION"


async def _state(session,key,value):
    row=await session.get(SystemState,key)
    if row is None: session.add(SystemState(key=key,value=str(value)))
    else: row.value=str(value)


class GoPlusClient:
    def __init__(self, settings: Settings):
        self.settings=settings; self.token=settings.goplus_access_token.strip(); self.last_call=0.0

    async def _pace(self):
        wait=self.settings.goplus_min_request_interval_seconds-(time.monotonic()-self.last_call)
        if wait>0: await asyncio.sleep(wait)
        self.last_call=time.monotonic()

    async def _auth_token(self, client:httpx.AsyncClient):
        if self.token or not self.settings.goplus_app_key or not self.settings.goplus_app_secret:
            return self.token
        now=int(time.time())
        raw=f"{self.settings.goplus_app_key}{now}{self.settings.goplus_app_secret}".encode()
        sign=hashlib.sha1(raw).hexdigest()
        await self._pace()
        r=await client.post(GOPLUS_TOKEN_URL,json={"app_key":self.settings.goplus_app_key,"time":now,"sign":sign},timeout=self.settings.goplus_request_timeout_seconds)
        r.raise_for_status(); payload=r.json(); result=payload.get("result") or {}
        self.token=str(result.get("access_token") or result.get("token") or "")
        return self.token

    async def fetch(self, client:httpx.AsyncClient, mint:str) -> dict:
        token=await self._auth_token(client)
        headers={"accept":"application/json"}
        if token: headers["Authorization"]=f"Bearer {token}"
        await self._pace()
        r=await client.get(GOPLUS_SOLANA_URL,params={"contract_addresses":mint},headers=headers,timeout=self.settings.goplus_request_timeout_seconds)
        r.raise_for_status(); payload=r.json()
        if payload.get("code") not in (None,1,"1"):
            raise RuntimeError(f"GoPlus response: {payload.get('message') or payload.get('code')}")
        result=payload.get("result") or {}
        # API may return {mint: {...}} or a direct object.
        if mint in result and isinstance(result[mint],dict): return result[mint]
        if isinstance(result,dict): return result
        return {}


async def _candidates(session):
    mints=set()
    for model in (PaperCopyTradeV06,SignalPaperTradeV06):
        rows=list((await session.execute(select(model.token_mint).where(model.status.in_(["pending","open"])).limit(200))).scalars())
        mints.update(rows)
    return sorted(x for x in mints if x)


async def _update_one(settings:Settings, gp:GoPlusClient, client:httpx.AsyncClient, mint:str):
    now=datetime.now(timezone.utc)
    async with SessionLocal() as session:
        row=await session.get(TokenRiskV072,mint)
        if row is None:
            row=TokenRiskV072(token_mint=mint); session.add(row)
        state=await session.get(TokenPairState,mint)
        latest=await session.get(PairLatestPrice,state.pair_address) if state and state.status=="active" else None
        liq=float(latest.liquidity_usd) if latest and latest.liquidity_usd is not None else None
        prev=row.liquidity_usd
        drop=None
        if prev and liq is not None and prev>0 and liq<prev:
            drop=(prev-liq)/prev*100.0
        row.previous_liquidity_usd=prev; row.liquidity_usd=liq; row.liquidity_drop_pct=drop

        reasons=[]; external_score=0.0; hard=False
        external_fresh=row.external_checked_at and (now-aware(row.external_checked_at)).total_seconds() < settings.rug_shield_external_ttl_seconds and row.external_status=="ok"
        if settings.goplus_enabled and not external_fresh:
            try:
                data=await gp.fetch(client,mint)
                if not data: raise RuntimeError("empty token-security result")
                a=analyse_goplus(data,settings); external_score=a["score"]; hard=a["hard"]; reasons.extend(a["reasons"])
                for name in ("mintable","freezable","closable","non_transferable","balance_mutable","creator_malicious","transfer_hook_risky","top10_unlocked_pct","lp_top_unlocked_pct","transfer_fee_pct"):
                    setattr(row,name,a[name])
                row.external_checked_at=now; row.external_status="ok"
                row.raw_summary_json=json.dumps({k:a[k] for k in a if k not in {"reasons"}},separators=(",",":"))[:4000]
                await _state(session,"goplus_last_error","")
                await _state(session,"goplus_last_success",now.isoformat())
            except Exception as exc:
                safe=str(exc)
                for secret in (settings.goplus_access_token,settings.goplus_app_key,settings.goplus_app_secret):
                    if secret: safe=safe.replace(secret,"***")
                row.external_status="error"; reasons.append("external_security_unavailable")
                await _state(session,"goplus_last_error",safe[:300])
        else:
            # Rehydrate stable external factors from the stored row while the expensive call is cached.
            if row.external_status=="ok":
                cached={
                    "mintable":row.mintable,"freezable":row.freezable,"closable":row.closable,
                    "non_transferable":row.non_transferable,"balance_mutable":row.balance_mutable,
                    "creator_malicious":row.creator_malicious,"transfer_hook_risky":row.transfer_hook_risky,
                    "top10_unlocked_pct":row.top10_unlocked_pct,"lp_top_unlocked_pct":row.lp_top_unlocked_pct,
                    "transfer_fee_pct":row.transfer_fee_pct,
                }
                # Re-score cached flags without pretending unknown fields are safe.
                fake={"mintable":{"status":"1" if cached["mintable"] else "0"},"freezable":{"status":"1" if cached["freezable"] else "0"},
                      "closable":{"status":"1" if cached["closable"] else "0"},"non_transferable":"1" if cached["non_transferable"] else "0",
                      "balance_mutable_authority":{"status":"1" if cached["balance_mutable"] else "0"}}
                a=analyse_goplus(fake,settings); external_score=a["score"]; hard=a["hard"]
                if cached["creator_malicious"]: hard=True; external_score=max(external_score,100); reasons.append("malicious_creator")
                if cached["transfer_hook_risky"]: external_score+=20; reasons.append("transfer_hook_present")
                if cached["top10_unlocked_pct"] is not None:
                    t=float(cached["top10_unlocked_pct"])
                    if t>=settings.rug_shield_top10_block_pct: hard=True; external_score=max(external_score,85); reasons.append(f"top10_unlocked_{t:.1f}%")
                    elif t>=settings.rug_shield_top10_caution_pct: external_score+=28; reasons.append(f"top10_unlocked_{t:.1f}%")
                if cached["lp_top_unlocked_pct"] is not None and float(cached["lp_top_unlocked_pct"])>=50:
                    external_score+=30; reasons.append("lp_concentration")
            elif not settings.goplus_enabled:
                row.external_status="disabled"

        score=min(100.0,external_score)
        if liq is None or liq<=0:
            score=max(score,90); hard=True; reasons.append("liquidity_unavailable")
        elif liq < settings.min_liquidity_usd:
            score=max(score,75); reasons.append(f"liquidity_below_${settings.min_liquidity_usd:.0f}")
        elif liq < settings.paper_copy_min_liquidity_usd:
            score=max(score,48); reasons.append("thin_liquidity")
        if drop is not None and drop >= settings.rug_shield_emergency_liquidity_drop_pct:
            score=max(score,90); hard=True; reasons.append(f"liquidity_drop_{drop:.1f}%")
        elif drop is not None and drop>=15:
            score=max(score,55); reasons.append(f"liquidity_drop_{drop:.1f}%")

        if settings.goplus_enabled and settings.rug_shield_require_external and row.external_status!="ok":
            score=max(score,70); reasons.append("external_security_required")
        row.risk_score=min(100.0,score); row.hard_block=hard; row.status=classify(row.risk_score,hard,settings)
        row.reasons_json=json.dumps(sorted(set(reasons)))
        row.updated_at=now
        await session.commit()


async def risk_gate(session,mint:str,settings:Settings) -> tuple[bool,str,TokenRiskV072|None]:
    if not settings.rug_shield_enabled:
        return True,"rug_shield_disabled",None
    row=await session.get(TokenRiskV072,mint)
    if row is None:
        return False,"rug_shield_pending",None
    updated=aware(row.updated_at)
    if updated is None or (datetime.now(timezone.utc)-updated).total_seconds()>settings.rug_shield_max_state_age_seconds:
        return False,"rug_shield_stale",row
    if settings.goplus_enabled and settings.rug_shield_require_external and row.external_status!="ok":
        return False,"rug_shield_external_unavailable",row
    if row.status=="BLOCK" or row.hard_block:
        return False,"rug_shield_block",row
    if settings.rug_shield_require_pass and row.status!="PASS":
        return False,"rug_shield_caution",row
    return True,"rug_shield_pass",row


async def run_rug_shield(settings:Settings, stop:asyncio.Event):
    if not settings.rug_shield_enabled:
        log.info("Rug Shield disabled"); return
    log.info("Rug Shield active; hard veto enabled; GoPlus=%s", "ON" if settings.goplus_enabled else "OFF")
    gp=GoPlusClient(settings)
    async with httpx.AsyncClient() as client:
        while not stop.is_set():
            try:
                async with SessionLocal() as session:
                    candidates=await _candidates(session)
                for mint in candidates:
                    if stop.is_set(): break
                    await _update_one(settings,gp,client,mint)
                async with SessionLocal() as session:
                    await _state(session,"rug_shield_candidates",len(candidates)); await _state(session,"rug_shield_last_cycle",datetime.now(timezone.utc).isoformat()); await session.commit()
            except asyncio.CancelledError: raise
            except Exception as exc:
                log.exception("Rug Shield cycle failed: %s",exc)
            try: await asyncio.wait_for(stop.wait(),timeout=max(settings.rug_shield_refresh_seconds,2.0))
            except asyncio.TimeoutError: pass
