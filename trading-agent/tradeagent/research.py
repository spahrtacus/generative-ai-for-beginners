"""Research layer, data half: pull regime inputs from primary sources, stamped with source + time.

    python -m tradeagent.research regime        # writes research/regime-YYYY-MM-DD.json

Every number carries {source_url, fetched_at_utc}. If a source fails, the field
is recorded as missing - nothing is filled in or estimated silently.
The qualitative half (theses, bear cases, invalidation) lives in docs/RESEARCH.md
and is written by the BRAIN (Opus) from these observations plus primary docs.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request
from pathlib import Path

SOURCES = {
    "global": "https://api.coingecko.com/api/v3/global",
    "btc_chart": "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=200&interval=daily",
    "eth_chart": "https://api.coingecko.com/api/v3/coins/ethereum/market_chart?vs_currency=usd&days=200&interval=daily",
    "stablecoins": "https://stablecoins.llama.fi/stablecoincharts/all",
    "btc_funding": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
    "eth_funding": "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=ETHUSDT",
    "btc_oi_hist": "https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1d&limit=30",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def fetch(name: str) -> dict:
    url = SOURCES[name]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "tradeagent/0.1"}), timeout=15) as r:
            return {"ok": True, "data": json.loads(r.read()), "source_url": url, "fetched_at_utc": _now()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "source_url": url, "fetched_at_utc": _now()}


def _sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def obs(value, src: dict) -> dict:
    return {"value": value, "source_url": src["source_url"], "fetched_at_utc": src["fetched_at_utc"]}


def regime() -> dict:
    raw = {k: fetch(k) for k in SOURCES}
    out: dict = {"generated_at_utc": _now(), "observations": {}, "missing": [], "classification": None}
    o = out["observations"]

    g = raw["global"]
    if g["ok"]:
        d = g["data"]["data"]
        o["btc_dominance_pct"] = obs(d["market_cap_percentage"].get("btc"), g)
        o["eth_dominance_pct"] = obs(d["market_cap_percentage"].get("eth"), g)
        o["total_mcap_usd"] = obs(d["total_market_cap"].get("usd"), g)
    else:
        out["missing"].append("global")

    for coin in ("btc", "eth"):
        c = raw[f"{coin}_chart"]
        if c["ok"]:
            px = [p for _, p in c["data"]["prices"]]
            o[f"{coin}_price"] = obs(px[-1], c)
            o[f"{coin}_sma50"] = obs(_sma(px, 50), c)
            o[f"{coin}_sma200"] = obs(_sma(px, 200) if len(px) >= 200 else _sma(px, len(px)), c)
            o[f"{coin}_ret_30d_pct"] = obs((px[-1] / px[-31] - 1) * 100 if len(px) > 31 else None, c)
        else:
            out["missing"].append(f"{coin}_chart")

    s = raw["stablecoins"]
    if s["ok"] and s["data"]:
        series = s["data"]
        tot = lambda row: float(row.get("totalCirculatingUSD", {}).get("peggedUSD", 0))
        o["stablecoin_supply_usd"] = obs(tot(series[-1]), s)
        if len(series) > 31:
            o["stablecoin_supply_30d_change_pct"] = obs((tot(series[-1]) / tot(series[-31]) - 1) * 100, s)
    else:
        out["missing"].append("stablecoins")

    for coin in ("btc", "eth"):
        f = raw[f"{coin}_funding"]
        if f["ok"]:
            o[f"{coin}_funding_rate_8h"] = obs(float(f["data"]["lastFundingRate"]), f)
        else:
            out["missing"].append(f"{coin}_funding")

    oi = raw["btc_oi_hist"]
    if oi["ok"] and len(oi["data"]) >= 2:
        first, last = float(oi["data"][0]["sumOpenInterestValue"]), float(oi["data"][-1]["sumOpenInterestValue"])
        o["btc_oi_usd"] = obs(last, oi)
        o["btc_oi_30d_change_pct"] = obs((last / first - 1) * 100, oi)
    else:
        out["missing"].append("btc_oi_hist")

    out["classification"] = classify(o)
    return out


def classify(o: dict) -> dict:
    """Deterministic, transparent regime rule. Labelled as a heuristic, not a forecast."""
    v = lambda k: (o.get(k) or {}).get("value")
    notes = []
    btc, s50, s200 = v("btc_price"), v("btc_sma50"), v("btc_sma200")
    if None in (btc, s50, s200):
        return {"label": "unknown", "notes": ["BTC trend inputs missing"]}
    trend_up = btc > s50 > s200
    trend_dn = btc < s50 < s200
    fund = v("btc_funding_rate_8h")
    stable = v("stablecoin_supply_30d_change_pct")
    if fund is not None and fund > 0.0005:
        notes.append("funding elevated (>0.05%/8h): crowded longs")
    if stable is not None:
        notes.append(f"stablecoin supply 30d {'expanding' if stable > 0 else 'contracting'}")
    r30 = v("btc_ret_30d_pct")
    if r30 is not None and r30 < -25:
        label = "crisis"
    elif trend_up:
        label = "trending_up"
    elif trend_dn:
        label = "trending_down"
    else:
        label = "range"
    return {"label": label, "notes": notes, "method": "price vs SMA50/SMA200 + 30d drawdown; heuristic"}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "regime":
        raise SystemExit("usage: python -m tradeagent.research regime")
    res = regime()
    Path("research").mkdir(exist_ok=True)
    out = Path("research") / f"regime-{res['generated_at_utc'][:10]}.json"
    out.write_text(json.dumps(res, indent=2))
    print(out)
    print(json.dumps(res["classification"], indent=2))
    if res["missing"]:
        print("MISSING:", res["missing"])


if __name__ == "__main__":
    main()
