"""Portfolio accounting, paper broker, and optional live (ccxt) broker."""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from .config import PolicyParams
from .policy import Intent
from .risk import PortfolioView
from .state_engine import PositionView, Snapshot


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    fee_usd: float
    ts_ms: int
    client_id: str


@dataclass
class Portfolio:
    cash: float
    qty: dict[str, float] = field(default_factory=dict)
    avg: dict[str, float] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    day: str = ""

    def equity(self) -> float:
        return self.cash + sum(q * self.marks.get(s, self.avg.get(s, 0.0)) for s, q in self.qty.items())

    def mark(self, symbol: str, price: float, ts_ms: int) -> None:
        self.marks[symbol] = price
        eq = self.equity()
        today = dt.datetime.fromtimestamp(ts_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.day_start_equity = today, eq
        self.peak_equity = max(self.peak_equity, eq)

    def apply(self, f: Fill) -> None:
        signed = f.qty if f.side == "buy" else -f.qty
        old = self.qty.get(f.symbol, 0.0)
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):
            self.avg[f.symbol] = (abs(old) * self.avg.get(f.symbol, 0.0) + f.qty * f.price) / abs(new)
        elif (old > 0) != (new > 0) and new != 0:
            self.avg[f.symbol] = f.price  # flipped through zero
        self.qty[f.symbol] = new
        if abs(new) < 1e-12:
            self.qty[f.symbol] = 0.0
        self.cash -= signed * f.price + f.fee_usd

    def view(self) -> PortfolioView:
        return PortfolioView(self.equity(), self.peak_equity, self.day_start_equity, dict(self.qty), dict(self.marks))

    def position_view(self, symbol: str) -> PositionView:
        return PositionView(self.qty.get(symbol, 0.0), self.avg.get(symbol, 0.0),
                            self.equity(), self.peak_equity, self.day_start_equity)


class PaperBroker:
    """Fills at the touch plus assumed slippage, pays taker fee. Pessimistic by design."""

    def __init__(self, params: PolicyParams):
        self.params = params

    def execute(self, intent: Intent, snap: Snapshot) -> Fill:
        half_spread = snap.spread_bps / 2
        slip = (half_spread + self.params.slippage_bps) / 1e4
        price = snap.mid * (1 + slip) if intent.side == "buy" else snap.mid * (1 - slip)
        qty = intent.notional_usd / price
        fee = intent.notional_usd * self.params.fee_bps / 1e4
        return Fill(intent.symbol, intent.side, qty, price, fee, snap.asof_ms, uuid.uuid4().hex[:16])


class LiveBroker:
    """Thin ccxt wrapper. Validate on the exchange TESTNET before any real funds.

    Keys come from env (EXCHANGE_API_KEY / EXCHANGE_API_SECRET); use a key with
    trade permission only - never withdrawal permission.
    """

    def __init__(self, exchange_id: str, api_key: str, secret: str, testnet: bool = True):
        import ccxt  # optional dependency

        self.ex = getattr(ccxt, exchange_id)({"apiKey": api_key, "secret": secret, "enableRateLimit": True})
        if testnet:
            self.ex.set_sandbox_mode(True)

    def execute(self, intent: Intent, snap: Snapshot) -> Fill:
        amount = float(self.ex.amount_to_precision(intent.symbol, intent.notional_usd / snap.mid))
        cid = uuid.uuid4().hex[:16]
        params = {"clientOrderId": cid}
        if intent.reduce_only:
            params["reduceOnly"] = True  # honoured on derivatives venues; spot ignores it
        o = self.ex.create_order(intent.symbol, "market", intent.side, amount, None, params)
        price = float(o.get("average") or o.get("price") or snap.mid)
        filled = float(o.get("filled") or amount)
        fee = float((o.get("fee") or {}).get("cost") or 0.0)
        return Fill(intent.symbol, intent.side, filled, price, fee, snap.asof_ms, cid)
