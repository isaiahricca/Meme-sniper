import asyncio
import logging

from sqlalchemy import select

from app.config import Settings
from app.db import SessionLocal
from app.models import SmartWalletProfile, TrackedWallet, WalletCopyabilityV06
from app.services.smart_money import hft_penalty, clamp

log = logging.getLogger("wallet_policy")


def policy_score(profile: SmartWalletProfile, copy: WalletCopyabilityV06 | None, settings: Settings) -> float:
    penalty = hft_penalty(
        profile.total_trades_30d,
        settings.copyability_hft_trades_30d_soft,
        settings.copyability_hft_trades_30d_hard,
    )
    if copy is not None and copy.eligible_observations >= settings.copyability_min_observations:
        # Proven forward evidence dominates historical Birdeye ranking.
        return round(clamp(copy.copyability_score), 2)
    return round(clamp((profile.score or 0.0) - penalty), 2)


def eligible(profile: SmartWalletProfile, copy: WalletCopyabilityV06 | None, settings: Settings) -> bool:
    score = policy_score(profile, copy, settings)

    # Keep the research/observation universe broader than the VERIFIED execution
    # universe. A wallet can be useful evidence without being qualified to copy.
    # The verified paper-copy module still enforces its own >=75 profile/trader
    # floors and >=65 proven copyability score.
    research_floor = max(0.0, min(100.0, float(settings.research_wallet_min_score)))
    if score < research_floor:
        return False

    # Once enough forward evidence says AVOID, stop spending stream capacity on it.
    if copy is not None and copy.eligible_observations >= settings.copyability_min_observations:
        return copy.copyability_tier != "AVOID"

    return True


async def reconcile_wallets(settings: Settings) -> tuple[int, int]:
    manual = set(settings.tracked_wallet_list)
    async with SessionLocal() as session:
        profiles = list((await session.execute(select(SmartWalletProfile))).scalars())
        copies = {
            x.wallet: x for x in (await session.execute(select(WalletCopyabilityV06))).scalars()
        }
        tracked_rows = {
            x.address: x for x in (await session.execute(select(TrackedWallet))).scalars()
        }

        ranked: list[tuple[float, SmartWalletProfile, bool]] = []
        for profile in profiles:
            copy = copies.get(profile.wallet)
            score = policy_score(profile, copy, settings)
            ranked.append((score, profile, eligible(profile, copy, settings)))

        eligible_ranked = sorted(
            [(score, profile) for score, profile, ok in ranked if ok],
            key=lambda x: x[0], reverse=True,
        )
        capacity = max(settings.smart_wallet_max_tracked - len(manual), 0)
        selected = {p.wallet for _, p in eligible_ranked[:capacity]} | manual

        changes = 0
        profile_addresses = set()
        for score, profile, is_eligible in ranked:
            profile_addresses.add(profile.wallet)
            row = tracked_rows.get(profile.wallet)
            if row is None:
                row = TrackedWallet(address=profile.wallet, enabled=False, score=score)
                session.add(row)
                tracked_rows[profile.wallet] = row

            copy = copies.get(profile.wallet)
            tier = copy.copyability_tier if copy else "UNPROVEN"
            should_enable = is_eligible and profile.wallet in selected
            source_label = (profile.source or "historical").title()
            label = f"Policy {tier} / {source_label} {profile.tier}"
            if row.enabled != should_enable or abs((row.score or 0.0) - score) > 0.01 or row.label != label:
                changes += 1
            row.enabled = should_enable
            row.score = score
            row.label = label

        for address in manual:
            row = tracked_rows.get(address)
            if row is None:
                row = TrackedWallet(address=address, label="Manual", enabled=True, score=100.0)
                session.add(row)
                tracked_rows[address] = row
                changes += 1
            else:
                if not row.enabled:
                    changes += 1
                row.enabled = True
                row.score = max(row.score or 0.0, 100.0)
                row.label = "Manual"

        # Disable legacy rows that no longer have a profile/policy basis.
        for address, row in tracked_rows.items():
            if address in manual or address in profile_addresses:
                continue
            if row.enabled:
                changes += 1
            row.enabled = False
            row.label = row.label or "Legacy disabled"

        await session.commit()
        enabled = sum(1 for row in tracked_rows.values() if row.enabled)
        return enabled, changes


async def run_wallet_policy(settings: Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            enabled, changes = await reconcile_wallets(settings)
            if changes:
                log.info("Wallet policy reconciled %d change(s); %d enabled", changes, enabled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Wallet policy error: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(settings.wallet_policy_refresh_seconds, 1.0))
        except asyncio.TimeoutError:
            pass
