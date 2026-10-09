"""Escalation to Claude Opus 5.5: the BRAIN's deep re-read, off the hot path.

Triggered when Jev confidence < 0.60 or regime == crisis. Runs in a background
thread; while a symbol is escalated, the loop only allows HOLD / reduce-only.

Opus's answer can only be one of: hold, reduce, flatten, resume.
"resume" just clears the escalation flag; it cannot open a position or change
any limit. The risk layer still checks every resulting order.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass

ACTIONS = ("hold", "reduce", "flatten", "resume")

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "rationale": {"type": "string"},
        "thesis_invalidated": {"type": "boolean"},
    },
    "required": ["action", "rationale", "thesis_invalidated"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are the risk reviewer for an autonomous crypto trading system. A fast model "
    "flagged low confidence or a crisis regime. Read the state, the thesis and its "
    "invalidation condition, and the recent decisions. Choose the most conservative "
    "action the evidence supports. You cannot open positions or change limits. "
    "Prefer 'hold' or 'reduce' when unsure; choose 'resume' only if the flag was clearly noise."
)


@dataclass(frozen=True)
class EscalationResult:
    symbol: str
    action: str
    rationale: str
    thesis_invalidated: bool


class OpusEscalator:
    def __init__(self, model: str = "claude-opus-5-5"):
        import anthropic  # optional dependency

        self.client = anthropic.Anthropic()
        self.model = model
        self._pending: dict[str, threading.Thread] = {}
        self._results: dict[str, EscalationResult] = {}
        self._lock = threading.Lock()

    def submit(self, symbol: str, context: dict) -> None:
        with self._lock:
            if symbol in self._pending:
                return
            t = threading.Thread(target=self._run, args=(symbol, context), daemon=True)
            self._pending[symbol] = t
        t.start()

    def is_pending(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._pending

    def take_result(self, symbol: str) -> EscalationResult | None:
        with self._lock:
            return self._results.pop(symbol, None)

    def _run(self, symbol: str, context: dict) -> None:
        try:
            result = self._ask(symbol, context)
        except Exception as e:  # any failure -> most conservative non-trading answer
            result = EscalationResult(symbol, "hold", f"escalation failed: {type(e).__name__}", False)
        with self._lock:
            self._results[symbol] = result
            self._pending.pop(symbol, None)

    def _ask(self, symbol: str, context: dict) -> EscalationResult:
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(context, sort_keys=True)}],
        )
        if resp.stop_reason != "end_turn":
            return EscalationResult(symbol, "hold", f"stop_reason={resp.stop_reason}", False)
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
        action = data.get("action")
        if action not in ACTIONS:
            action = "hold"
        return EscalationResult(symbol, action, str(data.get("rationale", ""))[:2000],
                                bool(data.get("thesis_invalidated", False)))


class NullEscalator:
    """Used when no Anthropic credentials are configured: escalation == hold until resolved by human."""

    def submit(self, symbol: str, context: dict) -> None:
        pass

    def is_pending(self, symbol: str) -> bool:
        return False

    def take_result(self, symbol: str) -> EscalationResult | None:
        return None
