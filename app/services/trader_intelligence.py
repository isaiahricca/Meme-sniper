import asyncio
import json
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import NansenSmartTradeV07, TraderIntelligenceV07, SmartMoneyClusterV07, SmartWalletProfile, WalletCopyabilityV06, PaperCopyTradeV06, WalletSwapV06
from app.services.smart_money import hft_penalty, clamp

log = logging.getLogger("trader_intel")
QUOTE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USD1"}


def tier_for(score: float) -> str:
    return "ELITE" if score >= 85 else "STRONG" if score >= 72 else "TESTING" if score >= 60 else "AVOID"


def activity_score(trades: int, volume: float, distinct_tokens: int) -> float:
    if trades <= 0: return 50.0
    return round(clamp(55 + min(math.log10(max(volume,0)+1)*4,24) + min(distinct_tokens*1.5,9) + min(math.log10(trades+1)*3,7)),2)


def paper_score(trades: int, wins: int, pnl_usd: float, notional_usd: float):
    if trades <= 0 or notional_usd <= 0: return None
    win_rate = wins/trades*100
    ret = pnl_usd/notional_usd*100
    return round(clamp(50 + ret*1.4 + (win_rate-50)*0.30),2)


def combined_score(historical, nansen_activity, copy_score, copy_obs, paper, min_copy_obs):
    parts=[]
    if copy_score is not None and copy_obs >= min_copy_obs:
        parts=[(copy_score,.45),(historical,.25)]
        if nansen_activity is not None: parts.append((nansen_activity,.15))
        if paper is not None: parts.append((paper,.15))
    else:
        parts=[(historical,.55)]
        if nansen_activity is not None: parts.append((nansen_activity,.25))
        if paper is not None: parts.append((paper,.20))
    w=sum(x[1] for x in parts)
    return round(clamp(sum(v*wt for v,wt in parts)/w),2)


def cluster_score(wallet_count, avg_trader_score, total_value):
    return round(clamp(30 + min(max(wallet_count-1,0)*10,35) + clamp(avg_trader_score)*.28 + min(math.log10(max(total_value,0)+1)*2.5,12)),2)


async def recalc_traders(settings: Settings):
    now=datetime.now(timezone.utc); cutoff=now-timedelta(hours=24)
    async with SessionLocal() as session:
        profiles=list((await session.execute(select(SmartWalletProfile))).scalars())
        copies={x.wallet:x for x in (await session.execute(select(WalletCopyabilityV06))).scalars()}
        ns=list((await session.execute(select(NansenSmartTradeV07).where(NansenSmartTradeV07.ts>=cutoff))).scalars())
        papers=list((await session.execute(select(PaperCopyTradeV06).where(PaperCopyTradeV06.status=="closed"))).scalars())
        n_by=defaultdict(list); p_by=defaultdict(list)
        for x in ns: n_by[x.wallet].append(x)
        for x in papers: p_by[x.wallet].append(x)
        # Cap the persisted leaderboard by historical score before expensive per-wallet aggregation.
        profiles=sorted(profiles,key=lambda x:x.score or 0,reverse=True)[:settings.trader_universe_max]
        for profile in profiles:
            rows=n_by.get(profile.wallet,[]); volume=sum(float(x.trade_value_usd or 0) for x in rows); distinct=len({x.token_bought_address for x in rows})
            activity=activity_score(len(rows),volume,distinct) if rows else None
            hist=round(clamp((profile.score or 0)-hft_penalty(profile.total_trades_30d,settings.copyability_hft_trades_30d_soft,settings.copyability_hft_trades_30d_hard)),2)
            copy=copies.get(profile.wallet); cscore=copy.copyability_score if copy else None; cobs=copy.eligible_observations if copy else 0
            prs=p_by.get(profile.wallet,[]); wins=sum(1 for x in prs if (x.pnl_usd or 0)>0); pnl=sum(float(x.pnl_usd or 0) for x in prs); notion=sum(float(x.notional_usd or 0) for x in prs)
            ps=paper_score(len(prs),wins,pnl,notion); combined=combined_score(hist,activity,cscore,cobs,ps,settings.copyability_min_observations)
            row=await session.get(TraderIntelligenceV07,profile.wallet)
            if row is None: row=TraderIntelligenceV07(wallet=profile.wallet); session.add(row)
            row.updated_at=now; row.last_seen_at=max((x.ts for x in rows),default=row.last_seen_at); row.display_label=next((x.wallet_label for x in rows if x.wallet_label),row.display_label)
            row.sources_json=json.dumps(sorted(set((profile.source or "unknown").split("+"))))
            row.nansen_trades_24h=len(rows); row.nansen_volume_usd_24h=volume; row.nansen_distinct_tokens_24h=distinct; row.historical_score=hist
            row.copyability_score=cscore; row.copyability_observations=cobs; row.copied_trades=len(prs); row.copied_win_rate_pct=wins/len(prs)*100 if prs else None; row.copied_pnl_usd=pnl; row.paper_score=ps; row.combined_score=combined; row.tier=tier_for(combined)
        await session.commit(); return len(profiles)


