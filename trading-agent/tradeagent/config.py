"""All thresholds, sizes and limits live here, in code. The model never sets them.

Every dataclass is frozen: nothing at runtime (and nothing the model returns)
can mutate a limit. Changing a limit means changing code or env and restarting,
which goes through the review gate.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

LIVE_CONFIRM_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"


@dataclass(frozen=True)
class RiskLimits:
    max_drawdown_pct: float = 0.15          # from peak equity -> kill switch trips
    max_daily_loss_pct: float = 0.03        # from UTC-day start equity -> reduce-only for the day
    max_position_pct: float = 0.20          # |position notional| / equity, per symbol
    max_gross_exposure_pct: float = 0.50    # sum |notional| / equity, all symbols
    max_order_notional_usd: float = 1_000.0
    min_order_notional_usd: float = 10.0
    max_spread_bps: float = 25.0            # don't open into a wide book
    max_snapshot_age_ms: int = 3_000        # stale data -> no new risk
    max_orders_per_hour: int = 30           # runaway-loop guard


@dataclass(frozen=True)
class PolicyParams:
    min_setup_quality: int = 2              # Jev setup_quality >= 2
    min_direction_conf: float = 0.80        # strictly greater than
    escalate_below_conf: float = 0.60       # any gating field below this -> Opus re-read
    kelly_multiplier: float = 0.25          # quarter Kelly, hard cap
    take_profit_bps: float = 150.0
    stop_loss_bps: float = 100.0
    fee_bps: float = 5.0                    # per side, taker
    slippage_bps: float = 3.0               # per side, assumed

    @property
    def round_trip_cost_bps(self) -> float:
        return 2 * (self.fee_bps + self.slippage_bps)


@dataclass(frozen=True)
class Settings:
    mode: str = "paper"                     # "paper" | "live"
    exchange: str = "binance"
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    poll_seconds: float = 5.0
    starting_equity_usd: float = 10_000.0
    data_dir: str = "logs"
    schema_path: str = "schemas/active.json"
    kill_switch_path: str = "logs/KILL"
    jev_api_url: str = ""
    jev_api_key: str = field(default="", repr=False)
    jev_timeout_s: float = 0.25
    escalation_model: str = "claude-opus-5-5"
    risk: RiskLimits = RiskLimits()
    policy: PolicyParams = PolicyParams()

    @staticmethod
    def from_env() -> "Settings":
        mode = os.getenv("TRADEAGENT_MODE", "paper")
        if mode == "live" and os.getenv("TRADEAGENT_LIVE_CONFIRM") != LIVE_CONFIRM_PHRASE:
            raise SystemExit(
                f"Live mode refused: set TRADEAGENT_LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}"
            )
        symbols = tuple(s.strip() for s in os.getenv("TRADEAGENT_SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip())
        return Settings(
            mode=mode,
            exchange=os.getenv("TRADEAGENT_EXCHANGE", "binance"),
            symbols=symbols,
            poll_seconds=float(os.getenv("TRADEAGENT_POLL_SECONDS", "5")),
            starting_equity_usd=float(os.getenv("TRADEAGENT_START_EQUITY", "10000")),
            jev_api_url=os.getenv("JEV_API_URL", ""),
            jev_api_key=os.getenv("JEV_API_KEY", ""),
        )
