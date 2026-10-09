"""Overnight review: read every decision/fill/miss, score calibration, propose a new schema.

    python -m tradeagent.review.nightly report  --day 2026-10-09
    python -m tradeagent.review.nightly propose --day 2026-10-09      # needs ANTHROPIC_API_KEY
    python -m tradeagent.review.nightly evaluate --candidate schemas/candidates/v2.json --day 2026-10-09
    python -m tradeagent.review.nightly promote --candidate schemas/candidates/v2.json --i-approve

Shipping a new schema is gated: it must validate, it must not have a worse Brier
score than the active schema on the same logged snapshots, and a human must pass
--i-approve. Thresholds/limits in config.py are NEVER rewritten by this loop.
"""
from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path

from ..jev_schema import load_schema, validate_schema_doc

HORIZON_TICKS = 12  # forward window used to label direction outcomes


def load_records(log_dir: str, day: str) -> list[dict]:
    p = Path(log_dir) / f"decisions-{day}.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def label_outcomes(records: list[dict], horizon: int = HORIZON_TICKS, cost_bps: float = 16.0) -> list[dict]:
    """Attach y=1 if price moved in the predicted direction by more than costs within `horizon` ticks.

    Uses only *later* records for the label (that's fine offline; it's never fed live).
    """
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_sym[r["snapshot"]["symbol"]].append(r)
    out = []
    for rows in by_sym.values():
        for i, r in enumerate(rows):
            j = r.get("jev")
            if not j or j["direction"]["value"] == "neutral" or i + horizon >= len(rows):
                continue
            m0, m1 = r["snapshot"]["mid"], rows[i + horizon]["snapshot"]["mid"]
            move_bps = (m1 - m0) / m0 * 1e4
            sign = 1 if j["direction"]["value"] == "long" else -1
            out.append({**r, "y": 1 if sign * move_bps > cost_bps else 0, "move_bps": move_bps})
    return out


def brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def calibration_table(pairs: list[tuple[float, int]], bins: int = 5) -> list[dict]:
    rows = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, y) for p, y in pairs if lo <= p < hi or (b == bins - 1 and p == 1.0)]
        if sel:
            rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                         "mean_p": sum(p for p, _ in sel) / len(sel), "hit_rate": sum(y for _, y in sel) / len(sel)})
    return rows


def build_report(records: list[dict]) -> dict:
    labeled = label_outcomes(records)
    pairs = [(r["jev"]["direction"]["confidence"], r["y"]) for r in labeled]
    traded = [r for r in labeled if r.get("fill")]
    missed = [r for r in labeled if not r.get("fill") and r["jev"]["direction"]["confidence"] > 0.8]
    fills = [r["fill"] for r in records if r.get("fill")]
    reasons: dict[str, int] = defaultdict(int)
    for r in records:
        reasons[r["risk"]["reason"] if r["intent"]["action"] != "hold" else r["intent"]["reason"]] += 1
    return {
        "n_decisions": len(records),
        "n_labeled": len(labeled),
        "brier_direction": brier(pairs),
        "brier_baseline_0.5": brier([(0.5, y) for _, y in pairs]),
        "calibration": calibration_table(pairs),
        "n_fills": len(fills),
        "fees_usd": sum(f["fee_usd"] for f in fills),
        "traded_hit_rate": sum(r["y"] for r in traded) / len(traded) if traded else None,
        "missed_high_conf": len(missed),
        "missed_high_conf_hit_rate": sum(r["y"] for r in missed) / len(missed) if missed else None,
        "equity_start": records[0]["equity"] if records else None,
        "equity_end": records[-1]["equity"] if records else None,
        "kill_tripped": any(r.get("kill") for r in records),
        "jev_null_rate": sum(1 for r in records if r.get("jev") is None) / len(records) if records else None,
        "top_reasons": sorted(reasons.items(), key=lambda kv: -kv[1])[:10],
    }


def render_markdown(day: str, rep: dict) -> str:
    lines = [f"# Nightly review {day}", "", "| metric | value |", "|---|---|"]
    for k, v in rep.items():
        if k not in ("calibration", "top_reasons"):
            lines.append(f"| {k} | {v if not isinstance(v, float) else round(v, 5)} |")
    lines += ["", "## Calibration (direction)", "", "| bin | n | mean p | hit rate |", "|---|---|---|---|"]
    lines += [f"| {c['bin']} | {c['n']} | {c['mean_p']:.3f} | {c['hit_rate']:.3f} |" for c in rep["calibration"]]
    lines += ["", "## Top hold/reject reasons", ""] + [f"- {n}× {r}" for r, n in rep["top_reasons"]]
    lines += ["", "Brier < baseline means Jev's direction probabilities beat a coin flip on this sample. "
              "Small samples are noise; do not promote on < 200 labeled decisions."]
    return "\n".join(lines) + "\n"


