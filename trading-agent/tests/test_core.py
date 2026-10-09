import json
import math

import pytest

from tradeagent.broker import PaperBroker, Portfolio
from tradeagent.config import PolicyParams, RiskLimits, Settings
from tradeagent.jev_client import COMPILE, HttpJevClient, StubJevClient, build_request, decode_fields
from tradeagent.jev_schema import SchemaError, load_schema, parse_decision
from tradeagent.loop import Agent, ReplayFeed
from tradeagent.escalation import NullEscalator
from tradeagent.policy import Intent, decide, kelly_fraction, sized_fraction
from tradeagent.review.nightly import brier, build_report
from tradeagent.risk import KillSwitch, PortfolioView, RiskManager
from tradeagent.state_engine import (TOKEN_BUDGET, CausalityError, OrderBook, PositionView,
                                     StateEngine, estimate_tokens)

SYM = "BTC/USDT"
POS0 = PositionView(0.0, 0.0, 10_000, 10_000, 10_000)


def book(ts, bid=100.0, ask=100.02, bsz=5.0, asz=5.0):
    return OrderBook(SYM, ts, ((bid, bsz), (bid - 0.01, 3)), ((ask, asz), (ask + 0.01, 3)))


def snap_at(ts=1_000, **kw):
    return StateEngine(SYM).update(book(ts, **kw), POS0, ts)


def decision(direction="long", dconf=0.9, quality=3, risk="safe", regime="trending", toxic=False, conf=0.9):
    raw = {"regime": {"value": regime, "confidence": conf},
           "direction": {"value": direction, "confidence": dconf},
           "toxic_flow": {"value": toxic, "confidence": conf},
           "setup_quality": {"value": quality, "confidence": conf},
           "risk_state": {"value": risk, "confidence": conf}}
    return parse_decision(SYM, raw, 1.0, "test")


# ---------- state engine ----------

def test_future_book_rejected():
    with pytest.raises(CausalityError):
        StateEngine(SYM).update(book(10_000), POS0, now_ms=1_000)


def test_out_of_order_ignored_and_history_not_rewritten():
    eng = StateEngine(SYM)
    assert eng.update(book(2_000), POS0, 2_000) is not None
    assert eng.update(book(1_500, bid=50, ask=50.1), POS0, 2_100) is None
    s = eng.update(book(3_000), POS0, 3_000)
    assert s.n_obs == 2 and abs(s.ret_1_bps) < 1e-9


def test_snapshot_values_and_token_budget():
    s = snap_at(bsz=9, asz=1)
    assert math.isclose(s.mid, 100.01)
    assert math.isclose(s.spread_bps, 0.02 / 100.01 * 1e4)
    assert s.imbalance > 0
    assert estimate_tokens(s.render()) < TOKEN_BUDGET


def test_crossed_book_dropped():
    assert StateEngine(SYM).update(book(1_000, bid=101, ask=100), POS0, 1_000) is None


# ---------- schema ----------

def test_schema_rejects_bad_values():
    with pytest.raises(SchemaError):
        decision(direction="up")
    with pytest.raises(SchemaError):
        decision(dconf=1.5)
    with pytest.raises(SchemaError):
        decision(quality=True)


def test_active_schema_valid_and_request_shape():
    schema = load_schema("schemas/active.json")
    req = build_request(schema, snap_at())
    assert req["model"] == "jev-latest"
    assert req["state"]["symbol"] == SYM and req["state"]["thesis"]
    n_q = sum(len(v) for v in COMPILE.values())
    assert len(req["questions"]) == n_q == 11
    for q in req["questions"].values():
        assert q["type"] == "noul" and set(q["criteria"]) == {"true", "false"}


# Exact shape returned by the console playground (jev-1.13.0), 2026-10-09.
PLAYGROUND_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {"new_noul_1": {"type": "noul", "noul": 0.52, "stats": {}}},
    "usage": {"input_tokens": 288, "output_tokens": 24},
    "request_id": "playground_example",
    "evaluation_time_ms": 98.0,
}


def answers(**p):
    base = {k: 0.1 for qs in COMPILE.values() for k, *_ in qs}
    base.update(p)
    return {k: {"type": "noul", "noul": v, "stats": {}} for k, v in base.items()}


