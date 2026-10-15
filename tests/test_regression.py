"""Permanent regression tests: every previously-found failure, re-run against the agent.

    pytest tests/test_regression.py                     # agent mode from EVAL_MODE (default: auto)
    EVAL_MODE=live pytest tests/test_regression.py -v   # against the real Claude agent

Each case is a dataset row with "regression": true; its regression_note documents the original bug.
"""
import os

import pytest

from agent import get_agent
from eval.checks import evaluate_trace
from eval.regression import regression_cases
from eval.runner import run_case

AGENT = get_agent(os.environ.get("EVAL_MODE", "auto"))
CASES = regression_cases()


@pytest.mark.regression
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_regression(case):
    trace = run_case(case, AGENT)
    result = evaluate_trace(trace, case)
    failures = "\n".join(f"  - {f['check']} [{f['severity']}]: {f['detail']}" for f in result["failed_checks"])
    assert result["passed"], (f"{case['id']} regressed.\nOriginal bug: {case['regression_note']}\n"
                              f"Failed checks:\n{failures}\nReply: {trace['final_output']}")