async def detect_clusters(settings: Settings):
    now=datetime.now(timezone.utc); cutoff=now-timedelta(seconds=settings.trader_cluster_window_seconds)
    async with SessionLocal() as session:
        ns=list((await session.execute(select(NansenSmartTradeV07).where(NansenSmartTradeV07.ts>=cutoff))).scalars())
        sw=list((await session.execute(select(WalletSwapV06).where(WalletSwapV06.ts>=cutoff,WalletSwapV06.action=="BUY",WalletSwapV06.copy_eligible.is_(True)))).scalars())
        scores={x.wallet:x.combined_score for x in (await session.execute(select(TraderIntelligenceV07))).scalars()}
        g=defaultdict(lambda:{"wallets":set(),"nansen":set(),"helius":set(),"value":0.0,"times":[]})
        for x in ns:
            if str(x.token_bought_symbol or "").upper() in QUOTE_SYMBOLS: continue
            z=g[x.token_bought_address]; z["wallets"].add(x.wallet); z["nansen"].add(x.wallet); z["value"]+=float(x.trade_value_usd or 0); z["times"].append(x.ts)
        for x in sw:
            z=g[x.token_mint]; z["wallets"].add(x.wallet); z["helius"].add(x.wallet); z["times"].append(x.ts)
        created=0; bucket=max(settings.trader_cluster_window_seconds,1)
        for mint,z in g.items():
            if len(z["wallets"])<settings.trader_cluster_min_wallets: continue
            avg=sum(scores.get(w,50) for w in z["wallets"])/len(z["wallets"]); score=cluster_score(len(z["wallets"]),avg,z["value"])
            if score<settings.trader_cluster_min_score: continue
            times=[t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in z["times"]]; first=min(times); last=max(times); key=f"{mint}:{int(first.timestamp())//bucket}"
            row=await session.get(SmartMoneyClusterV07,key)
            if row is None: row=SmartMoneyClusterV07(cluster_key=key,token_mint=mint,first_seen_at=first,last_seen_at=last); session.add(row); created+=1
            row.last_seen_at=last; row.unique_wallets=len(z["wallets"]); row.nansen_wallets=len(z["nansen"]); row.helius_wallets=len(z["helius"]); row.total_trade_value_usd=z["value"]; row.avg_trader_score=avg; row.cluster_score=score; row.wallets_json=json.dumps(sorted(z["wallets"])); row.source_json=json.dumps([x for x,on in (("nansen",z["nansen"]),("helius",z["helius"])) if on])
        await session.commit(); return created


async def run_trader_intelligence(settings: Settings, stop: asyncio.Event):
    log.info("Trader Intelligence active; universe cap=%d cluster=%ds",settings.trader_universe_max,settings.trader_cluster_window_seconds)
    while not stop.is_set():
        try:
            count=await recalc_traders(settings); new=await detect_clusters(settings)
            if new: log.info("Trader Intelligence found %d new cluster(s); %d trader profiles",new,count)
        except asyncio.CancelledError: raise
        except Exception as exc: log.exception("Trader Intelligence error: %s",exc)
        try: await asyncio.wait_for(stop.wait(),timeout=max(settings.trader_intel_refresh_seconds,5))
        except asyncio.TimeoutError: pass
