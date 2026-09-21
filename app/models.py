from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, DateTime, Text, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    token_mint: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    wallet: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    signature: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text)


class Token(Base):
    __tablename__ = "tokens"

    mint: Mapped[str] = mapped_column(String(80), primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String(40), nullable=True)
    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(50), default="unknown")
    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_h24_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_m5_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    buys_m5: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sells_m5: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pair_address: Mapped[str | None] = mapped_column(String(80), nullable=True)


class TrackedWallet(Base):
    __tablename__ = "tracked_wallets"

    address: Mapped[str] = mapped_column(String(80), primary_key=True)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[float] = mapped_column(Float, default=50.0)
    observed_events: Mapped[int] = mapped_column(Integer, default=0)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    total_score: Mapped[float] = mapped_column(Float)
    momentum_score: Mapped[float] = mapped_column(Float)
    liquidity_score: Mapped[float] = mapped_column(Float)
    flow_score: Mapped[float] = mapped_column(Float)
    wallet_score: Mapped[float] = mapped_column(Float)
    risk_penalty: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(30))


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    notional_usd: Mapped[float] = mapped_column(Float)
    entry_market_price: Mapped[float] = mapped_column(Float)
    entry_fill_price: Mapped[float] = mapped_column(Float)
    exit_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float] = mapped_column(Float)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_score: Mapped[float] = mapped_column(Float)
    exit_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)


class SignalMeasurement(Base):
    __tablename__ = "signal_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    horizon_seconds: Mapped[int] = mapped_column(Integer, index=True)
    baseline_price: Mapped[float] = mapped_column(Float)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class WalletTrade(Base):
    __tablename__ = "wallet_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    wallet: Mapped[str] = mapped_column(String(80), index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    signature: Mapped[str] = mapped_column(String(120), index=True)
    side: Mapped[str] = mapped_column(String(10), index=True)
    token_delta: Mapped[float] = mapped_column(Float)
    observed_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)


class BirdeyeTokenScan(Base):
    __tablename__ = "birdeye_token_scans"

    token_mint: Mapped[str] = mapped_column(String(80), primary_key=True)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    signal_score: Mapped[float] = mapped_column(Float)
    liquidity_usd: Mapped[float] = mapped_column(Float)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    profiles_requested: Mapped[int] = mapped_column(Integer, default=0)
    cu_estimated: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="complete")
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class WalletDiscovery(Base):
    __tablename__ = "wallet_discoveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    wallet: Mapped[str] = mapped_column(String(80), index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    token_realized_pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    token_total_pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    token_volume_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    token_trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")


class SmartWalletProfile(Base):
    __tablename__ = "smart_wallet_profiles"

    wallet: Mapped[str] = mapped_column(String(80), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    tier: Mapped[str] = mapped_column(String(20), default="TESTING", index=True)
    win_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl_usd_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_pnl_usd_30d: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_trades_30d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovery_count: Mapped[int] = mapped_column(Integer, default=1)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    source: Mapped[str] = mapped_column(String(30), default="birdeye")


class WalletTradeMeasurement(Base):
    __tablename__ = "wallet_trade_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_trade_id: Mapped[int] = mapped_column(Integer, index=True)
    wallet: Mapped[str] = mapped_column(String(80), index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    horizon_seconds: Mapped[int] = mapped_column(Integer, index=True)
    baseline_price: Mapped[float] = mapped_column(Float)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class WalletCopyability(Base):
    __tablename__ = "wallet_copyability"

    wallet: Mapped[str] = mapped_column(String(80), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    buy_observations: Mapped[int] = mapped_column(Integer, default=0)
    avg_return_10s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_return_30s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_return_60s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_return_300s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    positive_30s_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_hit_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_target_horizon_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    hft_penalty: Mapped[float] = mapped_column(Float, default=0.0)
    observed_edge_score: Mapped[float] = mapped_column(Float, default=50.0)
    copyability_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    copyability_tier: Mapped[str] = mapped_column(String(20), default="UNPROVEN", index=True)


class PaperCopyTrade(Base):
    __tablename__ = "paper_copy_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_trade_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    wallet: Mapped[str] = mapped_column(String(80), index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    entry_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)

    wallet_profile_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_copy_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_copy_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)

    detected_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    liquidity_at_entry_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    notional_usd: Mapped[float] = mapped_column(Float)
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)

    detection_to_entry_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_slippage_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_slippage_bps: Mapped[float | None] = mapped_column(Float, nullable=True)

    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_favourable_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_adverse_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

# ============================================================================
# V0.6 VERIFIED PIPELINE TABLES
# Legacy V0.1-V0.5 tables above are intentionally preserved for audit/history.
# New profitability and copyability statistics use only the tables below.
# ============================================================================

class SystemState(Base):
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TokenPairState(Base):
    __tablename__ = "token_pair_state_v06"

    token_mint: Mapped[str] = mapped_column(String(80), primary_key=True)
    pair_address: Mapped[str] = mapped_column(String(100), index=True)
    dex_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    base_mint: Mapped[str] = mapped_column(String(80))
    quote_mint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)


