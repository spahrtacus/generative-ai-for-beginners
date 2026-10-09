# Build plan: six phases, each with an approval gate

Written to be driven by any structured agentic harness (AgenKit or plain Claude Code).
I could not verify agenkit.xyz from this environment, so these phases are written down as files in this repo and don't depend on any one tool.
Each phase ends with a **GATE**. The operator approves it, or the work does not move on.

| # | Phase | Output (exact paths) | Gate: operator approves when… |
|---|---|---|---|
| 1 | **Spec** | `docs/PLAN.md` (this file), `README.md` § Mission, `tradeagent/config.py` (every limit, as code) | Limits in `config.py` match the risk appetite: 15% max DD, 3% daily loss, 20% per-symbol, 50% gross, $1k max order |
| 2 | **Architecture** | `README.md` § Architecture, the module boundaries below | BRAIN/REFLEX split holds: no model output reaches `risk.py`, no limit lives outside `config.py` |
| 3 | **Plan** | `docs/RESEARCH.md` (finalists + bear cases), `schemas/active.json` | Each finalist has a thesis, a confirmed/speculative catalyst split, a bear case and an invalidation condition |
| 4 | **Test-first build** | `tests/test_core.py` → `tradeagent/state_engine.py`, `jev_schema.py`, `jev_client.py`, `policy.py`, `risk.py`, `broker.py`, `escalation.py`, `loop.py`, `review/nightly.py`, `research.py` | `pytest -q` is green, and causality, gate, Kelly, every risk limit, the kill switch and e2e replay are all covered |
| 5 | **Review** | `docs/phases/05-review.md` (filled from the checklist below), 14+ days of paper logs in `logs/`, nightly `logs/review-*.md` | Paper Brier score beats the 0.5 baseline on ≥ 200 labeled decisions, net-of-cost PnL ≥ 0, zero risk-layer bypasses |
| 6 | **Ship** | `ops/tradeagent.service`, `ops/nightly.sh`, testnet run, then live with reduced limits | Testnet fills reconcile with the logs for 7 days, then live at ≤ 10% of intended capital |

## Module boundaries (phase 2)

```
            ┌──────────── BRAIN (Opus 5.5, slow) ─────────────┐
 research.py ─► docs/RESEARCH.md ─► schemas/active.json        │
 review/nightly.py ─► schemas/candidates/vN.json ─(gate)─► active
 escalation.py  (hold | reduce | flatten | resume only)        │
            └─────────────────────────────────────────────────┘
                              │ schema (text only)
 exchange ─► state_engine.py ─► Snapshot.render() (<400 tok) ─► jev_client.py (REFLEX)
                                                                   │ typed fields + confidences
                                       policy.py (gate + ¼ Kelly) ◄┘
                                            │ Intent
                                       risk.py (hard limits, kill switch; NO model input)
                                            │ Verdict
                                       broker.py ─► fills ─► logs/decisions-*.jsonl
```

## Phase 5 review checklist
- [ ] `grep -rn "jev\|decision" tradeagent/risk.py` returns nothing (risk takes no model input)
- [ ] Every limit lives in `config.py`, and the dataclasses are frozen
- [ ] Kill switch tested by hand: `touch logs/KILL` → only reduce-only orders appear in the logs
- [ ] Jev timeout/garbage → `jev: null` rows → hold (check `jev_null_rate` in the nightly report)
- [ ] Paper fills priced at touch + half-spread + slippage + fee (pessimistic)
- [ ] Calibration table: mean p within ±0.05 of hit rate in each populated bin
- [ ] No promotion of a schema with fewer than 200 labeled decisions

## Phase 6 ship steps
1. `EXCHANGE_TESTNET=1 TRADEAGENT_MODE=live TRADEAGENT_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK python -m tradeagent.loop` for 7 days.
2. Reconcile exchange fills against `logs/decisions-*.jsonl`.
3. Lower `RiskLimits` (e.g. `max_order_notional_usd=100`), set `EXCHANGE_TESTNET=0`, and fund the account with only what you can lose.
4. API key: trade permission only, **no withdrawal**, IP-allowlisted.
