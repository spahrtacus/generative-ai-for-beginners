"""Desk rule numbers (see DESK_RULES.md). Frozen: change here, re-run, never tune after seeing results."""
from __future__ import annotations

from dataclasses import dataclass

H1 = 3_600_000
H4 = 4 * H1
D1 = 24 * H1


@dataclass(frozen=True)
class DeskRules:
    risk_pct: float = 0.005            # per entry, fraction of starting equity
    max_adds: int = 2                  # extra legs after the first
    max_notional_per_leg: float = 1.0  # x equity
    max_concurrent: int = 10
    fee_rate: float = 0.0005           # per side
    slippage_rate: float = 0.0005      # per side
    stale_hours: int = 72
    stale_band_r: float = 0.25
    stale_policy: str = "close"        # "close" | "hold"
    prime_lookback_4h: int = 3


@dataclass(frozen=True)
class Gates:
    min_trades: int = 40
    min_win_rate: float = 0.33
    min_expectancy_r: float = 0.4
    max_drawdown: float = 0.20
