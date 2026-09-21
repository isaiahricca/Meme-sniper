import asyncio
import logging

from app.config import Settings
from app.services.integrity_upgrade import ensure_v06_integrity_epoch
from app.services.pumpportal import run_pumpportal
from app.services.market_data import run_market_data
from app.services.birdeye import run_birdeye
from app.services.wallet_policy import run_wallet_policy
from app.services.helius import run_helius
from app.services.verified_copyability import run_verified_copyability
from app.services.paper_copy_v06 import run_paper_copy_v06
from app.services.signal_runtime_v06 import run_signal_runtime_v06
from app.services.nansen import run_nansen
from app.services.rug_shield import run_rug_shield
from app.services.trader_intelligence import run_trader_intelligence
from app.services.ai_ensemble import run_ai_ensemble
from app.services.x_social import run_x_social
from app.services.candle_sampler import run_candle_sampler
from app.services.forward_test import ensure_forward_epoch

log = logging.getLogger("supervisor")


class Supervisor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop_event = asyncio.Event()
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        # Establish the verified epoch before anything capable of creating new
        # strategy/trader records starts. This prevents a startup race with legacy data.
        epoch = await ensure_v06_integrity_epoch(self.settings)
        log.info("Verified data epoch: %s", epoch.isoformat())
        forward = await ensure_forward_epoch(self.settings)
        log.info("V0.7.4 forward-test epoch: %s", forward.isoformat())

        runners = [
            run_pumpportal(self.settings, self.stop_event),
            run_market_data(self.settings, self.stop_event),
            run_birdeye(self.settings, self.stop_event),
            run_wallet_policy(self.settings, self.stop_event),
            run_helius(self.settings, self.stop_event),
            run_verified_copyability(self.settings, self.stop_event),
            run_rug_shield(self.settings, self.stop_event),
            run_paper_copy_v06(self.settings, self.stop_event),
            run_signal_runtime_v06(self.settings, self.stop_event),
            run_nansen(self.settings, self.stop_event),
            run_trader_intelligence(self.settings, self.stop_event),
            run_ai_ensemble(self.settings, self.stop_event),
            run_x_social(self.settings, self.stop_event),
            run_candle_sampler(self.settings, self.stop_event),
        ]
        self.tasks = [asyncio.create_task(coro) for coro in runners]
        log.info("Started %d V0.7.4 intelligence services", len(self.tasks))

    async def stop(self) -> None:
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