def test_decode_long_setup():
    f = decode_fields(answers(regime_trending=0.8, dir_long=0.9, setup_tradable=0.85, setup_strong=0.7))
    d = parse_decision(SYM, f, 1.0, "v")
    assert d.regime.value == "trending" and d.direction.value == "long"
    assert math.isclose(d.direction.confidence, 0.9)
    assert d.setup_quality.value == 3 and d.risk_state.value == "safe"
    assert d.toxic_flow.value is False


def test_decode_neutral_and_reduce():
    f = decode_fields(answers(dir_long=0.55, dir_short=0.6, risk_reduce=0.7))
    assert f["direction"]["value"] == "short"
    f = decode_fields(answers(dir_long=0.4, dir_short=0.45))
    assert f["direction"] == {"value": "neutral", "confidence": 0.55}
    assert decode_fields(answers(risk_reduce=0.7))["risk_state"]["value"] == "reduce"


def test_decode_rejects_missing_or_bad():
    with pytest.raises(KeyError):
        decode_fields(PLAYGROUND_RESPONSE["answers"])  # missing our questions
    with pytest.raises(SchemaError):
        decode_fields(answers(dir_long=1.7))


def test_http_client_roundtrip(monkeypatch):
    client = HttpJevClient("https://example.invalid/v1/evaluate", "XXXX")
    sent = {}

    def fake_call(payload):
        sent.update(payload)
        return {**PLAYGROUND_RESPONSE, "answers": answers(dir_long=0.9, setup_tradable=0.9, regime_trending=0.9)}

    monkeypatch.setattr(client, "_call", fake_call)
    out = client.decide(load_schema("schemas/active.json"), [snap_at()])
    d = out[SYM]
    assert sent["model"] == "jev-latest" and d.direction.value == "long" and d.latency_ms == 98.0
    assert client.last_meta[SYM]["request_id"] == "playground_example"


def test_http_client_fails_closed(monkeypatch):
    client = HttpJevClient("https://example.invalid/v1/evaluate", "XXXX")
    monkeypatch.setattr(client, "_call", lambda p: PLAYGROUND_RESPONSE)  # answers missing
    assert client.decide(load_schema("schemas/active.json"), [snap_at()]) == {SYM: None}

    def boom(p):
        raise TimeoutError

    monkeypatch.setattr(client, "_call", boom)
    assert client.decide(load_schema("schemas/active.json"), [snap_at()]) == {SYM: None}


# ---------- policy ----------

P = PolicyParams()


def test_kelly_net_of_costs_and_quarter_cap():
    # breakeven p = loss/(win+loss) = (100+16)/(150-16+100+16) = 0.464
    assert kelly_fraction(0.45, P) < 0
    assert kelly_fraction(0.48, P) > 0
    f = kelly_fraction(0.9, P)
    assert math.isclose(sized_fraction(0.9, P), 0.25 * f)


@pytest.mark.parametrize("kw,reason", [
    (dict(quality=1), "setup_quality"),
    (dict(dconf=0.80), "direction conf"),
    (dict(risk="near_limit"), "risk_state"),
    (dict(toxic=True), "toxic_flow"),
    (dict(direction="neutral"), "neutral"),
])
def test_gate_blocks(kw, reason):
    it = decide(snap_at(), decision(**kw), 0.0, 10_000, P)
    assert it.action == "hold" and reason in it.reason


def test_gate_passes_and_sizes():
    it = decide(snap_at(), decision(), 0.0, 10_000, P)
    assert it.action == "open" and it.side == "buy"
    assert math.isclose(it.notional_usd, sized_fraction(0.9, P) * 10_000)


def test_low_confidence_escalates_and_holds():
    it = decide(snap_at(), decision(conf=0.5), 0.0, 10_000, P)
    assert it.escalate and it.action == "hold"


def test_crisis_flattens():
    it = decide(snap_at(), decision(regime="crisis"), 1.0, 10_000, P)
    assert it.action == "flatten" and it.reduce_only and it.escalate


def test_no_decision_holds():
    assert decide(snap_at(), None, 0.0, 10_000, P).action == "hold"


# ---------- risk ----------

def pf(equity=10_000, peak=10_000, day=10_000, qty=0.0):
    return PortfolioView(equity, peak, day, {SYM: qty}, {SYM: 100.01})


