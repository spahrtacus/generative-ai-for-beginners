# DESK_RULES.md: single source of truth (backtest v1)

Adapted from @0xNevsky, "Grok Bot for Traders: The 6-Bot Desk" (X, 2026-09-25).
Where the article is ambiguous, the decision made here is marked **[DECIDED]**.
The code is in `desk/config.py` (numbers) and `desk/signals.py` + `desk/backtest.py` (logic).

## Market
USDT-margined perpetual futures, Binance USDT-M (data source). **Short-only.**
Universe: top N USDT perps by current 24h quote volume (default 50). **[DECIDED]**
This has survivorship bias: delisted coins are missing. See the report caveats.

## 1. Screener: trend filter (1D, UTC daily close)
Short flip on daily candles C1, C2, C3:
- C1 closes down (close < open).
- C2 closes below C1's close, and C2's high stays below C1's high.
- C3 closes below C2's close, and C3's high stays below C1's high.
- Dojis are valid for C2/C3. C2 does not have to be red.
The anchor high is C1's high. The setup starts at C3's daily close.
A bullish flip (the mirror image) is never traded. It is used as context and as the profit exit.
PRIME tag: the setup is flagged PRIME when, at the flip, the latest 4H close is above the 4H close 3 bars earlier (a 4H bounce). It is logged, not filtered. **[DECIDED]**

## 2. Cartographer: zones (4H) and confirmation (1H)
Bearish fair value gap (FVG) on 4H bars a, b, c (consecutive): a.low > c.high → zone [c.high, a.low].
Scanned across the full impulse, from C1's open to now, using closed 4H bars only. Ladder sorted top to bottom.
TESTED: any 1H high reaches zone bottom (a partial wick counts).
VOID: any 1H close above zone top.
CONFIRMED: after TESTED, a 1H close below zone bottom. **[DECIDED: your choice]**
Entry: the open of the next 1H bar (no look-ahead). Each zone is used at most once.

## 3. Risk Officer
Risk per entry: 0.5% of starting equity (non-compounding in backtest v1). **[DECIDED: your choice]**
Stop distance used for sizing (1R) = anchor high − entry.
Notional per leg is capped at 1.0× equity. If the cap binds, risk shrinks with it.
Adds: up to 2 extra legs at 0.5% risk, each from a new confirmed zone, and only if price is at or below the average entry (at breakeven or better). **[DECIDED]**
Max 10 concurrent positions across the desk. **[DECIDED]**

## 4. Gate
Backtest: every signal is assumed TAKEN. Live: a Telegram alert, and a human taps TAKEN/SKIPPED.

## 5. Exit Clerk
1. Daily close above the anchor → close at that daily close. No "one more candle".
2. Daily bullish three-candle flip → close at that daily close. **[DECIDED: the article's implied profit exit]**
3. State neutral while in profit → hold.
4. At the 72h mark after entry (checked once), unrealized PnL within ±0.25R → STALE. The backtest runs both `--stale close` and `--stale hold`. **[DECIDED]**
There is no intrabar hard stop, matching the article. Losses can therefore exceed 1R on gaps.

## Costs
Taker fee 0.05% + slippage 0.05% per side, on notional **[estimate: check your fee tier]**.
Funding comes from Binance history when downloaded: shorts receive positive funding and pay negative funding.

## 6. Auditor: go / no-go (LOCKED before the run, never tuned after)
- trades ≥ 40
- win rate ≥ 33%
- expectancy ≥ +0.4R (net of fees, slippage and funding)
- max drawdown ≤ 20% (mark-to-market on daily closes)
These are reported in-sample (first 70% of time) and out-of-sample (last 30%). It must pass both.
