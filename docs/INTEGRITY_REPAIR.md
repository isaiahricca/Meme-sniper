# Paper research integrity repair

This change repairs collection and validation defects in the existing V0.7.4 pipeline. It does not establish profitable trading, change entry/exit thresholds, implement the V0.8 wallet research engine, or enable real execution.

## Changes

- Save short-lived exact-pair receipt history so later updates cannot erase a timely measurement observation. Measurement rules and maximum capture lag remain unchanged.
- Prioritize open/pending trades and imminent distinct measurement pairs. Critical, broad and discovery polling run independently under shared request pacing. Missing batch pairs retry on the next cycle, preventing serial fallback delays from inflating receipt timestamps. Honor numeric Retry-After on market rate limits.
- Prevent discovery or older observations from overwriting newer exact-pair snapshots, reject wrong-chain/unrequested batch results and changed pair base mints.
- Fix wallet-copy seeding after the first 300 processed swaps and count only open trades against entry capacity.
- Reject non-finite fill inputs, missing liquidity, invalid fees, malformed AI decisions and incomplete Claude responses. Expose sanitized Claude error type, message and request ID without changing the configured model.
- Reject future entry snapshots; resolve stale signal positions after their exit grace period as invalid rather than leaving them open indefinitely.
- Use receipt-time candle buckets, deduplicate the same observation, separate pools, and return one selected pool's sampled-price series from the candle endpoint.
- Reject a true live-trading configuration before starting services. The unconditional live execution lock remains.
- Add bounded ongoing telemetry cleanup, Helius queue/failure health and authenticated `/api/data-integrity` diagnostics, including invalid filled exposure.

## Schema and storage

Adds `pair_price_observations` with a pair/time index and nullable `price_candles_v074.last_observed_at`. SQLite startup migration is additive and idempotent. Existing trades, decisions, measurements and strategy epochs are preserved. PostgreSQL installations must add the nullable candle timestamp column before deploying; this repository's production target uses SQLite.

Every 30 seconds maintenance removes at most 5,000 old raw events and 5,000 old receipt rows per table. Retention thresholds are six hours and ten minutes respectively. This is bounded cleanup, not a disk-quota guarantee. At sustained ingestion beyond cleanup throughput, or during downtime, backlog can grow. No automatic VACUUM is introduced.

## Validation and rollout

Run `python -m pytest -q`. Regression tests use temporary SQLite databases and mocked HTTP; no paid provider calls or production writes are required.

Before rollout: back up `/data` using a consistent SQLite backup, preserve current environment values and historical results, and record the deployment boundary as a new data-quality epoch. Do not reuse `FORWARD_EPOCH_KEY` as a casual reset: its existing initializer quarantines open trades. Deploy only after explicit rollout approval. Compare reason-level measurement invalidation, capture latency, request rate, queue lag and storage growth before/after under real load.

## Remaining work

- The exact production Claude 400 error still needs authenticated saved-error access. Diagnostic improvements are not a claim that provider access is repaired. Anthropic documents the error type/message and request identifier at https://platform.claude.com/docs/en/api/errors.
- Receipt timestamps are not upstream price-update timestamps. Real ticks/on-chain swap observations are still needed for stronger execution realism.
- Historical P/L remains split across shadow/verified/time-filtered lanes. Immutable strategy IDs, frozen trade attribution, honest unpriced equity, delayed exit fills and selection-bias-aware evaluation remain required.
- The dashboard now has an integrity API but has not received the full V0.8 redesign.
- Stale/dead pair lifecycle, durable Helius replay, historical wallet reconstruction, independence checks and out-of-sample wallet qualification remain migration work.
- Historical invalid measurements are not retroactively relabelled valid; no lost observations are fabricated.
