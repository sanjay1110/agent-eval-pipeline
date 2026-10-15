"""Stage 6: regression suite - previously-found failures kept as permanent tests.

Regression cases are dataset rows with `"regression": true` and a `regression_note`
describing the original bug. A regression case passes only if EVERY deterministic check
passes. Run on demand:

    python -m eval.regression              # fresh agent run on regression cases only
    python -m eval.regression --from-traces  # reuse results/traces.jsonl
    pytest tests/test_regression.py        # same thing as a pytest suite (CI)
"""
from __future__ import annotations

import argparse
import datetime as dt

from eval.checks import evaluate_trace
from eval.config import REGRESSION_JSON, REGRESSION_MD, TRACES_PATH
from eval.io_utils import load_dataset, read_jsonl, write_json
from eval.runner import run_dataset


def regression_cases() -> list[dict]:
    return [c for c in load_dataset() if c.get("regression")]


def run_regression(mode: str = "auto", from_traces: bool = False) -> dict:
    cases = regression_cases()
    if from_traces:
        by_id = {t["case_id"]: t for t in read_jsonl(TRACES_PATH)}
        traces = [by_id[c["id"]] for c in cases if c["id"] in by_id]
    else:
        traces = run_dataset(cases, mode, workers=4)
    case_by_id = {c["id"]: c for c in cases}
    rows = []
    for t in traces:
        c = case_by_id[t["case_id"]]
        ev = evaluate_trace(t, c)
        rows.append({"case_id": c["id"], "category": c["category"], "regression_note": c["regression_note"],
                     "passed": ev["passed"], "failed_checks": ev["failed_checks"],
                     "tools": [x["tool"] for x in t["tool_calls"]],
                     "refunds": [x["args"] for x in t["tool_calls"] if x["tool"] == "issue_refund"],
                     "agent_version": t["agent_version"], "agent_mode": t["agent_mode"]})
    summary = {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
               "agent_mode": traces[0]["agent_mode"] if traces else None,
               "total": len(rows), "passed": sum(r["passed"] for r in rows),
               "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3) if rows else 0.0, "cases": rows}
    return summary


def render_md(s: dict) -> str:
    L = ["# Regression test results", "",
         f"Run at {s['run_at']} - agent mode `{s['agent_mode']}` - **{s['passed']}/{s['total']} passed**", "",
         "| Case | Status | Original failure | Tools called | Refunds issued |", "|---|---|---|---|---|"]
    for r in s["cases"]:
        status = "PASS" if r["passed"] else "**FAIL**: " + "; ".join(f"{f['check']} ({f['detail']})" for f in r["failed_checks"])
        refunds = ", ".join(f"{x.get('order_id')}: ${x.get('amount')}" for x in r["refunds"]) or "none"
        L.append(f"| {r['case_id']} | {status} | {r['regression_note']} | {' > '.join(r['tools']) or 'none'} | {refunds} |")
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Run the regression suite.")
    p.add_argument("--mode", default="auto", choices=["auto", "live", "stub"])
    p.add_argument("--from-traces", action="store_true", help="reuse results/traces.jsonl instead of re-running the agent")
    a = p.parse_args(argv)
    s = run_regression(a.mode, a.from_traces)
    write_json(REGRESSION_JSON, s)
    REGRESSION_MD.write_text(render_md(s), encoding="utf-8")
    print(f"[regression] {s['passed']}/{s['total']} passed -> {REGRESSION_MD}")
    raise SystemExit(0 if s["passed"] == s["total"] else 1)


if __name__ == "__main__":
    main()
