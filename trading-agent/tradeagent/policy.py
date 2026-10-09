"""Policy: turns a Jev judgement into an order *intent*. Pure function, no I/O.

Gate (all must hold to open/add):
  setup_quality >= 2, direction confidence > 0.80, risk_state == safe,
  direction != neutral, toxic_flow == False, regime != crisis.
Size: fractional Kelly from Jev's calibrated direction probability, net of
round-trip costs, capped at quarter Kelly. Risk layer clips further.

Escalation to Opus is flagged when any gating confidence < 0.60 or the regime
is crisis. Escalation can only make the book safer: the intent while escalated
is HOLD (or REDUCE/FLATTEN), never a new entry.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import PolicyParams
from .jev_schema import JevDecision
from .state_engine import Snapshot


@dataclass(frozen=True)
class Intent:
    symbol: str
    action: str             # "open" | "reduce" | "flatten" | "hold"
    side: str               # "buy" | "sell" | ""
    notional_usd: float     # >= 0
    reduce_only: bool
    reason: str
    escalate: bool = False
    kelly_fraction: float = 0.0


def kelly_fraction(p: float, params: PolicyParams) -> float:
    """Full-Kelly fraction for a binary TP/SL bet, net of costs. May be <= 0."""
    cost = params.round_trip_cost_bps
    win = params.take_profit_bps - cost
    loss = params.stop_loss_bps + cost
    if win <= 0 or loss <= 0:
        return 0.0
    b = win / loss
    return p - (1 - p) / b


def sized_fraction(p: float, params: PolicyParams) -> float:
    f = kelly_fraction(p, params)
    if f <= 0:
        return 0.0
    return min(f * params.kelly_multiplier, f * 0.25)  # never above quarter Kelly


def decide(snap: Snapshot, d: JevDecision | None, position_qty: float, equity: float,
           params: PolicyParams) -> Intent:
    sym = snap.symbol
    hold = lambda reason, esc=False: Intent(sym, "hold", "", 0.0, True, reason, esc)
    pos_notional = abs(position_qty) * snap.mid
    close_side = "sell" if position_qty > 0 else "buy"

    if d is None:
        return hold("no valid Jev decision (timeout/schema) -> hold")

    gating_conf = min(d.direction.confidence, d.setup_quality.confidence,
                      d.risk_state.confidence, d.regime.confidence)
    escalate = gating_conf < params.escalate_below_conf or d.regime.value == "crisis"

    # Risk-reducing outcomes come first; they never need a confident model.
    if d.regime.value == "crisis":
        if position_qty:
            return Intent(sym, "flatten", close_side, pos_notional, True, "regime=crisis -> flatten", True)
        return hold("regime=crisis -> no entries", True)
    if d.risk_state.value == "reduce" and position_qty:
        return Intent(sym, "reduce", close_side, pos_notional / 2, True, "risk_state=reduce -> halve", escalate)
    if escalate:
        return hold(f"gating confidence {gating_conf:.2f} < {params.escalate_below_conf} -> escalate", True)

    # Exit if Jev confidently says the other way.
    want = d.direction.value
    if position_qty and ((position_qty > 0 and want == "short") or (position_qty < 0 and want == "long")) \
            and d.direction.confidence > params.min_direction_conf:
        return Intent(sym, "flatten", close_side, pos_notional, True, f"direction flipped to {want}")

    # Entry gate.
    if want == "neutral":
        return hold("direction=neutral")
    if d.setup_quality.value < params.min_setup_quality:
        return hold(f"setup_quality {d.setup_quality.value} < {params.min_setup_quality}")
    if not d.direction.confidence > params.min_direction_conf:
        return hold(f"direction conf {d.direction.confidence:.2f} <= {params.min_direction_conf}")
    if d.risk_state.value != "safe":
        return hold(f"risk_state={d.risk_state.value}")
    if d.toxic_flow.value:
        return hold("toxic_flow=true")

    frac = sized_fraction(d.direction.confidence, params)
    if frac <= 0:
        return hold("Kelly <= 0 after costs")
    target = frac * equity
    side = "buy" if want == "long" else "sell"
    same_side_notional = pos_notional if (position_qty > 0) == (want == "long") and position_qty else 0.0
    add = max(0.0, target - same_side_notional)
    if add <= 0:
        return hold("already at target size")
    return Intent(sym, "open", side, add, False, f"gate passed, kelly_frac={frac:.4f}", False, frac)
