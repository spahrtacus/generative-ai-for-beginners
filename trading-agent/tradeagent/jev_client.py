"""Jev (TypeSafe AI System One model) client: the REFLEX layer.

Jev only judges. It receives the compact snapshot (plus thesis/invalidation)
and returns calibrated probabilities. It never sees limits, sizes, balances or
keys, and nothing it returns is executed without passing policy.py and risk.py.

Wire format (confirmed from the console playground, model jev-1.13.0):
    request : {"model": "jev-latest", "state": {...}, "questions": {name: {"type": "noul",
               "instructions": str, "criteria": {"true": str, "false": str}}}}
    response: {"model": ..., "answers": {name: {"type": "noul", "noul": <P(true)>, "stats": {}}},
               "usage": {...}, "request_id": ..., "evaluation_time_ms": ...}

Every typed field in our schema is compiled into yes/no "noul" questions and
decoded back here (see COMPILE / decode_fields). Endpoint URL and auth header
are env-configurable: JEV_API_URL, JEV_AUTH_HEADER (default "Authorization"),
JEV_AUTH_PREFIX (default "Bearer ").

Any failure (timeout, HTTP error, missing answer) returns None for that symbol,
which policy treats as HOLD. Fail closed, never fail open.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from .jev_schema import JevDecision, SchemaError, parse_decision
from .state_engine import Snapshot

JEV_MODEL = "jev-latest"

# Our typed field -> the yes/no questions Jev answers. (key, instructions, true, false)
COMPILE: dict[str, list[tuple[str, str, str, str]]] = {
    "regime": [
        ("regime_trending", "Is this market in a trending regime right now?",
         "Persistent directional drift: rn_bps large relative to rv_bps.", "No persistent drift."),
        ("regime_mean_reverting", "Is this market mean-reverting right now?",
         "Price oscillates around a level; returns reverse.", "Not oscillating around a level."),
        ("regime_high_vol", "Is volatility abnormally high but still orderly?",
         "rv_bps elevated, book still two-sided.", "Volatility normal."),
        ("regime_crisis", "Is this market in crisis (disorderly, gapping, liquidity vanishing)?",
         "Gaps, very wide spread, one-sided book, extreme rv_bps.", "Orderly market."),
    ],
    "direction": [
        ("dir_long", "Will a long position beat ~16 bps round-trip costs over the next ~1 minute?",
         "Evidence favours price rising more than costs.", "No such evidence."),
        ("dir_short", "Will a short position beat ~16 bps round-trip costs over the next ~1 minute?",
         "Evidence favours price falling more than costs.", "No such evidence."),
    ],
    "toxic_flow": [
        ("toxic_flow", "Does the book show informed, aggressive one-sided flow we would be adversely selected against?",
         "One-sided aggressive flow, thinning opposite side.", "Balanced or benign flow."),
    ],
    "setup_quality": [
        ("setup_tradable", "Is there a tradable setup here (clear enough to risk capital)?",
         "Clean, readable signal consistent with the thesis.", "Noise or conflicting signals."),
        ("setup_strong", "Is the setup strong and clean (top-tier)?",
         "Multiple aligned signals, tight spread, consistent with the thesis.", "Anything less."),
    ],
    "risk_state": [
        ("risk_reduce", "Should exposure be reduced now (invalidation condition met, or drawdown/inventory dangerous)?",
         "Invalidation condition is visible in the state, or dd/inv are dangerous.", "No reason to reduce."),
        ("risk_near_limit", "Is inventory or drawdown approaching a limit?",
         "inv or dd is elevated.", "inv and dd comfortably small."),
    ],
}


class DecisionClient(Protocol):
    def decide(self, schema: dict, snapshots: list[Snapshot]) -> dict[str, JevDecision | None]: ...


def build_request(schema: dict, snap: Snapshot) -> dict:
    fin = next((f for f in schema["finalists"] if f["symbol"] == snap.symbol), {})
    context = schema["instructions"]
    questions = {}
    for field, qs in COMPILE.items():
        desc = schema["fields"].get(field, {}).get("description", "")
        for key, instr, t, f in qs:
            questions[key] = {"type": "noul", "instructions": f"{instr} {desc}".strip(),
                              "criteria": {"true": t, "false": f}}
    return {
        "model": JEV_MODEL,
        "state": {
            "context": context,
            "symbol": snap.symbol,
            "thesis": fin.get("thesis", ""),
            "invalidation": fin.get("invalidation", ""),
            "snapshot": snap.render(),
        },
        "questions": questions,
    }


def _p(answers: dict, key: str) -> float:
    a = answers[key]
    p = a["noul"]
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not 0.0 <= p <= 1.0:
        raise SchemaError(f"{key}: bad noul {p!r}")
    return float(p)


def decode_fields(answers: dict) -> dict[str, dict]:
    """Turn noul probabilities into {field: {value, confidence}} for parse_decision.

    Decoding rules (deterministic, in code):
    - regime: argmax of the four nouls; confidence = winner's share of the total.
    - direction: long/short if that noul > 0.5 and beats the other; confidence = that P.
      Otherwise neutral with confidence 1 - max(P_long, P_short).
    - toxic_flow: P(true) >= 0.5 -> True; confidence = P of the chosen value.
    - setup_quality: 3 if strong>0.5 and tradable>0.5, 2 if tradable>0.5, 1 if tradable>0.25, else 0;
      confidence = P(tradable) for >=2, 1-P(tradable) otherwise.
    - risk_state: reduce if P>0.5, else near_limit if P>0.5, else safe with 1 - max(P).
    """
    reg = {k: _p(answers, f"regime_{k}") for k in ("trending", "mean_reverting", "high_vol", "crisis")}
    tot = sum(reg.values())
    rv = max(reg, key=reg.get)
    regime = {"value": rv, "confidence": reg[rv] / tot if tot > 0 else 0.0}

    pl, ps = _p(answers, "dir_long"), _p(answers, "dir_short")
    if pl > 0.5 and pl > ps:
        direction = {"value": "long", "confidence": pl}
    elif ps > 0.5 and ps > pl:
        direction = {"value": "short", "confidence": ps}
    else:
        direction = {"value": "neutral", "confidence": 1 - max(pl, ps)}

    pt = _p(answers, "toxic_flow")
    toxic = {"value": pt >= 0.5, "confidence": pt if pt >= 0.5 else 1 - pt}

    ptr, pst = _p(answers, "setup_tradable"), _p(answers, "setup_strong")
    q = 3 if (ptr > 0.5 and pst > 0.5) else 2 if ptr > 0.5 else 1 if ptr > 0.25 else 0
    setup = {"value": q, "confidence": ptr if q >= 2 else 1 - ptr}

    pr, pn = _p(answers, "risk_reduce"), _p(answers, "risk_near_limit")
    if pr > 0.5:
        risk = {"value": "reduce", "confidence": pr}
    elif pn > 0.5:
        risk = {"value": "near_limit", "confidence": pn}
    else:
        risk = {"value": "safe", "confidence": 1 - max(pr, pn)}

    return {"regime": regime, "direction": direction, "toxic_flow": toxic,
            "setup_quality": setup, "risk_state": risk}


class HttpJevClient:
    def __init__(self, url: str, api_key: str, timeout_s: float = 1.5):
        if not url or not api_key:
            raise ValueError("JEV_API_URL and JEV_API_KEY are required for HttpJevClient")
        self.url, self.api_key, self.timeout_s = url, api_key, timeout_s
        self.auth_header = os.getenv("JEV_AUTH_HEADER", "Authorization")
        self.auth_prefix = os.getenv("JEV_AUTH_PREFIX", "Bearer ")
        self.last_meta: dict[str, dict] = {}

    def _call(self, payload: dict) -> dict:
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method="POST",
            headers={self.auth_header: f"{self.auth_prefix}{self.api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.loads(resp.read())

    def _one(self, schema: dict, snap: Snapshot) -> JevDecision | None:
        t0 = time.perf_counter()
        try:
            data = self._call(build_request(schema, snap))
            fields = decode_fields(data["answers"])
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError, SchemaError):
            return None
        latency = float(data.get("evaluation_time_ms") or (time.perf_counter() - t0) * 1000)
        self.last_meta[snap.symbol] = {"request_id": data.get("request_id"), "model": data.get("model"),
                                       "usage": data.get("usage")}
        try:
            return parse_decision(snap.symbol, fields, latency, schema["version"])
        except SchemaError:
            return None

    def decide(self, schema: dict, snapshots: list[Snapshot]) -> dict[str, JevDecision | None]:
        """All symbols scored concurrently; each call evaluates all questions in parallel."""
        if not snapshots:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(snapshots))) as ex:
            results = ex.map(lambda s: self._one(schema, s), snapshots)
            return {s.symbol: r for s, r in zip(snapshots, results)}


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
