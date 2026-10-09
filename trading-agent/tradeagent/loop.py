"""The live 24/7 loop: book -> snapshot -> Jev -> policy -> risk -> execute -> log.

Usage:
    python -m tradeagent.loop                 # paper mode, live public market data
    python -m tradeagent.loop --stub          # paper mode with the stub decision client
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Iterator, Protocol

from .broker import LiveBroker, PaperBroker, Portfolio
from .config import Settings
from .escalation import NullEscalator, OpusEscalator
from .jev_client import DecisionClient, HttpJevClient, StubJevClient
from .jev_schema import load_schema
from .policy import Intent, decide
from .risk import KillSwitch, RiskManager
from .state_engine import CausalityError, OrderBook, StateEngine

log = logging.getLogger("tradeagent")
ESCALATION_COOLDOWN_MS = 15 * 60 * 1000


class Feed(Protocol):
    def books(self) -> Iterator[tuple[int, list[OrderBook]]]: ...


class CcxtFeed:
    """Public order books via ccxt (no keys needed)."""

    def __init__(self, exchange_id: str, symbols: Iterable[str], poll_seconds: float, depth: int = 10):
        import ccxt

        self.ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        self.symbols, self.poll, self.depth = list(symbols), poll_seconds, depth

    def books(self) -> Iterator[tuple[int, list[OrderBook]]]:
        while True:
            out = []
            for s in self.symbols:
                try:
                    ob = self.ex.fetch_order_book(s, limit=self.depth)
                except Exception as e:
                    log.warning("book fetch failed %s: %s", s, e)
                    continue
                recv = int(time.time() * 1000)
                ts = int(ob.get("timestamp") or recv)
                out.append(OrderBook(s, ts, tuple(map(tuple, ob["bids"])), tuple(map(tuple, ob["asks"]))))
            yield int(time.time() * 1000), out
            time.sleep(self.poll)


class ReplayFeed:
    def __init__(self, ticks: list[tuple[int, list[OrderBook]]]):
        self.ticks = ticks

    def books(self) -> Iterator[tuple[int, list[OrderBook]]]:
        yield from self.ticks


class Agent:
    def __init__(self, settings: Settings, client: DecisionClient, broker, escalator, schema: dict):
        self.s = settings
        self.client, self.broker, self.escalator, self.schema = client, broker, escalator, schema
        self.pf = Portfolio(cash=settings.starting_equity_usd, peak_equity=settings.starting_equity_usd,
                            day_start_equity=settings.starting_equity_usd)
        self.engines = {sym: StateEngine(sym) for sym in settings.symbols}
        self.risk = RiskManager(settings.risk, KillSwitch(settings.kill_switch_path))
        self.blocked_until: dict[str, int] = {}
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    def _log(self, now_ms: int, record: dict) -> None:
        day = dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        with open(Path(self.s.data_dir) / f"decisions-{day}.jsonl", "a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def step(self, now_ms: int, books: list[OrderBook]) -> list[dict]:
        snaps = []
        for b in books:
            eng = self.engines.get(b.symbol)
            if eng is None:
                continue
            self.pf.mark(b.symbol, (b.bids[0][0] + b.asks[0][0]) / 2 if b.bids and b.asks else self.pf.marks.get(b.symbol, 0), now_ms)
            try:
                snap = eng.update(b, self.pf.position_view(b.symbol), now_ms)
            except CausalityError as e:
                log.error("causality violation, dropping book: %s", e)
                continue
            if snap:
                snaps.append(snap)
        if not snaps:
            return []

        decisions = self.client.decide(self.schema, snaps)
        records = []
        for snap in snaps:
            sym = snap.symbol
            d = decisions.get(sym)
            qty = self.pf.qty.get(sym, 0.0)
            intent = decide(snap, d, qty, self.pf.equity(), self.s.policy)

            # Escalation: off the hot path; blocks new risk while unresolved.
            res = self.escalator.take_result(sym)
            if res:
                if res.action == "resume":
                    self.blocked_until.pop(sym, None)
                elif res.action in ("reduce", "flatten") and qty:
                    n = abs(qty) * snap.mid * (0.5 if res.action == "reduce" else 1.0)
                    intent = Intent(sym, res.action, "sell" if qty > 0 else "buy", n, True, f"opus: {res.rationale[:200]}")
            if intent.escalate and not self.escalator.is_pending(sym):
                self.blocked_until[sym] = now_ms + ESCALATION_COOLDOWN_MS
                self.escalator.submit(sym, {"snapshot": snap.render(), "decision": d.to_dict() if d else None,
                                            "finalist": next((f for f in self.schema["finalists"] if f["symbol"] == sym), None),
                                            "position_qty": qty, "reason": intent.reason})
            if intent.action == "open" and self.blocked_until.get(sym, 0) > now_ms:
                intent = Intent(sym, "hold", "", 0.0, True, "escalation pending -> hold")

            verdict = self.risk.check(intent, self.pf.view(), snap)
            fill = None
            if verdict.approved:
                fill = self.broker.execute(verdict.intent, snap)
                self.pf.apply(fill)
            rec = {"ts": now_ms, "snapshot": snap.to_dict(), "jev": d.to_dict() if d else None,
                   "intent": asdict(intent), "risk": {"approved": verdict.approved, "reason": verdict.reason,
                   "notional": verdict.intent.notional_usd}, "fill": asdict(fill) if fill else None,
                   "equity": self.pf.equity(), "kill": self.risk.kill.is_tripped()}
            self._log(now_ms, rec)
            records.append(rec)
        return records

    def run(self, feed: Feed) -> None:
        for now_ms, books in feed.books():
            self.step(now_ms, books)


def build(settings: Settings, use_stub: bool) -> Agent:
    schema = load_schema(settings.schema_path)
    if use_stub:
        if settings.mode == "live":
            raise SystemExit("StubJevClient is forbidden in live mode")
        client: DecisionClient = StubJevClient()
    else:
        client = HttpJevClient(settings.jev_api_url, settings.jev_api_key, settings.jev_timeout_s)
    if settings.mode == "live":
        broker = LiveBroker(settings.exchange, os.environ["EXCHANGE_API_KEY"], os.environ["EXCHANGE_API_SECRET"],
                            testnet=os.getenv("EXCHANGE_TESTNET", "1") == "1")
    else:
        broker = PaperBroker(settings.policy)
    has_claude = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    escalator = OpusEscalator(settings.escalation_model) if has_claude else NullEscalator()
    return Agent(settings, client, broker, escalator, schema)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true", help="use the stub decision client (paper only)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    agent = build(settings, args.stub)
    log.info("starting mode=%s symbols=%s", settings.mode, settings.symbols)
    agent.run(CcxtFeed(settings.exchange, settings.symbols, settings.poll_seconds))


if __name__ == "__main__":
    main()