def propose(active: dict, rep: dict, model: str = "claude-opus-5-5") -> dict:
    """Ask Opus to rewrite the schema's *instructions* and field descriptions only."""
    import anthropic

    client = anthropic.Anthropic()
    schema_out = {"type": "object", "properties": {"instructions": {"type": "string"},
                  "field_descriptions": {"type": "object", "additionalProperties": {"type": "string"}},
                  "changelog": {"type": "string"}},
                  "required": ["instructions", "field_descriptions", "changelog"], "additionalProperties": False}
    resp = client.beta.messages.create(
        model=model, max_tokens=16000, betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema_out}},
        system=("You improve the instructions of a typed decision schema used by a fast classifier in a "
                "crypto trading loop. You may only rewrite wording to improve calibration. You may not "
                "add or remove fields, change allowed values, or mention thresholds or sizes."),
        messages=[{"role": "user", "content": json.dumps({"active_schema": active, "nightly_report": rep})}],
    )
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"proposal stopped: {resp.stop_reason}")
    data = json.loads(next(b.text for b in resp.content if b.type == "text"))
    cand = json.loads(json.dumps(active))
    major, minor = (cand["version"].lstrip("v").split(".") + ["0"])[:2]
    cand["version"] = f"v{major}.{int(minor) + 1}"
    cand["instructions"] = data["instructions"]
    for name, desc in data["field_descriptions"].items():
        if name in cand["fields"]:
            cand["fields"][name]["description"] = desc
    cand["changelog"] = data["changelog"]
    validate_schema_doc(cand)
    return cand


def evaluate(candidate_path: str, active_path: str, records: list[dict]) -> dict:
    """Re-score logged snapshots with both schemas via Jev and compare Brier."""
    import os

    from ..jev_client import HttpJevClient
    from ..state_engine import Snapshot

    client = HttpJevClient(os.environ["JEV_API_URL"], os.environ["JEV_API_KEY"], timeout_s=2.0)
    labeled = label_outcomes(records)
    result = {}
    for name, path in (("active", active_path), ("candidate", candidate_path)):
        schema = load_schema(path)
        pairs = []
        for r in labeled:
            snap = Snapshot(**r["snapshot"])
            d = client.decide(schema, [snap]).get(snap.symbol)
            if d and d.direction.value != "neutral":
                want = r["jev"]["direction"]["value"]
                p = d.direction.confidence if d.direction.value == want else 1 - d.direction.confidence
                pairs.append((p, r["y"]))
        result[name] = {"brier": brier(pairs), "n": len(pairs)}
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["report", "propose", "evaluate", "promote"])
    ap.add_argument("--day")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--active", default="schemas/active.json")
    ap.add_argument("--candidate")
    ap.add_argument("--i-approve", action="store_true")
    a = ap.parse_args()

    if a.cmd == "report":
        rep = build_report(load_records(a.log_dir, a.day))
        out = Path(a.log_dir) / f"review-{a.day}.md"
        out.write_text(render_markdown(a.day, rep))
        (Path(a.log_dir) / f"review-{a.day}.json").write_text(json.dumps(rep, indent=2))
        print(out)
    elif a.cmd == "propose":
        rep = json.loads((Path(a.log_dir) / f"review-{a.day}.json").read_text())
        cand = propose(load_schema(a.active), rep)
        out = Path("schemas/candidates") / f"{cand['version']}.json"
        out.write_text(json.dumps(cand, indent=2))
        print(out)
    elif a.cmd == "evaluate":
        res = evaluate(a.candidate, a.active, load_records(a.log_dir, a.day))
        Path(a.candidate).with_suffix(".eval.json").write_text(json.dumps(res, indent=2))
        print(json.dumps(res, indent=2))
    elif a.cmd == "promote":
        cand = load_schema(a.candidate)
        ev_path = Path(a.candidate).with_suffix(".eval.json")
        if not ev_path.exists():
            raise SystemExit("run `evaluate` first")
        ev = json.loads(ev_path.read_text())
        c, b = ev["candidate"]["brier"], ev["active"]["brier"]
        if c is None or b is None or ev["candidate"]["n"] < 200:
            raise SystemExit("not enough labeled decisions (need >= 200) to promote")
        if c > b:
            raise SystemExit(f"candidate Brier {c:.4f} worse than active {b:.4f}; not promoting")
        if not a.i_approve:
            raise SystemExit(f"candidate Brier {c:.4f} vs active {b:.4f}. Re-run with --i-approve to ship.")
        shutil.copy(a.active, Path("schemas") / f"archive-{load_schema(a.active)['version']}.json")
        shutil.copy(a.candidate, a.active)
        print(f"promoted {cand['version']} -> {a.active}. Restart the loop to load it.")


if __name__ == "__main__":
    main()
