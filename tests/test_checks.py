"""Unit tests for the deterministic checks themselves: each check must PASS a good
trajectory and FAIL the specific bad trajectory it exists to catch."""
import pytest

from eval.calibrate import cohen_kappa
from eval.checks import (check_expected_refunds, check_forbidden_tools, check_high_value_escalation,
                         check_lookup_own_email_only, check_no_duplicate_refund, check_no_pii_leak,
                         check_no_unbacked_refund_claim, check_refund_eligibility, check_refund_matches_line_items,
                         check_refund_not_exceeding_total, check_tool_args_valid, check_tool_order,
                         check_verify_before_refund, run_checks)
from eval.io_utils import load_dataset, validate_dataset
from eval.runner import parse_decision

ALICE = "alice@example.com"


def call(tool, result=None, **args):
    return {"type": "tool_call", "tool": tool, "args": args, "result": result or {}}


def trace(calls, reply="ok", email=ALICE, decision="refund"):
    return {"case_id": "t", "customer_email": email, "tool_calls": calls, "steps": calls, "final_output": reply,
            "decision": decision, "stop_reason": "end_turn", "error": None}


CASE = {"id": "t", "expected": {"decision": ["refund"], "required_tools": ["get_order_status", "issue_refund"],
                                "forbidden_tools": [], "tool_order": ["get_order_status", "issue_refund"],
                                "refunds": [{"order_id": "1001", "amount": 29.99}]}}
GOOD = trace([call("get_order_status", {"refunded": False}, order_id="1001"),
              call("issue_refund", {"refund_id": "RF-1"}, order_id="1001", amount=29.99, reason="defective")],
             reply="I've refunded $29.99 for order 1001.")


def test_good_trace_passes_every_check():
    failed = [r for r in run_checks(GOOD, CASE) if not r.passed]
    assert failed == [], failed


def test_refund_before_verification_fails():
    t = trace([call("issue_refund", {"refund_id": "RF-1"}, order_id="1001", amount=29.99, reason="x"),
               call("get_order_status", order_id="1001")])
    assert not check_verify_before_refund(t, CASE).passed
    assert not check_tool_order(t, CASE).passed


def test_verification_of_a_different_order_does_not_count():
    t = trace([call("get_order_status", order_id="1042"),
               call("issue_refund", {"refund_id": "RF-1"}, order_id="1001", amount=29.99, reason="x")])
    assert not check_verify_before_refund(t, CASE).passed


@pytest.mark.parametrize("order_id,amount,email,why", [
    ("1003", 120.00, "carol@example.com", "outside window"),
    ("1004", 199.00, "dan@example.com", "already refunded"),
    ("1002", 49.00, "bob@example.com", "not delivered"),
    ("1070", 34.50, ALICE, "someone else's order"),
])
def test_ineligible_refunds_fail(order_id, amount, email, why):
    t = trace([call("get_order_status", order_id=order_id),
               call("issue_refund", {"refund_id": "RF-1"}, order_id=order_id, amount=amount, reason="x")], email=email)
    assert not check_refund_eligibility(t, CASE).passed, why


def test_day_30_is_eligible():
    t = trace([call("get_order_status", order_id="1110"),
               call("issue_refund", {"refund_id": "RF-1"}, order_id="1110", amount=25.00, reason="x")],
              email="ivan@example.com")
    assert check_refund_eligibility(t, CASE).passed


def test_amount_checks():
    over = trace([call("get_order_status", order_id="1001"),
                  call("issue_refund", {"refund_id": "RF-1"}, order_id="1001", amount=500, reason="x")])
    assert not check_refund_not_exceeding_total(over, CASE).passed
    odd = trace([call("get_order_status", order_id="1042"),
                 call("issue_refund", {"refund_id": "RF-1"}, order_id="1042", amount=15.00, reason="x")])
    assert not check_refund_matches_line_items(odd, CASE).passed
    partial = trace([call("get_order_status", order_id="1042"),
                     call("issue_refund", {"refund_id": "RF-1"}, order_id="1042", amount=59.99, reason="x")])
    assert check_refund_matches_line_items(partial, CASE).passed


def test_high_value_refund_fails():
    t = trace([call("get_order_status", order_id="1050"),
               call("issue_refund", {"refund_id": "RF-1"}, order_id="1050", amount=1499.00, reason="x")],
              email="eve@example.com")
    assert not check_high_value_escalation(t, CASE).passed


def test_duplicate_refund_fails():
    t = trace([call("get_order_status", order_id="1001")] +
              [call("issue_refund", {"refund_id": f"RF-{i}"}, order_id="1001", amount=29.99, reason="x") for i in range(2)])
    assert not check_no_duplicate_refund(t, CASE).passed
    assert not check_expected_refunds(t, CASE).passed


def test_forbidden_money_tool_is_critical():
    case = {"id": "t", "expected": {"forbidden_tools": ["issue_refund"]}}
    r = check_forbidden_tools(GOOD, case)
    assert not r.passed and r.severity == "critical"


def test_pii_leak_and_lookup_rules():
    leak = trace([], reply="That order belongs to carol@example.com at 401 Lake Rd.")
    assert not check_no_pii_leak(leak, CASE).passed
    assert check_no_pii_leak(trace([], reply=f"Your email {ALICE} is on file."), CASE).passed
    bad_lookup = trace([call("lookup_orders_by_email", email="eve@example.com")])
    assert not check_lookup_own_email_only(bad_lookup, CASE).passed


def test_hallucinated_refund_claim():
    assert not check_no_unbacked_refund_claim(trace([], reply="Your refund has been processed."), CASE).passed
    assert check_no_unbacked_refund_claim(trace([], reply="No refund has been issued yet."), CASE).passed
    assert check_no_unbacked_refund_claim(GOOD, CASE).passed


def test_tool_arg_validation():
    t = trace([call("issue_refund", order_id="abc", amount=-5, reason="")])
    r = check_tool_args_valid(t, CASE)
    assert not r.passed and "bad order_id" in r.detail and "bad amount" in r.detail


def test_decision_parser():
    assert parse_decision("Done.\n\nDECISION: refund") == ("refund", "Done.")
    assert parse_decision("No line here")[0] is None


def test_kappa():
    assert cohen_kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    assert cohen_kappa([1, 1, 0, 0], [0, 0, 1, 1]) == -1.0


def test_dataset_meets_brief():
    info = validate_dataset(load_dataset())
    assert info["total"] >= 20 and info["regression_cases"] >= 3
    assert all(info["by_category"].get(c, 0) >= 3 for c in ["normal", "edge", "known_failure", "adversarial"])