class PairLatestPrice(Base):
    __tablename__ = "pair_latest_price_v06"

    pair_address: Mapped[str] = mapped_column(String(100), primary_key=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    base_mint: Mapped[str] = mapped_column(String(80))
    quote_mint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    dex_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    price_usd: Mapped[float] = mapped_column(Float)
    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_h24_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_m5_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    buys_m5: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sells_m5: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(60), default="dexscreener_exact_pair")


class WalletSwapV06(Base):
    __tablename__ = "wallet_swaps_v06"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    wallet: Mapped[str] = mapped_column(String(80), index=True)
    signature: Mapped[str] = mapped_column(String(120), index=True)
    action: Mapped[str] = mapped_column(String(10), index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    token_delta: Mapped[float] = mapped_column(Float)
    quote_mint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    quote_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_mint: Mapped[str | None] = mapped_column(String(80), nullable=True)
    input_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    classification: Mapped[str] = mapped_column(String(40), default="swap")
    copy_eligible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    episode_parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    tracked_at_detection: Mapped[bool] = mapped_column(Boolean, default=True)

    pair_address: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    baseline_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    baseline_delay_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    integrity_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(180), nullable=True)


class WalletSwapMeasurementV06(Base):
    __tablename__ = "wallet_swap_measurements_v06"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    swap_id: Mapped[int] = mapped_column(Integer, index=True)
    wallet: Mapped[str] = mapped_column(String(80), index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    pair_address: Mapped[str] = mapped_column(String(100), index=True)
    horizon_seconds: Mapped[int] = mapped_column(Integer, index=True)
    baseline_price: Mapped[float] = mapped_column(Float)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    capture_lag_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    valid_for_score: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    integrity_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(180), nullable=True)


class WalletCopyabilityV06(Base):
    __tablename__ = "wallet_copyability_v06_verified"

    wallet: Mapped[str] = mapped_column(String(80), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    eligible_observations: Mapped[int] = mapped_column(Integer, default=0)
    excluded_measurements: Mapped[int] = mapped_column(Integer, default=0)

    median_return_10s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_return_30s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_return_60s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_return_300s_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    positive_30s_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_hit_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_target_horizon_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    hft_penalty: Mapped[float] = mapped_column(Float, default=0.0)
    observed_edge_score: Mapped[float] = mapped_column(Float, default=50.0)
    copyability_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    copyability_tier: Mapped[str] = mapped_column(String(20), default="UNPROVEN", index=True)


class PaperCopyTradeV06(Base):
    __tablename__ = "paper_copy_trades_v06_verified"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    swap_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    wallet: Mapped[str] = mapped_column(String(80), index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    pair_address: Mapped[str] = mapped_column(String(100), index=True)

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_eligible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    integrity_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(180), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(180), nullable=True)

    wallet_profile_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_copy_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    wallet_copy_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)

    entry_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_at_entry_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    notional_usd: Mapped[float] = mapped_column(Float)
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    detection_to_entry_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_slippage_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_slippage_bps: Mapped[float | None] = mapped_column(Float, nullable=True)

    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_favourable_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_adverse_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


class SignalMeasurementV06(Base):
    __tablename__ = "signal_measurements_v06_verified"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    pair_address: Mapped[str] = mapped_column(String(100), index=True)
    horizon_seconds: Mapped[int] = mapped_column(Integer, index=True)
    baseline_price: Mapped[float] = mapped_column(Float)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    capture_lag_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    valid_for_score: Mapped[bool] = mapped_column(Boolean, default=False)
    integrity_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    invalid_reason: Mapped[str | None] = mapped_column(String(180), nullable=True)


