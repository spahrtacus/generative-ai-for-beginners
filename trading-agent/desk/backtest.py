"""Event-driven backtest of the 6-bot desk on 1H bars (4H/1D are resampled from 1H).

    python -m desk.backtest                      # both STALE variants, all cached symbols
    python -m desk.backtest --stale hold --symbols BTCUSDT,ETHUSDT

Causality: at each 1H close we only use 4H/1D bars whose close time <= now.
Entries fill at the NEXT 1H open. Daily-close exits fill at the daily close.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from bisect import bisect_right
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .config import D1, H1, H4, DeskRules, Gates
from .signals import Bar, Zone, bearish_fvgs, flips, resample


@dataclass
class Leg:
    t: int
    price: float
    notional: float      # fraction of starting equity
    risk: float          # fraction of starting equity at 1R


@dataclass
class Trade:
    symbol: str
    anchor: float
    prime: bool
    open_t: int
    legs: list[Leg] = field(default_factory=list)
    exit_t: int | None = None
    exit_price: float | None = None
    exit_reason: str = ""
    stale_flag: bool = False
    stale_checked: bool = False
    pnl: float = 0.0     # fraction of starting equity, net
    r: float = 0.0       # pnl / first-leg risk
    fees: float = 0.0
    funding: float = 0.0
    marks: list[tuple[int, float]] = field(default_factory=list)  # (daily close t, open pnl)

    def avg_entry(self) -> float:
        n = sum(l.notional for l in self.legs)
        return sum(l.price * l.notional for l in self.legs) / n

    def gross_pnl_at(self, price: float) -> float:
        return sum(l.notional * (l.price - price) / l.price for l in self.legs)


def _funding_between(funding: list[tuple[int, float]], t0: int, t1: int) -> float:
    if not funding:
        return 0.0
    ts = [f[0] for f in funding]
    i, j = bisect_right(ts, t0), bisect_right(ts, t1)
    return sum(f[1] for f in funding[i:j])


def _close(tr: Trade, t: int, price: float, reason: str, rules: DeskRules, funding) -> Trade:
    gross = tr.gross_pnl_at(price)
    side_cost = rules.fee_rate + rules.slippage_rate
    fees = sum(l.notional * side_cost for l in tr.legs) * 2
    # shorts receive positive funding, pay negative
    fund = sum(l.notional * _funding_between(funding, l.t, t) for l in tr.legs)
    tr.exit_t, tr.exit_price, tr.exit_reason = t, price, reason
    tr.fees, tr.funding = fees, fund
    tr.pnl = gross - fees + fund
    tr.r = tr.pnl / tr.legs[0].risk
    return tr


def _leg(t: int, price: float, anchor: float, rules: DeskRules) -> Leg | None:
    dist = (anchor - price) / price
    if dist <= 0:
        return None
    notional = min(rules.risk_pct / dist, rules.max_notional_per_leg)
    return Leg(t, price, notional, notional * dist)


def simulate_symbol(symbol: str, h1: list[Bar], rules: DeskRules,
                    funding: list[tuple[int, float]] | None = None) -> list[Trade]:
    funding = funding or []
    d1, h4 = resample(h1, D1), resample(h1, H4)
    flip_at = flips(d1)
    trades: list[Trade] = []
    setup: dict | None = None
    zones: dict[int, Zone] = {}
    pos: Trade | None = None
    pending = False
    nd = n4 = 0  # completed daily / 4H bars as of now

    for j, bar in enumerate(h1):
        # 1. fill pending entry at this bar's open
        if pending and setup:
            leg = _leg(bar[0], bar[1], setup["anchor"], rules)
            if leg:
                if pos is None:
                    pos = Trade(symbol, setup["anchor"], setup["prime"], bar[0], [leg])
                elif len(pos.legs) <= rules.max_adds and bar[1] <= pos.avg_entry():
                    pos.legs.append(leg)
        pending = False

        now = bar[0] + H1
        # 2. 1H zone logic (zones formed strictly before this bar opened)
        if setup:
            for z in zones.values():
                if z.state != "open" or z.formed_close_t > bar[0]:
                    continue
                if bar[4] > z.top:
                    z.state = "void"
                    continue
                if bar[2] >= z.bottom:
                    z.tested = True
                if z.tested and bar[4] < z.bottom and j + 1 < len(h1):
                    z.state = "used"
                    pending = True

        # 3. stale check
        if pos and not pos.stale_checked and now - pos.open_t >= rules.stale_hours * H1:
            pos.stale_checked = True   # judged once, at the 72h mark
            r_now = pos.gross_pnl_at(bar[4]) / pos.legs[0].risk
            if abs(r_now) <= rules.stale_band_r:
                pos.stale_flag = True
                if rules.stale_policy == "close":
                    trades.append(_close(pos, now, bar[4], "stale", rules, funding))
                    pos, pending = None, False

        # 4. newly completed 4H bars -> refresh zone ladder
        while n4 < len(h4) and h4[n4][0] + H4 <= now:
            n4 += 1
            if setup and n4 >= 3 and h4[n4 - 3][0] >= setup["start_t"]:
                a, c = h4[n4 - 3], h4[n4 - 1]   # only the newly completed triple
                if a[3] > c[2]:
                    zones.setdefault(c[0] + H4, Zone(bottom=c[2], top=a[3], formed_close_t=c[0] + H4))

        # 5. newly completed daily bars -> exits, invalidation, new flips
        while nd < len(d1) and d1[nd][0] + D1 <= now:
            i, db = nd, d1[nd]
            nd += 1
            f = flip_at.get(i)
            if pos:
                if db[4] > pos.anchor:
                    trades.append(_close(pos, now, db[4], "anchor_close", rules, funding))
                    pos, pending = None, False
                elif f and f.side == "long":
                    trades.append(_close(pos, now, db[4], "bullish_flip", rules, funding))
                    pos, pending = None, False
                else:
                    pos.marks.append((now, pos.gross_pnl_at(db[4])))
            if setup and (db[4] > setup["anchor"] or (f and f.side == "long")):
                setup, zones, pending = None, {}, False
            if f and f.side == "short" and pos is None:
                lb = rules.prime_lookback_4h
                prime = n4 > lb and h4[n4 - 1][4] > h4[n4 - 1 - lb][4]
                setup = {"anchor": f.anchor, "start_t": f.c1_t, "prime": prime}
                zones = {z.formed_close_t: z for z in bearish_fvgs(h4[:n4], f.c1_t, H4)}
                # zones that existed before the flip can only be traded on future bars
                for z in zones.values():
                    z.formed_close_t = max(z.formed_close_t, now)

    if pos:
        last = h1[-1]
        trades.append(_close(pos, last[0] + H1, last[4], "end_of_data", rules, funding))
    return trades


# ---------------- portfolio & metrics ----------------

def apply_concurrency(trades: list[Trade], cap: int) -> list[Trade]:
    kept: list[Trade] = []
    for tr in sorted(trades, key=lambda t: t.open_t):
        active = [k for k in kept if k.exit_t and k.exit_t > tr.open_t]
        if len(active) < cap:
            kept.append(tr)
    return kept


def equity_curve(trades: list[Trade]) -> list[tuple[int, float]]:
    times = sorted({t for tr in trades for t, _ in tr.marks} | {tr.exit_t for tr in trades})
    curve = []
    for t in times:
        realized = sum(tr.pnl for tr in trades if tr.exit_t <= t)
        opened = 0.0
        for tr in trades:
            if tr.open_t <= t < tr.exit_t:
                m = [p for mt, p in tr.marks if mt <= t]
                opened += m[-1] if m else 0.0
        curve.append((t, 1.0 + realized + opened))
    return curve


def max_drawdown(curve: list[tuple[int, float]]) -> float:
    peak, mdd = 1.0, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    return mdd


def metrics(trades: list[Trade], gates: Gates) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    exp_r = sum(t.r for t in trades) / n if n else 0.0
    mdd = max_drawdown(equity_curve(trades)) if n else 0.0
    m = {
        "trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "expectancy_r": exp_r,
        "max_drawdown": mdd,
        "total_return": sum(t.pnl for t in trades),
        "avg_win_r": sum(t.r for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_r": (sum(t.r for t in trades if t.pnl <= 0) / (n - len(wins))) if n > len(wins) else 0.0,
        "worst_r": min((t.r for t in trades), default=0.0),
        "fees": sum(t.fees for t in trades),
        "funding": sum(t.funding for t in trades),
        "prime_share": sum(t.prime for t in trades) / n if n else 0.0,
        "exit_reasons": {r: sum(1 for t in trades if t.exit_reason == r) for r in sorted({t.exit_reason for t in trades})},
    }
    m["checks"] = {
        "trades>=%d" % gates.min_trades: n >= gates.min_trades,
        "win_rate>=%.0f%%" % (gates.min_win_rate * 100): m["win_rate"] >= gates.min_win_rate,
        "expectancy>=+%.1fR" % gates.min_expectancy_r: exp_r >= gates.min_expectancy_r,
        "max_dd<=%.0f%%" % (gates.max_drawdown * 100): mdd <= gates.max_drawdown,
    }
    m["pass"] = all(m["checks"].values())
    return m


def split_is_oos(trades: list[Trade], frac: float = 0.7) -> tuple[list[Trade], list[Trade]]:
    if not trades:
        return [], []
    t0 = min(t.open_t for t in trades)
    t1 = max(t.open_t for t in trades)
    cut = t0 + (t1 - t0) * frac
    return [t for t in trades if t.open_t < cut], [t for t in trades if t.open_t >= cut]


def run(data: dict[str, tuple[list[Bar], list]], rules: DeskRules, gates: Gates) -> dict:
    allt = []
    for sym, (h1, fund) in data.items():
        allt += simulate_symbol(sym, h1, rules, fund)
    kept = apply_concurrency(allt, rules.max_concurrent)
    ins, oos = split_is_oos(kept)
    return {"rules": asdict(rules), "all": metrics(kept, gates), "in_sample": metrics(ins, gates),
            "out_of_sample": metrics(oos, gates), "skipped_by_concurrency": len(allt) - len(kept),
            "trades": kept}


# ---------------- I/O + report ----------------

def load_cache(data_dir: str, symbols: list[str] | None) -> dict[str, tuple[list[Bar], list]]:
    out = {}
    for p in sorted(Path(data_dir, "ohlcv").glob("*.csv")):
        sym = p.stem
        if symbols and sym not in symbols:
            continue
        with open(p) as f:
            h1 = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                  for r in csv.reader(f) if r and r[0].isdigit()]
        fund = []
        fp = Path(data_dir, "funding", f"{sym}.csv")
        if fp.exists():
            with open(fp) as f:
                fund = [(int(r[0]), float(r[1])) for r in csv.reader(f) if r and r[0].isdigit()]
        out[sym] = (h1, fund)
    return out


def _d(t: int) -> str:
    return dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def render(results: dict[str, dict], meta: dict) -> str:
    L = [f"# Desk backtest: {meta['generated']}", "",
         f"Symbols: {meta['n_symbols']} · Data: {meta['first']} → {meta['last']} (UTC) · Source: Binance USDT-M via ccxt",
         f"Funding data: {'yes' if meta['funding'] else 'NO (funding = 0, result optimistic/pessimistic unknown)'}", ""]
    for variant, res in results.items():
        L += [f"## STALE policy = {variant}", "", "| | all | in-sample (first 70%) | out-of-sample (last 30%) |", "|---|---|---|---|"]
        rows = [("trades", "{:d}"), ("win_rate", "{:.1%}"), ("expectancy_r", "{:+.2f}R"), ("max_drawdown", "{:.1%}"),
                ("total_return", "{:+.1%}"), ("avg_win_r", "{:+.2f}R"), ("avg_loss_r", "{:+.2f}R"), ("worst_r", "{:+.2f}R"),
                ("fees", "{:.2%}"), ("funding", "{:+.2%}"), ("prime_share", "{:.0%}")]
        for k, fmt in rows:
            L.append(f"| {k} | " + " | ".join(fmt.format(res[s][k]) for s in ("all", "in_sample", "out_of_sample")) + " |")
        L.append("| **GO / NO-GO** | " + " | ".join("**PASS**" if res[s]["pass"] else "**FAIL**" for s in ("all", "in_sample", "out_of_sample")) + " |")
        L += ["", "Checks (all): " + ", ".join(f"{k} {'✅' if v else '❌'}" for k, v in res["all"]["checks"].items()),
              f"Exit reasons: {res['all']['exit_reasons']} · skipped by concurrency cap: {res['skipped_by_concurrency']}", ""]
    L += ["## Caveats (read before believing any number)",
          "- Survivorship bias: universe = today's top perps; coins that died are missing (likely flatters a short-only desk *less* than a long one, but unknown).",
          "- Non-compounding: risk = 0.5% of *starting* equity. Drawdown is mark-to-market on daily closes only (intraday DD is worse).",
          "- No intrabar stop (per the article): `worst_r` shows how far beyond 1R a gap can take you.",
          "- Fees/slippage are assumptions (0.05% + 0.05% per side). Liquidity of small coins is not modelled.",
          "- Profit exit (bullish 3-candle flip) and STALE handling are decisions, not the article's words: see DESK_RULES.md.",
          "- Thresholds were locked before this run. Do not tune rules to make this table pass; that converts a test into a curve fit."]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--stale", choices=["close", "hold", "both"], default="both")
    ap.add_argument("--out", default="desk_reports")
    a = ap.parse_args()
    data = load_cache(a.data_dir, [s for s in a.symbols.split(",") if s] or None)
    if not data:
        raise SystemExit(f"no data in {a.data_dir}/ohlcv: run `python -m desk.data` first")
    variants = ["close", "hold"] if a.stale == "both" else [a.stale]
    results = {v: run(data, replace(DeskRules(), stale_policy=v), Gates()) for v in variants}
    all_t = [b[0] for h1, _ in data.values() for b in (h1[0], h1[-1])]
    meta = {"generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "n_symbols": len(data),
            "first": _d(min(all_t)), "last": _d(max(all_t)), "funding": any(f for _, f in data.values())}
    out = Path(a.out)
    out.mkdir(exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M")
    (out / f"backtest-{stamp}.md").write_text(render(results, meta))
    for v, res in results.items():
        with open(out / f"trades-{v}-{stamp}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "open", "exit", "reason", "legs", "avg_entry", "exit_price", "anchor", "prime", "stale", "r", "pnl", "fees", "funding"])
            for t in res["trades"]:
                w.writerow([t.symbol, _d(t.open_t), _d(t.exit_t), t.exit_reason, len(t.legs), f"{t.avg_entry():.6g}",
                            f"{t.exit_price:.6g}", f"{t.anchor:.6g}", t.prime, t.stale_flag, f"{t.r:.3f}", f"{t.pnl:.5f}",
                            f"{t.fees:.5f}", f"{t.funding:.5f}"])
        summary = {k: {kk: vv for kk, vv in res[k].items()} for k in ("all", "in_sample", "out_of_sample")}
        (out / f"summary-{v}-{stamp}.json").write_text(json.dumps(summary, indent=2))
    print(out / f"backtest-{stamp}.md")
    for v, res in results.items():
        print(f"stale={v}: {'PASS' if res['all']['pass'] else 'FAIL'}  "
              f"trades={res['all']['trades']} win={res['all']['win_rate']:.1%} "
              f"exp={res['all']['expectancy_r']:+.2f}R dd={res['all']['max_drawdown']:.1%}")


if __name__ == "__main__":
    main()
