import json
from datetime import timezone
from sqlalchemy import select
from app.config import Settings
from app.db import SessionLocal
from app.models import SystemState, Token, Signal, TrackedWallet, NansenSmartTradeV07, TraderIntelligenceV07, SmartMoneyClusterV07, WalletSwapV06, PaperCopyTradeV06


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))

def _iso(dt):
    dt=_aware(dt); return dt.isoformat() if dt else None

async def status(settings: Settings):
    async with SessionLocal() as session:
        st={x.key:x.value for x in (await session.execute(select(SystemState))).scalars()}
        traders=len(list((await session.execute(select(TraderIntelligenceV07.wallet))).scalars()))
        tracked=len(list((await session.execute(select(TrackedWallet.address).where(TrackedWallet.enabled.is_(True)))).scalars()))
        clusters=len(list((await session.execute(select(SmartMoneyClusterV07.cluster_key))).scalars()))
        return {"nansen_enabled":settings.nansen_enabled,"nansen_configured":bool(settings.nansen_api_key),"nansen_last_poll":st.get("nansen_last_poll"),"nansen_last_error":st.get("nansen_last_error") or None,"nansen_credits_remaining":st.get("nansen_credits_remaining"),"nansen_last_new_trades":int(st.get("nansen_last_new_trades") or 0),"trader_universe":traders,"live_tracked":tracked,"clusters_total":clusters,"universe_cap":settings.trader_universe_max}

async def traders(limit=100):
    async with SessionLocal() as session:
        tracked={x.address:x for x in (await session.execute(select(TrackedWallet))).scalars()}
        rows=list((await session.execute(select(TraderIntelligenceV07).order_by(TraderIntelligenceV07.combined_score.desc()).limit(min(max(limit,1),500)))).scalars())
        return [{"wallet":x.wallet,"label":x.display_label,"sources":json.loads(x.sources_json or "[]"),"score":round(x.combined_score,1),"tier":x.tier,"historical":round(x.historical_score,1),"copy_score":None if x.copyability_score is None else round(x.copyability_score,1),"copy_obs":x.copyability_observations,"paper_trades":x.copied_trades,"paper_win":None if x.copied_win_rate_pct is None else round(x.copied_win_rate_pct,1),"paper_pnl":round(x.copied_pnl_usd,2),"nansen_trades_24h":x.nansen_trades_24h,"nansen_volume_24h":round(x.nansen_volume_usd_24h,2),"distinct_tokens":x.nansen_distinct_tokens_24h,"tracked":bool(tracked.get(x.wallet) and tracked[x.wallet].enabled),"last_seen":_iso(x.last_seen_at)} for x in rows]

async def clusters(limit=50):
    async with SessionLocal() as session:
        rows=list((await session.execute(select(SmartMoneyClusterV07).order_by(SmartMoneyClusterV07.last_seen_at.desc()).limit(min(max(limit,1),100)))).scalars())
        mints={x.token_mint for x in rows}; toks={x.mint:x for x in (await session.execute(select(Token).where(Token.mint.in_(list(mints))))).scalars()} if mints else {}
        return [{"key":x.cluster_key,"token_mint":x.token_mint,"symbol":toks.get(x.token_mint).symbol if toks.get(x.token_mint) else None,"wallets":x.unique_wallets,"nansen":x.nansen_wallets,"helius":x.helius_wallets,"value_usd":round(x.total_trade_value_usd,2),"avg_trader_score":round(x.avg_trader_score,1),"score":round(x.cluster_score,1),"sources":json.loads(x.source_json or "[]"),"first_seen":_iso(x.first_seen_at),"last_seen":_iso(x.last_seen_at)} for x in rows]

