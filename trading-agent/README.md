# tradeagent: a 24/7 crypto agent with a fast reflex, a slow brain, and hard limits in code

**Paper mode by default.** Live trading needs an explicit env phrase, and testnet comes first. This is software, not financial advice. A pipeline that runs is not an edge, and none has been shown yet.

## Architecture
| Layer | Who | Job | Never does |
|---|---|---|---|
| BRAIN | Claude Opus 5.5 | research, schema authoring, escalation re-reads, nightly review | touch limits, open positions |
| REFLEX | Jev (TypeSafe AI) | score every symbol each tick into typed, calibrated fields | size, place orders, see balances |
| CODE | this repo | state math, gate, ¼-Kelly sizing, hard risk, execution, logs | defer a limit to a model |

Per tick: `exchange book → state_engine (causal, <400 tok) → Jev (1 parallel call) → policy (gate + Kelly) → risk (hard limits + kill switch) → broker → JSONL log`.

**Jev schema** (`schemas/active.json`): `regime ∈ {trending, mean_reverting, high_vol, crisis}`, `direction ∈ {long, short, neutral}`, `toxic_flow: bool`, `setup_quality ∈ {0..3}`, `risk_state ∈ {safe, near_limit, reduce}`. Each field carries a confidence.

**Gate** (`tradeagent/policy.py`): open only if setup_quality ≥ 2 **and** direction confidence > 0.80 **and** risk_state = safe **and** not toxic **and** not crisis. Size = ¼ × Kelly(p, TP/SL net of round-trip costs).

**Escalation**: if any gating confidence is < 0.60 or regime = crisis, Opus gets the case in a background thread. New entries on that symbol are blocked for 15 min. Opus can answer only `hold | reduce | flatten | resume`.

**Risk** (`tradeagent/risk.py`, no model input at all): 15% max drawdown arms the kill switch, 3% daily loss → reduce-only for the rest of the day, 20% per-symbol cap, 50% gross cap, $1k max order, spread/staleness guards, 30 orders/hour. The kill switch is the file `logs/KILL`, so `touch` arms it and only a human deleting it disarms it.

## Quickstart
```bash
cd trading-agent
pip install -r requirements.txt
python -m pytest -q                                   # 30 tests
cp .env.example .env                                  # add JEV_API_URL / JEV_API_KEY
python -m tradeagent.loop --stub                      # paper, live public books, stub reflex (pipeline check)
set -a; . ./.env; set +a; python -m tradeagent.loop   # paper, real Jev
./ops/nightly.sh                                      # review + regime + candidate schema
```
**Jev wire format** (confirmed from the console playground, `jev-1.13.0`): `{model, state, questions}` in, `answers.<name>.noul` (P(true)) out. Each typed field is compiled into yes/no **noul** questions (11 per symbol) and decoded back in code. The rules are in `decode_fields()` in `tradeagent/jev_client.py`. **Still needed:** the endpoint URL and auth header. Click `</>` in the playground and set `JEV_API_URL` (plus `JEV_AUTH_HEADER`/`JEV_AUTH_PREFIX` if it isn't `Authorization: Bearer`).

**Cost estimate [estimate]:** about 1–1.5k input tokens per call × 2 symbols every 5 s ≈ 35k calls/day. At the reported $0.042/M input tokens that's roughly $1.50–2/day. Check the console **Usage** page after the first hour and raise `TRADEAGENT_POLL_SECONDS` if needed.

## Files
`tradeagent/config.py` limits · `state_engine.py` snapshot · `jev_schema.py` types/validation · `jev_client.py` Jev + stub · `policy.py` gate/Kelly · `risk.py` hard limits · `broker.py` paper/ccxt · `escalation.py` Opus · `loop.py` 24/7 loop · `review/nightly.py` Brier/calibration/propose/promote · `research.py` regime data · `docs/PLAN.md` six phases · `docs/RESEARCH.md` finalist template · `ops/` systemd + cron.

## Overnight loop
`report` (fills, misses, Brier vs 0.5 baseline, calibration bins, hold reasons) → `propose` (Opus rewrites **wording only**: no fields, values, thresholds or sizes) → `evaluate` (re-scores logged snapshots with both schemas on Jev) → `promote` (needs ≥ 200 labeled decisions, candidate Brier ≤ active, **and** `--i-approve`). I changed one thing from the brief here: the loop doesn't ship automatically. It hands you a ready candidate before the next open, and you approve it.

---

## Final check
- **Organic or incentive-driven edge?** Unknown. The baseline symbols carry no fundamental thesis, and the finalist template forces an "organic vs. emissions" line per token.
- **Catalyst priced in?** Not assessed: there are no finalists yet (see `docs/RESEARCH.md`; the market data APIs were blocked from the build sandbox).
- **Value accrual?** Required field per finalist; blank until research is run.
- **Survives costs?** Kelly is computed **net of 16 bps round-trip**. Paper fills pay half-spread + slippage + fee. Labels in the nightly review only count a move as a win if it beats costs.
- **Any hard limit delegated to a model?** No. `risk.py` takes no model input. Opus/Jev outputs can only reduce risk or be gated out. Limits are frozen dataclasses.

## WHAT COULD I BE WRONG ABOUT?
1. **Jev's "direction confidence" is not P(TP before SL).** Kelly needs the probability of the payoff it sizes for. Jev reports P(label is correct), which is a different number. Until the nightly calibration shows the two line up, treat sizing as mis-specified. That's why it's capped at ¼ Kelly and $1k per order.
2. **Microstructure on 5-second REST polls is a weak signal.** Order-book imbalance edges decay in milliseconds and belong to colocated market makers. Polled public books are slow and get adversely selected. An ~81 ms model doesn't fix ~5,000 ms data. The real edge, if any, more likely sits at the slower thesis/regime horizon.
3. **Calibration claims are vendor-reported.** TypeSafe's speed and "can't hallucinate" claims haven't been independently reproduced ([DataCamp](https://www.datacamp.com/ja/blog/system-one-models-jev), [TrueFoundry](https://www.truefoundry.com/blog/typesafe-ai-jev)). Typed output means the values are always valid, not that they're correct.
4. **Small samples lie.** A good Brier score over a few hundred correlated ticks says almost nothing. Overlapping 12-tick labels are autocorrelated. Before trusting it, use weeks of data and walk-forward splits.
5. **The nightly loop can overfit.** Rewriting instructions every night against yesterday's tape is a form of curve-fitting. The Brier-must-not-worsen gate checks in-sample data only. Add a held-out week before any promotion counts.
6. **Paper ≠ live.** Queue position, partial fills, rate limits, exchange outages, spot vs. perp `reduceOnly` semantics, and funding costs on perps aren't modelled. The 15% drawdown limit is checked per tick, so a gap can blow through it.
7. **Single process, single host.** If the box dies with an open position, nothing manages it. Use exchange-side stop orders as a second line of defense before going live.
8. **AgenKit unverified.** I couldn't confirm agenkit.xyz exists or what it does. `docs/PLAN.md` gives you the six gated phases regardless of tooling.

**Rule: a system that survives beats one that looks profitable.** Run paper for weeks and testnet for one, then go live small. If the calibration table doesn't line up, the answer is "don't trade", not "tune harder".
