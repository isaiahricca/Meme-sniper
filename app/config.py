from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Meme Sniper V0.7.3"
    brand_name: str = "Meme Sniper"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000

    # Remote dashboard security (enable on any public/cloud deployment)
    dashboard_auth_enabled: bool = False
    dashboard_username: str = "meme"
    dashboard_password: str = ""

    database_url: str = "sqlite+aiosqlite:///./memesniper.db"

    # --------------------
    # Helius / Solana
    # --------------------
    helius_enabled: bool = True
    helius_api_key: str = ""
    helius_stream_mode: str = "standard"
    helius_commitment: str = "processed"
    tracked_wallets: str = ""
    helius_tx_worker_count: int = 6
    helius_tx_queue_size: int = 300
    helius_rpc_min_interval_seconds: float = 0.13
    wallet_buy_episode_seconds: int = 300

    # --------------------
    # PumpPortal discovery
    # --------------------
    pumpportal_enabled: bool = False
    pumpportal_api_key: str = ""
    pumpportal_subscribe_new_tokens: bool = True
    pumpportal_subscribe_migrations: bool = True
    pumpportal_subscribe_token_trades: bool = False
    pumpportal_subscribe_account_trades: bool = False
    pumpportal_token_mints: str = ""

    # --------------------
    # DEX Screener / pair integrity
    # --------------------
    dexscreener_enabled: bool = True
    dexscreener_refresh_seconds: int = 10  # broad-token refresh cadence
    market_critical_refresh_seconds: float = 2.0
    market_broad_refresh_seconds: float = 5.0
    market_discovery_refresh_seconds: float = 10.0
    market_recent_token_limit: int = 60
    market_max_active_pairs: int = 180
    market_exact_pair_batch_size: int = 25
    market_min_request_interval_seconds: float = 0.22
    market_max_price_age_seconds: float = 6.0
    pair_discovery_min_liquidity_usd: float = 1_000.0
    min_liquidity_usd: float = 10_000.0

    # --------------------
    # Birdeye / Smart Money
    # --------------------
    birdeye_enabled: bool = False
    birdeye_api_key: str = ""
    birdeye_discovery_score: float = 70.0
    birdeye_min_liquidity_usd: float = 25_000.0
    birdeye_token_scan_limit_per_day: int = 10
    birdeye_wallet_profiles_per_token: int = 2
    birdeye_min_wallet_score: float = 60.0
    birdeye_min_request_interval_seconds: float = 1.05
    smart_wallet_max_tracked: int = 75
    # Research universe can be broader than the strict verified-copy lane.
    research_wallet_min_score: float = 45.0
    smart_wallet_poll_seconds: int = 5
    wallet_policy_refresh_seconds: float = 3.0

    # --------------------
    # Nansen / Trader Intelligence (optional)
    # --------------------
    nansen_enabled: bool = False
    nansen_api_key: str = ""
    # V0.7.2: hard floor/backoff is applied by the Nansen service even if an older
    # .env still says 60 seconds. Nansen is discovery, not our live firehose.
    nansen_poll_seconds: float = 600.0
    nansen_min_poll_seconds: float = 300.0
    nansen_low_credit_threshold: int = 20
    nansen_low_credit_poll_seconds: float = 1800.0
    nansen_zero_credit_poll_seconds: float = 3600.0
    nansen_error_backoff_seconds: float = 300.0
    nansen_per_page: int = 200
    nansen_min_trade_value_usd: float = 1000.0
    nansen_max_token_age_days: int = 30
    nansen_max_market_cap_usd: float = 100000000.0
    nansen_labels: str = "Smart Trader,30D Smart Trader,90D Smart Trader,180D Smart Trader,Fund"
    trader_universe_max: int = 500
    trader_intel_refresh_seconds: float = 15.0
    trader_cluster_window_seconds: int = 45
    trader_cluster_min_wallets: int = 2
    trader_cluster_min_score: float = 60.0

    # --------------------
    # GoPlus / Rug Shield V0.7.2
    # --------------------
    rug_shield_enabled: bool = True
    # GoPlus Solana Token Security can operate with an access token. App key/secret
    # are optional and are only used to obtain a short-lived token when configured.
    goplus_enabled: bool = True
    goplus_access_token: str = ""
    goplus_app_key: str = ""
    goplus_app_secret: str = ""
    goplus_request_timeout_seconds: float = 15.0
    goplus_min_request_interval_seconds: float = 1.0
    rug_shield_refresh_seconds: float = 5.0
    rug_shield_external_ttl_seconds: float = 600.0
    rug_shield_max_state_age_seconds: float = 30.0
    rug_shield_require_external: bool = False
    rug_shield_require_pass: bool = True
    rug_shield_pass_max_score: float = 39.0
    rug_shield_block_min_score: float = 65.0
    rug_shield_emergency_liquidity_drop_pct: float = 30.0
    rug_shield_top10_caution_pct: float = 45.0
    rug_shield_top10_block_pct: float = 70.0

    # --------------------
    # Verified copyability
    # --------------------
    copyability_horizons_seconds: str = "10,30,60,300"
    copyability_min_observations: int = 10
    copyability_target_return_pct: float = 10.0
    copyability_hft_trades_30d_soft: int = 10_000
    copyability_hft_trades_30d_hard: int = 100_000
    copyability_baseline_max_wait_seconds: float = 15.0
    copyability_capture_grace_seconds: float = 20.0
    copyability_max_capture_lag_seconds: float = 8.0
    copyability_extreme_return_pct: float = 1_000.0
    copyability_min_baseline_liquidity_usd: float = 10_000.0
    copyability_refresh_seconds: float = 2.0

    # --------------------
    # Verified smart-wallet paper copy
    # --------------------
    paper_copy_enabled: bool = True
    paper_copy_position_usd: float = 25.0
    paper_copy_entry_delay_seconds: float = 2.0
    paper_copy_entry_deadline_seconds: float = 15.0
    paper_copy_max_hold_seconds: int = 300
    paper_copy_exit_price_grace_seconds: float = 30.0
    paper_copy_take_profit_pct: float = 15.0
    paper_copy_stop_loss_pct: float = 8.0
    paper_copy_fee_bps: float = 100.0
    paper_copy_slippage_bps: float = 75.0
    paper_copy_min_liquidity_usd: float = 50_000.0
    paper_copy_allow_unproven: bool = False
    paper_copy_min_profile_score: float = 75.0
    # V0.7.2 qualification gate. This remains effective even when an older .env
    # contains PAPER_COPY_ALLOW_UNPROVEN=true.
    paper_copy_require_qualified_trader: bool = True
    paper_copy_min_trader_score: float = 75.0
    paper_copy_min_copy_score: float = 65.0
    paper_copy_require_nansen_or_verified: bool = True
    # V0.7.3 profitability gate: copy only wallets whose measured forward returns
    # have enough room to clear our conservative fee/slippage model.
    paper_copy_min_observations: int = 20
    paper_copy_min_positive_30s_rate_pct: float = 55.0
    paper_copy_min_median_60s_pct: float = 5.0
    paper_copy_min_median_300s_pct: float = 7.0
    paper_copy_max_open_trades: int = 5
    paper_copy_daily_loss_limit_usd: float = 15.0
    paper_copy_min_net_edge_pct: float = 2.0
    paper_copy_trailing_activate_pct: float = 8.0
    paper_copy_trailing_retrace_pct: float = 3.0
    paper_copy_exit_on_source_wallet_sell: bool = True
    paper_copy_refresh_seconds: float = 2.0

    # --------------------
    # Verified signal paper strategy
    # --------------------
    paper_starting_balance_usd: float = 20_000.0
    paper_position_usd: float = 25.0
    # Challenge lane sizes down automatically in shallow pools rather than
    # pretending a fixed notional can be filled without huge market impact.
    paper_position_min_usd: float = 100.0
    paper_position_liquidity_fraction: float = 0.005
    paper_signal_reentry_cooldown_seconds: int = 900
    paper_max_open_positions: int = 3
    paper_fee_bps: float = 100.0
    paper_base_slippage_bps: float = 75.0
    paper_latency_ms: int = 350
    paper_entry_score: float = 88.0
    paper_exit_score: float = 42.0
    paper_stop_loss_pct: float = 12.0
    paper_take_profit_pct: float = 35.0
    paper_signal_max_hold_seconds: int = 300
    paper_signal_entry_deadline_seconds: float = 15.0
    paper_signal_exit_price_grace_seconds: float = 30.0
    signal_horizons_seconds: str = "10,30,60,300"
    signal_refresh_seconds: float = 5.0
    # V0.7.3: signals continue to be scored and simulated in SHADOW mode so they
    # keep producing research data without contaminating the main verified P/L.
    paper_signal_shadow_mode: bool = True

    # Clean forward-test epoch used by the dashboard and circuit breaker.
    forward_epoch_key: str = "v073_paper_epoch"

    # This remains physically ignored/blocked in V0.6.
    # --------------------
    # Dual-model AI ensemble (research/shadow only)
    # --------------------
    ai_ensemble_enabled: bool = True
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openai_model: str = "gpt-5.6-terra"
    anthropic_model: str = "claude-sonnet-5"
    ai_candidate_min_score: float = 58.0
    ai_poll_seconds: float = 8.0
    ai_max_candidates_per_cycle: int = 2
    ai_max_analyses_per_hour: int = 20
    ai_request_timeout_seconds: float = 25.0
    ai_max_output_tokens: int = 700

    live_trading_enabled: bool = False

    @property
    def nansen_label_list(self) -> list[str]:
        return [x.strip() for x in self.nansen_labels.split(",") if x.strip()]

    @property
    def tracked_wallet_list(self) -> list[str]:
        return [x.strip() for x in self.tracked_wallets.split(",") if x.strip()]

    @property
    def pumpportal_token_list(self) -> list[str]:
        return [x.strip() for x in self.pumpportal_token_mints.split(",") if x.strip()]

    @staticmethod
    def _parse_positive_ints(value: str, fallback: list[int]) -> list[int]:
        out: list[int] = []
        for item in value.split(","):
            try:
                n = int(item.strip())
                if n > 0:
                    out.append(n)
            except ValueError:
                pass
        return sorted(set(out)) or fallback

    @property
    def copyability_horizons(self) -> list[int]:
        return self._parse_positive_ints(self.copyability_horizons_seconds, [10, 30, 60, 300])

    @property
    def signal_horizons(self) -> list[int]:
        return self._parse_positive_ints(self.signal_horizons_seconds, [10, 30, 60, 300])

    @property
    def helius_wss_url(self) -> str:
        if not self.helius_api_key:
            return ""
        return f"wss://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"

    @property
    def helius_rpc_url(self) -> str:
        if not self.helius_api_key:
            return ""
        return f"https://mainnet.helius-rpc.com/?api-key={self.helius_api_key}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