async def opportunities(limit=30):
    async with SessionLocal() as session:
        sigs=list((await session.execute(select(Signal).order_by(Signal.ts.desc()).limit(300))).scalars()); latest_sig={}
        for x in sigs: latest_sig.setdefault(x.token_mint,x)
        cls=list((await session.execute(select(SmartMoneyClusterV07).order_by(SmartMoneyClusterV07.last_seen_at.desc()).limit(100))).scalars()); latest_cl={}
        for x in cls: latest_cl.setdefault(x.token_mint,x)
        mints=set(latest_sig)|set(latest_cl); toks={x.mint:x for x in (await session.execute(select(Token).where(Token.mint.in_(list(mints))))).scalars()} if mints else {}
        out=[]
        for mint in mints:
            sg=latest_sig.get(mint); cl=latest_cl.get(mint); tok=toks.get(mint)
            score=.45*sg.total_score+.55*cl.cluster_score if sg and cl else cl.cluster_score if cl else sg.total_score*.85
            out.append({"mint":mint,"symbol":tok.symbol if tok else None,"name":tok.name if tok else None,"score":round(score,1),"signal_score":round(sg.total_score,1) if sg else None,"cluster_score":round(cl.cluster_score,1) if cl else None,"cluster_wallets":cl.unique_wallets if cl else 0,"liquidity":tok.liquidity_usd if tok else None,"m5":tok.price_change_m5_pct if tok else None})
        return sorted(out,key=lambda x:x["score"],reverse=True)[:min(max(limit,1),100)]

async def live_feed(limit=80):
    ev=[]
    async with SessionLocal() as session:
        for x in list((await session.execute(select(NansenSmartTradeV07).order_by(NansenSmartTradeV07.ts.desc()).limit(30))).scalars()): ev.append({"ts":_iso(x.ts),"type":"SMART_MONEY","source":"NANSEN","title":f"{x.wallet_label or x.wallet[:8]} bought {x.token_bought_symbol or x.token_bought_address[:8]}","detail":f"${float(x.trade_value_usd or 0):,.0f} · {x.wallet[:6]}…","mint":x.token_bought_address})
        for x in list((await session.execute(select(SmartMoneyClusterV07).order_by(SmartMoneyClusterV07.last_seen_at.desc()).limit(20))).scalars()): ev.append({"ts":_iso(x.last_seen_at),"type":"CLUSTER","source":"TRADER INTEL","title":f"{x.unique_wallets} smart wallets clustered on {x.token_mint[:8]}…","detail":f"Cluster {x.cluster_score:.1f} · ${x.total_trade_value_usd:,.0f} known Nansen value","mint":x.token_mint})
        for x in list((await session.execute(select(WalletSwapV06).order_by(WalletSwapV06.ts.desc()).limit(25))).scalars()):
            if x.action=="BUY" and x.copy_eligible: ev.append({"ts":_iso(x.ts),"type":"WALLET_BUY","source":"HELIUS","title":f"Tracked wallet bought {x.token_mint[:8]}…","detail":f"{x.wallet[:6]}… · {x.integrity_status}","mint":x.token_mint})
        for x in list((await session.execute(select(PaperCopyTradeV06).order_by(PaperCopyTradeV06.id.desc()).limit(15))).scalars()):
            when=x.exited_at or x.entered_at or x.detected_at; detail=f"P/L {x.pnl_pct:+.2f}%" if x.pnl_pct is not None else (x.reject_reason or x.integrity_status); ev.append({"ts":_iso(when),"type":"PAPER_COPY","source":"EXECUTION","title":f"Copy trade {x.status}: {x.token_mint[:8]}…","detail":detail,"mint":x.token_mint})
        for x in list((await session.execute(select(Token).order_by(Token.discovered_at.desc()).limit(20))).scalars()): ev.append({"ts":_iso(x.discovered_at),"type":"NEW_COIN","source":x.source.upper(),"title":f"New coin {x.symbol or x.mint[:8]}","detail":f"Liquidity ${x.liquidity_usd:,.0f}" if x.liquidity_usd else "Awaiting verified market pair","mint":x.mint})
    ev=[x for x in ev if x.get("ts")]; ev.sort(key=lambda x:x["ts"],reverse=True); return ev[:min(max(limit,1),200)]
