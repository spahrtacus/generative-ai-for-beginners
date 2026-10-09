"""Typed Jev decision schema + strict response validation.

A schema is a versioned JSON document (schemas/*.json). Each finalist (symbol)
gets its own context block (thesis + invalidation) but shares the same typed
fields, so all symbols can be scored in parallel in one call.

Anything that doesn't validate exactly is treated as "no decision" -> hold.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REGIMES = ("trending", "mean_reverting", "high_vol", "crisis")
DIRECTIONS = ("long", "short", "neutral")
RISK_STATES = ("safe", "near_limit", "reduce")

# Field spec: name -> (kind, allowed values)
FIELDS: dict[str, tuple[str, tuple]] = {
    "regime": ("choice", REGIMES),
    "direction": ("choice", DIRECTIONS),
    "toxic_flow": ("bool", (True, False)),
    "setup_quality": ("score", (0, 1, 2, 3)),
    "risk_state": ("choice", RISK_STATES),
}


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    value: object
    confidence: float  # calibrated probability of `value`, in [0, 1]


@dataclass(frozen=True)
class JevDecision:
    symbol: str
    regime: Field
    direction: Field
    toxic_flow: Field
    setup_quality: Field
    risk_state: Field
    latency_ms: float
    schema_version: str

    @property
    def min_confidence(self) -> float:
        return min(self.regime.confidence, self.direction.confidence,
                   self.setup_quality.confidence, self.risk_state.confidence,
                   self.toxic_flow.confidence)

    def to_dict(self) -> dict:
        d = {"symbol": self.symbol, "latency_ms": self.latency_ms, "schema_version": self.schema_version}
        for name in FIELDS:
            f: Field = getattr(self, name)
            d[name] = {"value": f.value, "confidence": f.confidence}
        return d


def load_schema(path: str | Path) -> dict:
    doc = json.loads(Path(path).read_text())
    validate_schema_doc(doc)
    return doc


def validate_schema_doc(doc: dict) -> None:
    for key in ("version", "instructions", "fields", "finalists"):
        if key not in doc:
            raise SchemaError(f"schema missing '{key}'")
    if set(doc["fields"]) != set(FIELDS):
        raise SchemaError(f"schema fields must be exactly {sorted(FIELDS)}")
    for name, spec in doc["fields"].items():
        kind, allowed = FIELDS[name]
        if spec.get("type") != kind:
            raise SchemaError(f"field {name}: type must be {kind}")
        if kind in ("choice", "score") and tuple(spec.get("values", ())) != allowed:
            raise SchemaError(f"field {name}: values must be {allowed}")
    for fin in doc["finalists"]:
        for key in ("symbol", "thesis", "invalidation"):
            if not fin.get(key):
                raise SchemaError(f"finalist missing '{key}'")


def parse_field(name: str, raw: object) -> Field:
    kind, allowed = FIELDS[name]
    if not isinstance(raw, dict) or "value" not in raw or "confidence" not in raw:
        raise SchemaError(f"{name}: expected {{value, confidence}}")
    value, conf = raw["value"], raw["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0.0 <= conf <= 1.0:
        raise SchemaError(f"{name}: confidence out of range")
    if kind == "bool":
        if not isinstance(value, bool):
            raise SchemaError(f"{name}: expected bool")
    elif kind == "score":
        if isinstance(value, bool) or value not in allowed:
            raise SchemaError(f"{name}: expected one of {allowed}")
    elif value not in allowed:
        raise SchemaError(f"{name}: expected one of {allowed}")
    return Field(value=value, confidence=float(conf))


def parse_decision(symbol: str, raw: dict, latency_ms: float, schema_version: str) -> JevDecision:
    return JevDecision(
        symbol=symbol,
        latency_ms=latency_ms,
        schema_version=schema_version,
        **{name: parse_field(name, raw.get(name)) for name in FIELDS},
    )
