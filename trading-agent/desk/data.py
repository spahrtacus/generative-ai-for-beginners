"""Download 1H candles + funding history for the top USDT-M perps (Binance) into a local cache.

    pip install ccxt
    python -m desk.data --top 50 --since 2023-01-01

Writes data/ohlcv/<SYMBOL>.csv (t,o,h,l,c,v) and data/funding/<SYMBOL>.csv (t,rate).
Public endpoints only: no API key, no account needed. Re-running resumes from the last cached bar.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import time
from pathlib import Path


def _safe(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "")


def universe(ex, top: int) -> list[str]:
    ex.load_markets()
    perps = [s for s, m in ex.markets.items()
             if m.get("swap") and m.get("linear") and m.get("quote") == "USDT" and m.get("active")]
    tickers = ex.fetch_tickers(perps)
    ranked = sorted(perps, key=lambda s: float(tickers.get(s, {}).get("quoteVolume") or 0), reverse=True)
    return ranked[:top]


def _last_t(path: Path) -> int | None:
    if not path.exists():
        return None
    last = None
    with open(path) as f:
        for r in csv.reader(f):
            if r and r[0].isdigit():
                last = int(r[0])
    return last


def fetch_ohlcv(ex, symbol: str, since_ms: int, path: Path) -> int:
    last = _last_t(path)
    since = (last + 3_600_000) if last else since_ms
    n = 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        while True:
            rows = ex.fetch_ohlcv(symbol, "1h", since=since, limit=1500)
            rows = [r for r in rows if r[0] + 3_600_000 <= ex.milliseconds()]  # closed bars only
            if not rows:
                break
            w.writerows(rows)
            n += len(rows)
            since = rows[-1][0] + 3_600_000
            if len(rows) < 1000:
                break
    return n


def fetch_funding(ex, symbol: str, since_ms: int, path: Path) -> int:
    last = _last_t(path)
    since = (last + 1) if last else since_ms
    n = 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        while True:
            rows = ex.fetch_funding_rate_history(symbol, since=since, limit=1000)
            if not rows:
                break
            w.writerows([(r["timestamp"], r["fundingRate"]) for r in rows])
            n += len(rows)
            since = rows[-1]["timestamp"] + 1
            if len(rows) < 1000:
                break
    return n


def main() -> None:
    import ccxt

    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--since", default="2023-01-01")
    ap.add_argument("--symbols", default="", help="comma list like BTC/USDT:USDT; overrides --top")
    ap.add_argument("--data-dir", default="data")
    a = ap.parse_args()
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    since = int(dt.datetime.fromisoformat(a.since).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    syms = [s for s in a.symbols.split(",") if s] or universe(ex, a.top)
    (Path(a.data_dir) / "ohlcv").mkdir(parents=True, exist_ok=True)
    (Path(a.data_dir) / "funding").mkdir(parents=True, exist_ok=True)
    for s in syms:
        name = _safe(s)
        try:
            k = fetch_ohlcv(ex, s, since, Path(a.data_dir, "ohlcv", f"{name}.csv"))
            fz = fetch_funding(ex, s, since, Path(a.data_dir, "funding", f"{name}.csv"))
            print(f"{name}: +{k} bars, +{fz} funding")
        except Exception as e:
            print(f"{name}: FAILED {type(e).__name__}: {e}")
        time.sleep(0.2)


if __name__ == "__main__":
    main()
