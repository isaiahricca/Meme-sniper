import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, func

from app.config import Settings
from app.db import SessionLocal
from app.models import (
    SystemState, PaperTrade, PaperCopyTrade, TrackedWallet,
    SmartWalletProfile, WalletDiscovery,
)
from app.services.birdeye import score_wallet, tier_for

log = logging.getLogger("integrity_upgrade")
EPOCH_KEY = "v06_verified_epoch"


async def ensure_v06_integrity_epoch(settings: Settings) -> datetime:
    async with SessionLocal() as session:
        existing = await session.get(SystemState, EPOCH_KEY)
        if existing:
            value = datetime.fromisoformat(existing.value)
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)

        # Legacy V0.1-V0.5 data remains queryable for audit, but no open legacy
        # position is allowed to continue into the verified P/L series.
        legacy_signal = list((await session.execute(
            select(PaperTrade).where(PaperTrade.status == "open")
        )).scalars())
        for row in legacy_signal:
            row.status = "legacy_quarantined"
            row.exit_reason = "v06_integrity_reset"

        legacy_copy = list((await session.execute(
            select(PaperCopyTrade).where(PaperCopyTrade.status.in_(["pending", "open"]))
        )).scalars())
        for row in legacy_copy:
            row.status = "legacy_quarantined"
            row.reject_reason = row.reject_reason or "v06_integrity_reset"

        # Force central policy to explicitly re-evaluate every discovered wallet.
        tracked = list((await session.execute(select(TrackedWallet))).scalars())
        manual = set(settings.tracked_wallet_list)
        for row in tracked:
            if row.address not in manual:
                row.enabled = False

        # Repair the V0.3 off-by-one discovery counter and score bonus.
        repaired = 0
        profiles = list((await session.execute(select(SmartWalletProfile))).scalars())
        for profile in profiles:
            count = (await session.execute(
                select(func.count(func.distinct(WalletDiscovery.token_mint))).where(WalletDiscovery.wallet == profile.wallet)
            )).scalar_one()
            actual = max(int(count or 0), 1)
            latest = (await session.execute(
                select(WalletDiscovery)
                .where(WalletDiscovery.wallet == profile.wallet)
                .order_by(WalletDiscovery.discovered_at.desc())
                .limit(1)
            )).scalar_one_or_none()
            if latest is None:
                profile.discovery_count = actual
                continue
            try:
                tags = json.loads(latest.tags_json or "[]")
            except Exception:
                tags = []
            profile.discovery_count = actual
            profile.score = score_wallet(
                token_realized_pnl=latest.token_realized_pnl_usd or 0.0,
                token_total_pnl=latest.token_total_pnl_usd or 0.0,
                token_volume_usd=latest.token_volume_usd or 0.0,
                token_trade_count=latest.token_trade_count or 0,
                tags=tags,
                win_rate_pct=profile.win_rate_pct,
                wallet_realized_pnl=profile.realized_pnl_usd_30d or 0.0,
                wallet_total_pnl=profile.total_pnl_usd_30d or 0.0,
                wallet_trade_count=profile.total_trades_30d or 0,
                discovery_count=actual,
            )
            profile.tier = tier_for(profile.score)
            repaired += 1

        session.add(SystemState(key=EPOCH_KEY, value=now.isoformat(), updated_at=now))
        await session.commit()
        log.warning(
            "V0.6 verified epoch created. Quarantined %d legacy signal and %d legacy copy position(s); repaired %d wallet profile(s).",
            len(legacy_signal), len(legacy_copy), repaired,
        )
        return now


async def run_integrity_upgrade(settings: Settings, stop: asyncio.Event) -> None:
    try:
        await ensure_v06_integrity_epoch(settings)
    except Exception as exc:
        # This task failing is serious; other services may continue collecting, but
        # verified strategy engines read the epoch and will fail closed without it.
        log.exception("V0.6 integrity upgrade failed: %s", exc)
        return
    await stop.wait()
