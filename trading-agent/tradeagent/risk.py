"""Hard deterministic risk layer. Checked before EVERY order. No model input.

The risk manager's inputs are: the order intent (side, notional, reduce_only),
portfolio numbers, and the snapshot. It never reads any Jev/Opus field, so no
model output can loosen a limit. It can only reject or shrink an order.

Kill switch: a file on disk. If it exists, only reduce-only orders pass.
It is armed (tripped) automatically on max drawdown and by the operator with
`touch logs/KILL`. It is cleared only by a human deleting the file.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path

from .config import RiskLimits
from .policy import Intent
from .state_engine import Snapshot


@dataclass(frozen=True)
class PortfolioView:
    equity: float
    peak_equity: float
    day_start_equity: float
    position_qty: dict[str, float]
    marks: dict[str, float]

    def gross_exposure(self) -> float:
        return sum(abs(q) * self.marks.get(s, 0.0) for s, q in self.position_qty.items())


@dataclass(frozen=True)
class Verdict:
    approved: bool
    intent: Intent
    reason: str


class KillSwitch:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def is_tripped(self) -> bool:
        return self.path.exists()

    def trip(self, reason: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(f"{int(time.time())} {reason}\n")


class RiskManager:
    def __init__(self, limits: RiskLimits, kill: KillSwitch):
        self.limits = limits
        self.kill = kill
        self._order_times: list[float] = []

    def check(self, intent: Intent, pf: PortfolioView, snap: Snapshot, now_s: float | None = None) -> Verdict:
        L = self.limits
        now_s = time.time() if now_s is None else now_s
        reject = lambda why: Verdict(False, intent, why)

        if intent.action == "hold" or intent.notional_usd <= 0:
            return reject("nothing to do")

        # 1. Drawdown -> arm kill switch (persisted).
        dd = (pf.peak_equity - pf.equity) / pf.peak_equity if pf.peak_equity > 0 else 1.0
        if dd >= L.max_drawdown_pct:
            self.kill.trip(f"max drawdown {dd:.2%} >= {L.max_drawdown_pct:.0%}")

        qty = pf.position_qty.get(intent.symbol, 0.0)
        pos_notional = abs(qty) * snap.mid

        # 2. Reduce-only orders: must actually reduce, never flip. Always allowed otherwise.
        is_reducing = qty != 0 and ((qty > 0 and intent.side == "sell") or (qty < 0 and intent.side == "buy"))
        if intent.reduce_only or self.kill.is_tripped():
            if not is_reducing:
                return reject("kill switch armed: reduce-only" if self.kill.is_tripped() else "reduce-only order would not reduce")
            clipped = min(intent.notional_usd, pos_notional)
            return Verdict(True, replace(intent, notional_usd=clipped, reduce_only=True), "reduce-only ok")

        # --- From here on the order adds risk. ---
        day_loss = (pf.day_start_equity - pf.equity) / pf.day_start_equity if pf.day_start_equity > 0 else 1.0
        if day_loss >= L.max_daily_loss_pct:
            return reject(f"daily loss {day_loss:.2%} >= {L.max_daily_loss_pct:.0%}: reduce-only for the day")
        if snap.age_ms > L.max_snapshot_age_ms:
            return reject(f"stale snapshot {snap.age_ms}ms")
        if snap.spread_bps > L.max_spread_bps:
            return reject(f"spread {snap.spread_bps:.1f}bps > {L.max_spread_bps}")
        self._order_times = [t for t in self._order_times if now_s - t < 3600]
        if len(self._order_times) >= L.max_orders_per_hour:
            return reject("order rate limit")

        notional = min(intent.notional_usd, L.max_order_notional_usd)
        # Per-symbol position cap (post-trade, same side as the order).
        same_side = pos_notional if (qty > 0) == (intent.side == "buy") and qty else -pos_notional
        room_pos = L.max_position_pct * pf.equity - same_side
        room_gross = L.max_gross_exposure_pct * pf.equity - pf.gross_exposure()
        notional = min(notional, room_pos, room_gross)
        if notional < L.min_order_notional_usd:
            return reject("no room under position/exposure caps")

        self._order_times.append(now_s)
        return Verdict(True, replace(intent, notional_usd=notional), "ok")
