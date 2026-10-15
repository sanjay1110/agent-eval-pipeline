"""Stage 2: run the agent over the dataset and capture full trajectories.

Each case gets a fresh MockBackend. Output: results/traces.jsonl, one trace per case.
Downstream stages only read traces, so checks and the judge can be re-run without
re-running (or paying for) the agent.
"""
from __future__ import annotations

import argparse
import re
import time
from concurrent.futures import ThreadPoolExecutor

from agent import get_agent
from agent.mock_backend import MockBackend
from eval.config import TRACES_PATH
from eval.io_utils import load_dataset, write_jsonl

DECISION_RE = re.compile(r"^\s*DECISION:\s*([a-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_decision(raw: str) -> tuple[str | None, str]:
    """Return (decision, customer-visible text with the DECISION line removed)."""
    matches = DECISION_RE.findall(raw or "")
    decision = matches[-1].lower() if matches else None
    visible = DECISION_RE.sub("", raw or "").strip()
    return decision, visible


def run_case(case: dict, agent) -> dict:
    backend = MockBackend()
    t0 = time.perf_counter()
    try:
        out = agent.run(case["input"], case["customer_email"], backend)
    except Exception as exc:  # an agent crash is a failed case, not a failed pipeline
        out = {"raw_output": "", "steps": [], "stop_reason": "exception", "turns": 0,
               "usage": {}, "error": repr(exc)}
    decision, visible = parse_decision(out.get("raw_output", ""))
    return {
        "case_id": case["id"],
        "category": case["category"],
        "agent": getattr(agent, "name", "agent"),
        "agent_version": getattr(agent, "version", "unknown"),
        "agent_mode": getattr(agent, "mode", "unknown"),
        "model": getattr(agent, "model", None),
        "input": case["input"],
        "customer_email": case["customer_email"],
        "steps": out.get("steps", []),
        "tool_calls": [s for s in out.get("steps", []) if s["type"] == "tool_call"],
        "decision": decision,
        "final_output": visible,
        "raw_output": out.get("raw_output", ""),
        "stop_reason": out.get("stop_reason"),
        "turns": out.get("turns"),
        "usage": out.get("usage", {}),
        "error": out.get("error"),
        "latency_s": round(time.perf_counter() - t0, 3),
        "backend_refunds": backend.refunds_issued,
        "backend_escalations": backend.escalations,
    }


def run_dataset(cases: list[dict], mode: str = "auto", workers: int = 4) -> list[dict]:
    agent = get_agent(mode)
    print(f"[runner] agent={agent.name} version={agent.version} mode={agent.mode} cases={len(cases)}")
    if workers <= 1 or agent.mode == "stub":
        traces = [run_case(c, agent) for c in cases]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            traces = list(pool.map(lambda c: run_case(c, agent), cases))
    for t in traces:
        flag = "ERR" if t["error"] else "ok "
        print(f"  {flag} {t['case_id']:<4} decision={t['decision']!s:<9} tools={[c['tool'] for c in t['tool_calls']]}")
    return traces


def main(argv=None):
    p = argparse.ArgumentParser(description="Run the agent over the dataset and record traces.")
    p.add_argument("--mode", default="auto", choices=["auto", "live", "stub"])
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--only", nargs="*", help="case ids to run")
    a = p.parse_args(argv)
    cases = load_dataset()
    if a.only:
        cases = [c for c in cases if c["id"] in set(a.only)]
    traces = run_dataset(cases, a.mode, a.workers)
    write_jsonl(TRACES_PATH, traces)
    print(f"[runner] wrote {TRACES_PATH}")


if __name__ == "__main__":
    main()
