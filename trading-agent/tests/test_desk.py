import math

from desk.backtest import (Leg, Trade, apply_concurrency, equity_curve, max_drawdown, metrics,
                           simulate_symbol)
from desk.config import D1, H1, H4, DeskRules, Gates
from desk.signals import bearish_fvgs, flips, is_short_flip, resample

T0 = 1_700_006_400_000  # 2023-11-15 00:00 UTC (day-aligned)
assert T0 % D1 == 0


def seg(t, o, c, n):
    """n hourly bars moving linearly from o to c."""
    bars, prev = [], o
    for k in range(1, n + 1):
        px = o + (c - o) * k / n
        bars.append((t + (k - 1) * H1, prev, max(prev, px) + 0.05, min(prev, px) - 0.05, px, 1.0))
        prev = px
    return bars


def day(d, o, c, high=None, low=None, mid=None):
    t = T0 + d * D1
    bars = seg(t, o, mid, 12) + seg(t + 12 * H1, mid, c, 12) if mid is not None else seg(t, o, c, 24)
    if high is not None:
        b = bars[0]
        bars[0] = (b[0], b[1], high, b[3], b[4], b[5])
    if low is not None:
        b = bars[-1]
        bars[-1] = (b[0], b[1], b[2], low, b[4], b[5])
    return bars


def base_path():
    return (day(0, 100, 105) + day(1, 105, 108) + day(2, 108, 110)
            + day(3, 110, 100, high=112)          # C1, anchor 112
            + day(4, 100, 95, high=105)           # C2
            + day(5, 95, 90, high=100)            # C3 -> short flip at day-5 close
            + day(6, 90, 92, mid=97))             # bounce into a gap zone, close back below


def test_resample_drops_incomplete():
    bars = seg(T0, 1, 2, 30)
    d = resample(bars, D1)
    assert len(d) == 1 and d[0][1] == bars[0][1] and d[0][4] == bars[23][4]
    assert len(resample(bars, H4)) == 7


def test_three_candle_rule():
    c1 = (0, 110, 112, 99, 100, 1)
    c2 = (1, 100, 105, 94, 95, 1)
    c3 = (2, 95, 100, 89, 90, 1)
    assert is_short_flip(c1, c2, c3)
    assert not is_short_flip(c1, (1, 100, 113, 94, 95, 1), c3)   # C2 takes out C1 high
    assert not is_short_flip((0, 100, 112, 99, 110, 1), c2, c3)  # C1 not down
    d = resample(base_path(), D1)
    f = flips(d)
    assert 5 in f and f[5].side == "short" and f[5].anchor == 112


def test_bearish_fvg():
    a = (0, 100, 100.1, 99, 99.2, 1)
    b = (H4, 99.2, 99.3, 97, 97.1, 1)
    c = (2 * H4, 97.1, 98, 96, 96.5, 1)
    z = bearish_fvgs([a, b, c], 0, H4)
    assert len(z) == 1 and z[0].bottom == 98 and z[0].top == 99 and z[0].formed_close_t == 3 * H4


def test_profit_exit_on_bullish_flip():
    path = (base_path() + day(7, 92, 88) + day(8, 88, 84) + day(9, 84, 80)
            + day(10, 80, 83) + day(11, 83, 85) + day(12, 85, 87))
    trades = simulate_symbol("TEST", path, DeskRules())
    assert len(trades) == 1
    t = trades[0]
    flip_close = T0 + 6 * D1
    assert t.open_t >= flip_close + H1          # entry strictly after the flip and a confirming close
    assert all(l.price < 112 for l in t.legs)
    assert t.exit_reason == "bullish_flip" and t.r > 0
    assert math.isclose(t.legs[0].risk, 0.005, rel_tol=1e-9)


def test_anchor_breach_exit_is_a_loss():
    path = base_path() + day(7, 92, 105) + day(8, 105, 114)
    trades = simulate_symbol("TEST", path, DeskRules())
    assert trades and trades[-1].exit_reason == "anchor_close"
    assert trades[-1].r < -0.9                  # ~-1R or worse, beyond 1R possible (no intrabar stop)


def test_costs_and_funding_reduce_pnl():
    path = base_path() + day(7, 92, 105) + day(8, 105, 114)
    free = simulate_symbol("T", path, DeskRules(fee_rate=0, slippage_rate=0))[-1]
    paid = simulate_symbol("T", path, DeskRules())[-1]
    assert paid.pnl < free.pnl
    funded = simulate_symbol("T", path, DeskRules(), funding=[(T0 + 7 * D1, 0.001)])[-1]
    assert funded.pnl > paid.pnl                # shorts receive positive funding


def _trade(open_t, exit_t, r):
    tr = Trade("X", 1.0, False, open_t, [Leg(open_t, 1.0, 0.1, 0.005)])
    tr.exit_t, tr.r, tr.pnl = exit_t, r, r * 0.005
    return tr


def test_concurrency_cap_and_metrics():
    ts = [_trade(i, 100, 1.0) for i in range(5)]
    assert len(apply_concurrency(ts, 3)) == 3
    m = metrics([_trade(i * 10, i * 10 + 5, r) for i, r in enumerate([2, -1, 2, -1] * 10)], Gates())
    assert m["trades"] == 40 and math.isclose(m["win_rate"], 0.5) and math.isclose(m["expectancy_r"], 0.5)
    assert m["pass"]


def test_drawdown():
    curve = [(0, 1.0), (1, 1.2), (2, 0.9), (3, 1.1)]
    assert math.isclose(max_drawdown(curve), 0.25)
    assert equity_curve([_trade(0, 5, 2.0)])[-1][1] == 1.01
