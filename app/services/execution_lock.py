class LiveTradingLocked(RuntimeError):
    pass


def assert_live_trading_allowed() -> None:
    # V0.1 intentionally contains no path around this.
    raise LiveTradingLocked(
        "Live trading is physically disabled in Meme Sniper V0.6. "
        "Only paper trading is implemented."
    )
