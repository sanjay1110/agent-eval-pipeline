"""Run the whole evaluation pipeline end to end.

    python run_all.py                 # auto: live if ANTHROPIC_API_KEY is set, else offline stub
    python run_all.py --mode live     # real agent + Claude judge (sign-off run)
    python run_all.py --mode stub     # offline, no API calls (CI smoke test)
    python run_all.py --skip-agent    # re-score existing traces (no agent calls)
    python run_all.py --skip-judge    # deterministic checks only

Stages: dataset -> agent traces -> deterministic checks -> judge -> calibration -> regression -> report
Exit code 0 = gate PASS, 1 = gate FAIL (so CI can block a deploy).
"""
from __future__ import annotations

import argparse
import sys

from eval import calibrate, checks, judge, regression, report, runner
from eval.config import DETERMINISTIC_PATH, REGRESSION_JSON, REGRESSION_MD, TRACES_PATH
from eval.io_utils import load_dataset, read_jsonl, validate_dataset, write_json, write_jsonl


def banner(s):
    print(f"\n=== {s} " + "=" * max(0, 70 - len(s)))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", default="auto", choices=["auto", "live", "stub"], help="agent mode")
    p.add_argument("--judge-mode", default=None, choices=["auto", "live", "stub"], help="defaults to --mode")
    p.add_argument("--rubrics", nargs="*", default=["v1", "v2"], help="judge rubric versions to run")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--skip-agent", action="store_true")
    p.add_argument("--skip-judge", action="store_true")
    p.add_argument("--no-gate-exit", action="store_true", help="always exit 0")
    a = p.parse_args(argv)
    judge_mode = a.judge_mode or a.mode

    banner("1. Dataset")
    cases = load_dataset()
    print(validate_dataset(cases))

    banner("2. Agent run (trajectories)")
    if a.skip_agent:
        traces = read_jsonl(TRACES_PATH)
        print(f"reusing {len(traces)} traces from {TRACES_PATH}")
    else:
        traces = runner.run_dataset(cases, a.mode, a.workers)
        write_jsonl(TRACES_PATH, traces)

    banner("3. Deterministic checks")
    det = checks.evaluate_all(traces, cases)
    write_json(DETERMINISTIC_PATH, det)
    for r in det:
        if not r["passed"]:
            print(f"  FAIL {r['case_id']}: " + "; ".join(f"{f['check']}" for f in r["failed_checks"]))
    print(f"{sum(r['passed'] for r in det)}/{len(det)} cases pass every hard check")

    if not a.skip_judge:
        banner("4. LLM-as-judge")
        for v in a.rubrics:
            write_jsonl(judge.scores_path(v), judge.judge_traces(traces, judge_mode, v, a.workers))

        banner("5. Calibration (judge vs human labels)")
        calibrate.main(["--rubrics", *a.rubrics])

    banner("6. Regression suite")
    reg = regression.run_regression(from_traces=True)
    write_json(REGRESSION_JSON, reg)
    REGRESSION_MD.write_text(regression.render_md(reg), encoding="utf-8")
    print(f"{reg['passed']}/{reg['total']} regression tests passed")

    banner("7. Report")
    r = report.main([])
    print(f"\nDEPLOY GATE: {r['verdict']}")
    for g in r["gate"]:
        print(f"  [{'x' if g['passed'] else ' '}] {g['criterion']}: {g['value']} ({g['threshold']})")
    return 0 if (r["verdict"] == "PASS" or a.no_gate_exit) else 1


if __name__ == "__main__":
    sys.exit(main())
