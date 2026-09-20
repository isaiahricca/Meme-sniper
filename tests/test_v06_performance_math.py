from datetime import datetime, timezone, timedelta
from app.services.performance_math import calculate_performance


def test_equity_curve_drawdown_and_profit_factor():
    t=datetime(2026,1,1,tzinfo=timezone.utc)
    events=[
        {"ts":t,"pnl":10,"strategy":"Signal","token":"A","id":1},
        {"ts":t+timedelta(seconds=1),"pnl":-5,"strategy":"Smart wallet","token":"B","id":2},
        {"ts":t+timedelta(seconds=2),"pnl":8,"strategy":"Signal","token":"C","id":3},
    ]
    x=calculate_performance(events)
    assert x["realized_pnl_usd"] == 13
    assert x["trades"] == 3
    assert x["wins"] == 2
    assert x["max_drawdown_usd"] == -5
    assert x["profit_factor"] == 3.6
    assert [p["cumulative_pnl"] for p in x["curve"]] == [10,5,13]