def open_intent(n=500, side="buy"):
    return Intent(SYM, "open", side, n, False, "t")


def test_drawdown_trips_kill_and_blocks_new_risk(tmp_path):
    rm = RiskManager(RiskLimits(), KillSwitch(tmp_path / "KILL"))
    v = rm.check(open_intent(), pf(equity=8_400, peak=10_000, day=8_400), snap_at())
    assert not v.approved and rm.kill.is_tripped()
    # reduce-only still allowed under kill switch
    v = rm.check(Intent(SYM, "flatten", "sell", 1e6, True, "t"), pf(equity=8_400, qty=2.0), snap_at())
    assert v.approved and v.intent.notional_usd <= 2.0 * 100.01 + 1e-9


def test_daily_loss_blocks(tmp_path):
    rm = RiskManager(RiskLimits(), KillSwitch(tmp_path / "KILL"))
    assert not rm.check(open_intent(), pf(equity=9_650, day=10_000), snap_at()).approved


def test_position_and_order_caps(tmp_path):
    rm = RiskManager(RiskLimits(), KillSwitch(tmp_path / "KILL"))
    v = rm.check(open_intent(n=50_000), pf(), snap_at())
    assert v.approved and v.intent.notional_usd == 1_000  # max order
    v = rm.check(open_intent(n=900), pf(qty=19.0), snap_at())  # ~1900 already, cap 2000
    assert v.approved and v.intent.notional_usd <= 2_000 - 19 * 100.01 + 1e-6


def test_reduce_only_cannot_flip(tmp_path):
    rm = RiskManager(RiskLimits(), KillSwitch(tmp_path / "KILL"))
    assert not rm.check(Intent(SYM, "flatten", "buy", 100, True, "t"), pf(qty=1.0), snap_at()).approved


def test_stale_and_wide_spread(tmp_path):
    rm = RiskManager(RiskLimits(), KillSwitch(tmp_path / "KILL"))
    stale = StateEngine(SYM).update(book(1_000), POS0, 10_000)
    assert "stale" in rm.check(open_intent(), pf(), stale).reason
    wide = snap_at(bid=100, ask=100.5)
    assert "spread" in rm.check(open_intent(), pf(), wide).reason


def test_manual_kill_file(tmp_path):
    (tmp_path / "KILL").write_text("operator")
    rm = RiskManager(RiskLimits(), KillSwitch(tmp_path / "KILL"))
    assert not rm.check(open_intent(), pf(), snap_at()).approved


# ---------- portfolio / review / e2e ----------

def test_portfolio_accounting():
    p = Portfolio(cash=10_000, peak_equity=10_000, day_start_equity=10_000)
    s = snap_at()
    f = PaperBroker(P).execute(Intent(SYM, "open", "buy", 1_000, False, "t"), s)
    p.apply(f)
    p.mark(SYM, s.mid, s.asof_ms)
    assert p.equity() < 10_000  # paid spread + slippage + fee


def test_brier():
    assert brier([(1.0, 1), (0.0, 0)]) == 0
    assert brier([(0.5, 1), (0.5, 0)]) == 0.25


def test_end_to_end_replay(tmp_path):
    s = Settings(symbols=(SYM,), data_dir=str(tmp_path), kill_switch_path=str(tmp_path / "KILL"))
    agent = Agent(s, StubJevClient(), PaperBroker(s.policy), NullEscalator(), load_schema("schemas/active.json"))
    ticks, px = [], 100.0
    for i in range(80):
        px *= 1.0004  # steady uptrend
        ts = 1_700_000_000_000 + i * 5_000
        ticks.append((ts, [OrderBook(SYM, ts, ((px - 0.01, 9.0),), ((px + 0.01, 2.0),))]))
    agent.run(ReplayFeed(ticks))
    lines = (tmp_path / "decisions-2023-11-14.jsonl").read_text().splitlines()
    recs = [json.loads(line) for line in lines]
    assert recs and any(r["fill"] for r in recs)
    assert all(abs(r["snapshot"]["inventory_pct"]) <= s.risk.max_position_pct + 0.02 for r in recs)
    rep = build_report(recs)
    assert rep["n_decisions"] == len(recs) and rep["brier_direction"] is not None
