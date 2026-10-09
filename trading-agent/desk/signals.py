"""Pure candle logic: resampling, three-candle flips, bearish FVGs. No I/O.

Bar = (open_time_ms, open, high, low, close, volume). Times are UTC.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import H1

Bar = tuple  # (t, o, h, l, c, v)


def resample(bars: list[Bar], period_ms: int) -> list[Bar]:
    """Aggregate 1H bars into period buckets aligned to UTC. Incomplete buckets are dropped."""
    need = period_ms // H1
    out: list[Bar] = []
    cur: list[Bar] = []
    key = None
    for b in bars:
        k = b[0] // period_ms * period_ms
        if k != key:
            if cur and len(cur) == need:
                out.append(_agg(key, cur))
            cur, key = [], k
        cur.append(b)
    if cur and len(cur) == need:
        out.append(_agg(key, cur))
    return out


def _agg(t: int, bs: list[Bar]) -> Bar:
    return (t, bs[0][1], max(b[2] for b in bs), min(b[3] for b in bs), bs[-1][4], sum(b[5] for b in bs))


@dataclass(frozen=True)
class Flip:
    idx: int            # index of C3 in the daily list
    side: str           # "short" | "long"
    anchor: float       # C1 high (short) / C1 low (long)
    c1_t: int           # C1 open time (impulse start)


def is_short_flip(c1: Bar, c2: Bar, c3: Bar) -> bool:
    return (c1[4] < c1[1] and c2[4] < c1[4] and c2[2] < c1[2]
            and c3[4] < c2[4] and c3[2] < c1[2])


def is_long_flip(c1: Bar, c2: Bar, c3: Bar) -> bool:
    return (c1[4] > c1[1] and c2[4] > c1[4] and c2[3] > c1[3]
            and c3[4] > c2[4] and c3[3] > c1[3])


def flips(daily: list[Bar]) -> dict[int, Flip]:
    """Map daily index (C3) -> Flip. Short flips take precedence (they can't both fire)."""
    out: dict[int, Flip] = {}
    for i in range(2, len(daily)):
        c1, c2, c3 = daily[i - 2], daily[i - 1], daily[i]
        if is_short_flip(c1, c2, c3):
            out[i] = Flip(i, "short", c1[2], c1[0])
        elif is_long_flip(c1, c2, c3):
            out[i] = Flip(i, "long", c1[3], c1[0])
    return out


@dataclass
class Zone:
    bottom: float
    top: float
    formed_close_t: int     # close time of the third 4H bar
    tested: bool = False
    state: str = "open"     # open | used | void


def bearish_fvgs(bars4h: list[Bar], start_t: int, period_ms: int) -> list[Zone]:
    """Bearish FVGs on closed 4H bars with open time >= start_t. Ladder: top to bottom."""
    seg = [b for b in bars4h if b[0] >= start_t]
    zones = []
    for k in range(1, len(seg) - 1):
        a, c = seg[k - 1], seg[k + 1]
        if a[3] > c[2]:
            zones.append(Zone(bottom=c[2], top=a[3], formed_close_t=c[0] + period_ms))
    return sorted(zones, key=lambda z: -z.top)
