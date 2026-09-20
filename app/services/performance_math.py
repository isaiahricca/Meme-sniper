def calculate_performance(events: list[dict]) -> dict:
    events = sorted(events, key=lambda x: (x["ts"], x["strategy"], x["id"]))
    cumulative = high_water = max_drawdown = gross_profit = gross_loss = 0.0
    wins = 0
    curve = []
    for event in events:
        pnl = float(event["pnl"])
        cumulative += pnl
        if pnl > 0:
            wins += 1; gross_profit += pnl
        elif pnl < 0:
            gross_loss += pnl
        high_water = max(high_water, cumulative)
        max_drawdown = min(max_drawdown, cumulative - high_water)
        curve.append({
            "ts": event["ts"].isoformat(), "cumulative_pnl": round(cumulative, 4),
            "trade_pnl": round(pnl, 4), "strategy": event["strategy"],
            "token": event["token"], "wallet": event.get("wallet"), "trade_id": event["id"],
        })
    count = len(events)
    return {
        "realized_pnl_usd": round(cumulative, 2), "trades": count, "wins": wins,
        "win_rate_pct": round(wins / count * 100, 1) if count else 0.0,
        "max_drawdown_usd": round(max_drawdown, 2),
        "profit_factor": round(gross_profit / abs(gross_loss), 2) if gross_loss < 0 else None,
        "gross_profit_usd": round(gross_profit, 2), "gross_loss_usd": round(gross_loss, 2),
        "curve": curve,
    }
