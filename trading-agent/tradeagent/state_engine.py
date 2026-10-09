"""Deterministic state engine: order book in, one compact numeric snapshot out.

Rules enforced here:
- Strict causality: every field is computed only from data with timestamp <= asof.
  A book stamped in the future (vs. our clock) is rejected; an out-of-order or
  duplicate book is ignored. History buffers only ever append past observations.
- All math (mid, spread, imbalance, realized vol, inventory, drawdown) happens in
  code. The model only ever sees the rendered snapshot.
- The rendered snapshot must fit a 400-token budget; we assert it.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

TOKEN_BUDGET = 400
# Conservative chars-per-token for dense numeric text (real tokenizers do better).
_CHARS_PER_TOKEN = 2.5
# Allow small clock skew between exchange and local clock.
MAX_FUTURE_SKEW_MS = 250


class CausalityError(ValueError):
    """Raised when an input would leak information from the future."""


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    ts_ms: int                                  # exchange timestamp of the book
    bids: tuple[tuple[float, float], ...]       # (price, size), best first
    asks: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class PositionView:
    qty: float              # signed base units
    avg_price: float
    equity: float
    peak_equity: float
    day_start_equity: float


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    asof_ms: int
    book_ts_ms: int
    age_ms: int
    mid: float
    spread_bps: float
    imbalance: float        # (bidvol - askvol) / (bidvol + askvol), top N levels, in [-1, 1]
    micro_bps: float        # microprice offset from mid, bps
    ret_1_bps: float        # last mid-to-mid return
    ret_n_bps: float        # return over the vol window
    rv_bps: float           # realized vol: stdev of per-tick log returns, bps
    n_obs: int
    inventory_pct: float    # signed position notional / equity
    upnl_pct: float         # unrealized pnl / equity
    drawdown_pct: float     # (peak - equity) / peak
    day_pnl_pct: float

    def render(self) -> str:
        """Compact, stable, model-facing text. Field order is fixed (cache friendly)."""
        text = (
            f"sym={self.symbol} t={self.asof_ms} age_ms={self.age_ms} "
            f"mid={self.mid:.6g} spr_bps={self.spread_bps:.2f} imb={self.imbalance:+.3f} "
            f"micro_bps={self.micro_bps:+.2f} r1_bps={self.ret_1_bps:+.2f} "
            f"rn_bps={self.ret_n_bps:+.2f} rv_bps={self.rv_bps:.2f} n={self.n_obs} "
            f"inv={self.inventory_pct:+.4f} upnl={self.upnl_pct:+.4f} "
            f"dd={self.drawdown_pct:.4f} dpnl={self.day_pnl_pct:+.4f}"
        )
        if estimate_tokens(text) > TOKEN_BUDGET:
            raise ValueError("snapshot exceeds token budget")
        return text

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


class StateEngine:
    def __init__(self, symbol: str, depth: int = 5, vol_window: int = 60):
        self.symbol = symbol
        self.depth = depth
        self._mids: deque[tuple[int, float]] = deque(maxlen=vol_window + 1)
        self._last_ts: int | None = None

    def update(self, book: OrderBook, pos: PositionView, now_ms: int) -> Snapshot | None:
        """Returns a snapshot as of `now_ms`, or None if the book is stale/duplicate.

        Raises CausalityError if the book claims to be from the future.
        """
        if book.symbol != self.symbol:
            raise ValueError(f"engine for {self.symbol} got {book.symbol}")
        if book.ts_ms > now_ms + MAX_FUTURE_SKEW_MS:
            raise CausalityError(f"book ts {book.ts_ms} > now {now_ms}")
        if self._last_ts is not None and book.ts_ms <= self._last_ts:
            return None  # out-of-order or duplicate: never rewrite history
        if not book.bids or not book.asks:
            return None

        best_bid, best_ask = book.bids[0][0], book.asks[0][0]
        if best_bid <= 0 or best_ask <= best_bid:
            return None  # crossed/locked/garbage book
        self._last_ts = book.ts_ms

        mid = (best_bid + best_ask) / 2
        spread_bps = (best_ask - best_bid) / mid * 1e4
        bid_vol = sum(s for _, s in book.bids[: self.depth])
        ask_vol = sum(s for _, s in book.asks[: self.depth])
        tot = bid_vol + ask_vol
        imbalance = (bid_vol - ask_vol) / tot if tot > 0 else 0.0
        bsz, asz = book.bids[0][1], book.asks[0][1]
        micro = (best_ask * bsz + best_bid * asz) / (bsz + asz) if (bsz + asz) > 0 else mid
        micro_bps = (micro - mid) / mid * 1e4

        self._mids.append((book.ts_ms, mid))
        mids = [m for _, m in self._mids]
        rets = [math.log(b / a) for a, b in zip(mids, mids[1:])]
        ret_1 = rets[-1] * 1e4 if rets else 0.0
        ret_n = math.log(mids[-1] / mids[0]) * 1e4 if len(mids) > 1 else 0.0
        if len(rets) >= 2:
            mu = sum(rets) / len(rets)
            rv = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) * 1e4
        else:
            rv = 0.0

        eq = pos.equity if pos.equity > 0 else 1e-9
        notional = pos.qty * mid
        upnl = pos.qty * (mid - pos.avg_price) if pos.qty else 0.0
        dd = max(0.0, (pos.peak_equity - pos.equity) / pos.peak_equity) if pos.peak_equity > 0 else 0.0
        day = (pos.equity - pos.day_start_equity) / pos.day_start_equity if pos.day_start_equity > 0 else 0.0

        return Snapshot(
            symbol=self.symbol,
            asof_ms=now_ms,
            book_ts_ms=book.ts_ms,
            age_ms=max(0, now_ms - book.ts_ms),
            mid=mid,
            spread_bps=spread_bps,
            imbalance=imbalance,
            micro_bps=micro_bps,
            ret_1_bps=ret_1,
            ret_n_bps=ret_n,
            rv_bps=rv,
            n_obs=len(mids),
            inventory_pct=notional / eq,
            upnl_pct=upnl / eq,
            drawdown_pct=dd,
            day_pnl_pct=day,
        )
