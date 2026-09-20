from dataclasses import dataclass
from datetime import datetime, timezone

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYDtr56d7W5V2kVqzA1kqS"
USD1 = "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB"  # optional quote; harmless if unused
QUOTE_MINTS = {WSOL, USDC, USDT, USD1}


@dataclass(frozen=True)
class ParsedSwap:
    action: str
    token_mint: str
    token_delta: float
    quote_mint: str | None
    quote_delta: float | None
    input_mint: str | None
    input_delta: float | None
    classification: str


def _ui_amount(item: dict) -> float:
    ui = item.get("uiTokenAmount") or {}
    raw = ui.get("uiAmountString")
    if raw is None:
        raw = ui.get("uiAmount")
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _account_key_strings(result: dict) -> list[str]:
    tx = result.get("transaction") or {}
    msg = tx.get("message") or {}
    keys = msg.get("accountKeys") or []
    out = []
    for key in keys:
        if isinstance(key, str):
            out.append(key)
        elif isinstance(key, dict):
            value = key.get("pubkey") or key.get("key")
            if value:
                out.append(str(value))
    return out


def _token_deltas(result: dict, wallet: str) -> dict[str, float]:
    meta = result.get("meta") or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    before: dict[str, float] = {}
    after: dict[str, float] = {}

    for item in pre:
        if item.get("owner") == wallet and item.get("mint"):
            mint = str(item["mint"])
            before[mint] = before.get(mint, 0.0) + _ui_amount(item)
    for item in post:
        if item.get("owner") == wallet and item.get("mint"):
            mint = str(item["mint"])
            after[mint] = after.get(mint, 0.0) + _ui_amount(item)

    out: dict[str, float] = {}
    for mint in set(before) | set(after):
        delta = after.get(mint, 0.0) - before.get(mint, 0.0)
        if abs(delta) > 1e-12:
            out[mint] = delta
    return out


def _native_sol_delta(result: dict, wallet: str) -> float:
    """Economic native-SOL delta with fee removed when wallet paid the fee.

    This is important: a token transfer to a tracked wallet plus a 0.000005 SOL
    network fee must not be misclassified as "spent SOL -> bought token".
    """
    meta = result.get("meta") or {}
    keys = _account_key_strings(result)
    try:
        idx = keys.index(wallet)
    except ValueError:
        return 0.0

    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return 0.0

    delta_lamports = int(post[idx]) - int(pre[idx])
    if idx == 0:  # Solana fee payer is the first account key.
        delta_lamports += int(meta.get("fee") or 0)
    return delta_lamports / 1_000_000_000.0


def _collapse_sol_quote(deltas: dict[str, float], native_sol: float) -> dict[str, float]:
    out = dict(deltas)
    wsol = out.pop(WSOL, 0.0)
    combined = wsol + native_sol
    if abs(combined) > 1e-9:
        out[WSOL] = combined
    return out



def same_buy_episode(previous: datetime | None, current: datetime, window_seconds: int) -> bool:
    if previous is None:
        return False
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    gap = (current - previous).total_seconds()
    return 0 <= gap < window_seconds

def parse_wallet_swap(
    tx_payload: dict,
    wallet: str,
    *,
    allow_token_rotation: bool = False,
) -> tuple[ParsedSwap | None, str | None]:
    result = tx_payload.get("result") if isinstance(tx_payload, dict) else None
    if not isinstance(result, dict):
        return None, "missing_transaction"

    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return None, "transaction_failed"

    deltas = _collapse_sol_quote(_token_deltas(result, wallet), _native_sol_delta(result, wallet))
    positives = [(m, d) for m, d in deltas.items() if d > 1e-12]
    negatives = [(m, d) for m, d in deltas.items() if d < -1e-12]

    quote_pos = [(m, d) for m, d in positives if m in QUOTE_MINTS]
    quote_neg = [(m, d) for m, d in negatives if m in QUOTE_MINTS]
    asset_pos = [(m, d) for m, d in positives if m not in QUOTE_MINTS]
    asset_neg = [(m, d) for m, d in negatives if m not in QUOTE_MINTS]

    # Clean directional swap: quote decreased, one non-quote token increased.
    if len(asset_pos) == 1 and len(asset_neg) == 0 and len(quote_neg) == 1 and len(quote_pos) == 0:
        token_mint, token_delta = asset_pos[0]
        quote_mint, quote_delta = quote_neg[0]
        return ParsedSwap(
            action="BUY",
            token_mint=token_mint,
            token_delta=token_delta,
            quote_mint=quote_mint,
            quote_delta=quote_delta,
            input_mint=quote_mint,
            input_delta=quote_delta,
            classification="quote_to_token",
        ), None

    # Clean directional exit: one non-quote token decreased, quote increased.
    if len(asset_neg) == 1 and len(asset_pos) == 0 and len(quote_pos) == 1 and len(quote_neg) == 0:
        token_mint, token_delta = asset_neg[0]
        quote_mint, quote_delta = quote_pos[0]
        return ParsedSwap(
            action="SELL",
            token_mint=token_mint,
            token_delta=token_delta,
            quote_mint=quote_mint,
            quote_delta=quote_delta,
            input_mint=token_mint,
            input_delta=token_delta,
            classification="token_to_quote",
        ), None

    # A token-to-token rotation can be economically meaningful, but it is not a
    # clean copy signal without route valuation. Fail closed unless explicitly enabled.
    if len(asset_pos) == 1 and len(asset_neg) == 1 and not quote_pos and not quote_neg:
        if not allow_token_rotation:
            return None, "token_rotation_excluded"
        target_mint, target_delta = asset_pos[0]
        input_mint, input_delta = asset_neg[0]
        return ParsedSwap(
            action="BUY",
            token_mint=target_mint,
            token_delta=target_delta,
            quote_mint=None,
            quote_delta=None,
            input_mint=input_mint,
            input_delta=input_delta,
            classification="token_rotation",
        ), None

    if not asset_pos and not asset_neg:
        return None, "quote_only_or_no_asset_change"
    return None, "not_directional_swap"
