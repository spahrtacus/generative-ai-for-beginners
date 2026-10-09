# Research layer: regime + finalists

## Status (2026-10-09): no live research yet. Nothing here is a recommendation.
The build environment's network policy blocked every market-data host (CoinGecko, DefiLlama, Binance).
I won't fill in metrics from memory, so **no finalists are named yet**.
`schemas/active.json` ships with BTC/USDT and ETH/USDT marked as **BASELINE**. They're there to test the pipeline and measure Jev's calibration, not because they're asymmetric setups.

Run on a machine with internet:
```bash
python -m tradeagent.research regime     # -> research/regime-YYYY-MM-DD.json (every value has source_url + fetched_at_utc)
```
Then have Opus (in Claude Code) fill in the sections below from that file and from primary sources.

## 1. Regime inputs (automated, `tradeagent/research.py`)
| Input | Primary source | Field |
|---|---|---|
| BTC/ETH trend (price vs SMA50/SMA200, 30d return) | CoinGecko `/coins/{id}/market_chart` | `btc_*`, `eth_*` |
| BTC/ETH dominance, total mcap | CoinGecko `/global` | `btc_dominance_pct` |
| Stablecoin liquidity (supply, 30d change) | DefiLlama `stablecoincharts/all` | `stablecoin_supply_*` |
| Funding | Binance `fapi/v1/premiumIndex` | `*_funding_rate_8h` |
| Open interest (30d) | Binance `futures/data/openInterestHist` | `btc_oi_*` |
| Macro (rates, DXY, CPI dates) | **manual**: FRED, Fed calendar | add to notes |
| Narrative rotation | **manual**: sector performance, DefiLlama category TVL | add to notes |

## 2. Finalist template (5–10 of these, one per token)
Copy this block per candidate. Every number needs a source URL and a date. Label each claim **[confirmed]**, **[estimate]** or **[speculation]**.

```markdown
### TOKEN (exchange symbol)
**Thesis (1–2 sentences):** where valuation and fundamentals disagree, and why.
**Data (source, date):**
- Supply: circulating / total / FDV ratio — (tokenomics doc / token.unlocks.app, date)
- Unlocks next 90d: amount, % of circulating — (source, date)
- Revenue / fees, 30d and 90d trend — (DefiLlama fees/revenue or Token Terminal, date)
- TVL trend — (DefiLlama, date)
- Active users / addresses trend — (Artemis / Dune query link, date)
- Holder concentration: top-10 non-exchange wallets % — (explorer, date)
**Value accrual:** does fee revenue reach token holders (buyback/burn/staking yield), or does it go only to the protocol/treasury? [confirmed/no]
**Catalysts:** [confirmed] dated, on-chain or officially announced · [speculation] rumours, "soon"
**Bear case (argue it hard):** unlock overhang, incentive-driven usage, competitor, regulatory, team/treasury selling, already priced in.
**Invalidation:** a concrete, observable condition, e.g. "30d fees drop >40%" or "unlock of X sold to exchanges".
**Organic or incentive-driven?** Share of activity explained by emissions/points.
**Liquidity check:** 2% depth on the target venue ≥ 20× intended max position? If not, drop it.
**Jev context line (goes into schemas/active.json):** thesis + invalidation, ≤ 60 words.
```

## 3. Compiling finalists into the Jev schema
For each approved finalist, add `{symbol, thesis, invalidation, status:"active"}` to `finalists` in a **candidate** schema (`schemas/candidates/vX.json`).
Promote it with `python -m tradeagent.review.nightly promote --candidate ... --i-approve`.
The typed fields are the same for every symbol, so all finalists are scored in **one parallel Jev call** per tick.
