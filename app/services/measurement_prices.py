from sqlalchemy import select

from app.models import PairPriceObservation, PairLatestPrice


async def first_observation(session, pair_address, due, now):
    """Use the first received exact observation after the deadline, never a future one.

    This is receipt-time sampling, not proof of upstream on-chain freshness.
    Latest-price fallback supports measurements spanning the initial deployment.
    """
    observed = (await session.execute(
        select(PairPriceObservation).where(
            PairPriceObservation.pair_address == pair_address,
            PairPriceObservation.ts >= due,
            PairPriceObservation.ts <= now,
        ).order_by(PairPriceObservation.ts, PairPriceObservation.id).limit(1)
    )).scalar_one_or_none()
    return observed if observed is not None else await session.get(PairLatestPrice, pair_address)
