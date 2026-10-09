"""Jev (TypeSafe AI System One model) client: the REFLEX layer.

Jev only judges. It receives the compact snapshot text per symbol and returns
typed, calibrated fields. It never sees limits, sizes, balances or keys, and
nothing it returns is executed without passing policy.py and risk.py.

ADAPTER NOTE: TypeSafe's API is early-access and its wire format is not public.
`build_request()` and `extract_fields()` are the ONLY two functions that know the
wire format. Align them with the docs in your console.typesafe.ai account, then
add a recorded response as a fixture to tests/test_core.py.

Any failure (timeout, HTTP error, schema violation) returns None for that
symbol, which policy treats as HOLD. Fail closed, never fail open.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Protocol

from .jev_schema import JevDecision, SchemaError, parse_decision
from .state_engine import Snapshot


class DecisionClient(Protocol):
    def decide(self, schema: dict, snapshots: list[Snapshot]) -> dict[str, JevDecision | None]: ...


def build_request(schema: dict, snapshots: list[Snapshot]) -> dict:
    """One call, all symbols scored in parallel."""
    finalists = {f["symbol"]: f for f in schema["finalists"]}
    return {
        "schema_version": schema["version"],
        "instructions": schema["instructions"],
        "fields": schema["fields"],
        "items": [
            {
                "id": s.symbol,
                "context": {
                    "thesis": finalists.get(s.symbol, {}).get("thesis", ""),
                    "invalidation": finalists.get(s.symbol, {}).get("invalidation", ""),
                },
                "state": s.render(),
            }
            for s in snapshots
        ],
    }


def extract_fields(response: dict) -> dict[str, dict]:
    """Map the wire response to {symbol: {field: {value, confidence}}}."""
    return {item["id"]: item["fields"] for item in response.get("items", [])}


class HttpJevClient:
    def __init__(self, url: str, api_key: str, timeout_s: float = 0.25):
        if not url or not api_key:
            raise ValueError("JEV_API_URL and JEV_API_KEY are required for HttpJevClient")
        self.url, self.api_key, self.timeout_s = url, api_key, timeout_s

    def decide(self, schema: dict, snapshots: list[Snapshot]) -> dict[str, JevDecision | None]:
        out: dict[str, JevDecision | None] = {s.symbol: None for s in snapshots}
        body = json.dumps(build_request(schema, snapshots)).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return out
        latency_ms = (time.perf_counter() - t0) * 1000
        try:
            fields_by_symbol = extract_fields(payload)
        except (KeyError, TypeError):
            return out
        for sym in out:
            raw = fields_by_symbol.get(sym)
            if raw is None:
                continue
            try:
                out[sym] = parse_decision(sym, raw, latency_ms, schema["version"])
            except SchemaError:
                out[sym] = None
        return out


class StubJevClient:
    """Deterministic stand-in for paper runs, replay tests and CI.

    It is a plain heuristic over the snapshot, with made-up confidences.
    It is NOT an edge and must never be used in live mode (loop.py enforces this).
    """

    def decide(self, schema: dict, snapshots: list[Snapshot]) -> dict[str, JevDecision | None]:
        out = {}
        for s in snapshots:
            if s.rv_bps > 80:
                regime = "crisis"
            elif s.rv_bps > 25:
                regime = "high_vol"
            elif abs(s.ret_n_bps) > 3 * max(s.rv_bps, 1e-9):
                regime = "trending"
            else:
                regime = "mean_reverting"
            signal = 0.6 * s.imbalance + 0.4 * max(-1.0, min(1.0, s.ret_n_bps / 50))
            direction = "long" if signal > 0.15 else "short" if signal < -0.15 else "neutral"
            conf = min(0.95, 0.5 + abs(signal) / 2)
            quality = 3 if abs(signal) > 0.6 else 2 if abs(signal) > 0.35 else 1 if abs(signal) > 0.15 else 0
            risk_state = "reduce" if s.drawdown_pct > 0.10 else "near_limit" if abs(s.inventory_pct) > 0.15 else "safe"
            raw = {
                "regime": {"value": regime, "confidence": 0.75},
                "direction": {"value": direction, "confidence": conf},
                "toxic_flow": {"value": s.spread_bps > 15, "confidence": 0.7},
                "setup_quality": {"value": quality, "confidence": 0.7},
                "risk_state": {"value": risk_state, "confidence": 0.9},
            }
            out[s.symbol] = parse_decision(s.symbol, raw, 0.0, schema["version"])
        return out