class SignalPaperTradeV06(Base):
    __tablename__ = "signal_paper_trades_v06_verified"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    token_mint: Mapped[str] = mapped_column(String(80), index=True)
    pair_address: Mapped[str] = mapped_column(String(100), index=True)
    signal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_eligible_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    entry_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    exit_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    integrity_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    exit_reason: Mapped[str | None] = mapped_column(String(180), nullable=True)
    notional_usd: Mapped[float] = mapped_column(Float)
    entry_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_usd: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_score: Mapped[float] = mapped_column(Float)
    max_favourable_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_adverse_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

class NansenSmartTradeV07(Base):
    __tablename__ = "nansen_smart_trades_v07"
    event_key: Mapped[str] = mapped_column(String(260), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    transaction_hash: Mapped[str] = mapped_column(String(140), index=True)
    wallet: Mapped[str] = mapped_column(String(100), index=True)
    wallet_label: Mapped[str | None] = mapped_column(String(180), nullable=True)
    token_bought_address: Mapped[str] = mapped_column(String(100), index=True)
    token_bought_symbol: Mapped[str | None] = mapped_column(String(60), nullable=True)
    token_sold_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    token_sold_symbol: Mapped[str | None] = mapped_column(String(60), nullable=True)
    token_bought_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_bought_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    trade_value_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

class TraderIntelligenceV07(Base):
    __tablename__ = "trader_intelligence_v07"
    wallet: Mapped[str] = mapped_column(String(100), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    display_label: Mapped[str | None] = mapped_column(String(180), nullable=True)
    sources_json: Mapped[str] = mapped_column(Text, default="[]")
    nansen_trades_24h: Mapped[int] = mapped_column(Integer, default=0)
    nansen_volume_usd_24h: Mapped[float] = mapped_column(Float, default=0.0)
    nansen_distinct_tokens_24h: Mapped[int] = mapped_column(Integer, default=0)
    historical_score: Mapped[float] = mapped_column(Float, default=50.0)
    copyability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    copyability_observations: Mapped[int] = mapped_column(Integer, default=0)
    copied_trades: Mapped[int] = mapped_column(Integer, default=0)
    copied_win_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    copied_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    paper_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    combined_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    tier: Mapped[str] = mapped_column(String(20), default="TESTING", index=True)

class SmartMoneyClusterV07(Base):
    __tablename__ = "smart_money_clusters_v07"
    cluster_key: Mapped[str] = mapped_column(String(180), primary_key=True)
    token_mint: Mapped[str] = mapped_column(String(100), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    unique_wallets: Mapped[int] = mapped_column(Integer, default=0)
    nansen_wallets: Mapped[int] = mapped_column(Integer, default=0)
    helius_wallets: Mapped[int] = mapped_column(Integer, default=0)
    total_trade_value_usd: Mapped[float] = mapped_column(Float, default=0.0)
    avg_trader_score: Mapped[float] = mapped_column(Float, default=50.0)
    cluster_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    wallets_json: Mapped[str] = mapped_column(Text, default="[]")
    source_json: Mapped[str] = mapped_column(Text, default="[]")


class TokenRiskV072(Base):
    __tablename__ = "token_risk_v072"

    token_mint: Mapped[str] = mapped_column(String(100), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    external_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    external_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=50.0, index=True)
    status: Mapped[str] = mapped_column(String(20), default="CAUTION", index=True)
    hard_block: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")

    mintable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    freezable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    closable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    non_transferable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    balance_mutable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    creator_malicious: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    transfer_hook_risky: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    top10_unlocked_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    lp_top_unlocked_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    transfer_fee_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_liquidity_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    liquidity_drop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_summary_json: Mapped[str] = mapped_column(Text, default="{}")


class AIEnsembleDecisionV074(Base):
    __tablename__ = "ai_ensemble_decisions_v074"

    signal_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_mint: Mapped[str] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    packet_json: Mapped[str] = mapped_column(Text, default="{}")

    openai_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    openai_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    openai_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    openai_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    openai_expected_edge_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    openai_json: Mapped[str] = mapped_column(Text, default="{}")
    openai_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    claude_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    claude_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    claude_verdict: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    claude_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    claude_expected_edge_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    claude_json: Mapped[str] = mapped_column(Text, default="{}")
    claude_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    consensus: Mapped[str] = mapped_column(String(20), default="UNAVAILABLE", index=True)
    consensus_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
