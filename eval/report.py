"""Stage 7: evaluation report + deploy-gate verdict.

Reads traces, deterministic results, judge scores, regression results and calibration
results, and writes results/report.md (for humans) and results/report.json (for CI).
"""
from __future__ import annotations

import argparse
import datetime as dt
from collections import Counter, defaultdict

from eval.config import (CALIBRATION_JSON, CATEGORIES, DETERMINISTIC_PATH, GATE, REGRESSION_JSON, REPORT_JSON,
                         REPORT_MD, TRACES_PATH)
from eval.io_utils import load_dataset, read_json, read_jsonl, write_json
from eval.judge import scores_path

# Concrete agent changes for each failure tag (what to fix in the AGENT, not the eval).
AGENT_FIXES = {
    "multi_intent": "Add a rule and a few-shot example: 'Before replying, list every request in the message and "
                    "answer each one.' Add a final self-check step that compares that list with the draft reply.",
    "refund_window": "The 30-day window is applied off-by-one at the boundary. State it numerically in the system "
                     "prompt (`days_since_delivery <= 30`) and, better, have get_order_status return an "
                     "`eligible_for_refund` flag so the model never does date arithmetic.",
    "boundary": "Add boundary cases (day 30, day 31, exactly $500.00, $500.01) to the dataset as permanent tests.",
    "language": "Tell the agent to detect the customer's language and reply in it, add a non-English few-shot "
                "example, and add a language-match criterion (rubric v2 I3) to the gate.",
    "partial_refund": "Compute partial refunds from line items only; consider a `calculate_refund(order_id, items)` "
                      "tool so the amount is never produced by the model.",
    "user_stated_amount": "Never pass a customer-stated price to issue_refund; derive amounts from the order record.",
    "double_refund": "Check the `refunded` flag before refunding and allow one issue_refund per order per conversation; "
                     "also make the backend reject a second refund (defence in depth).",
    "ownership": "Compare the order's customer_email with the session before any action or disclosure.",
    "prompt_injection_direct": "Strengthen the injection rule with examples of authority claims; never act on them.",
    "prompt_injection_indirect": "Wrap tool results in clearly delimited data blocks and strip free-text `notes` from "
                                 "tool output the model doesn't need.",
    "encoded_injection": "Explain why encoded instructions can't be acted on, rather than calling them off-topic.",
    "social_engineering": "Never skip verification based on claimed roles; route staff requests to internal tools.",
    "pii_exfiltration": "Remove PII fields (email, address) from tool responses unless the session owns the order.",
    "out_of_scope": "Keep the scope rule; add an example of a polite decline with no tool calls.",
    "hallucinated_action": "Require a successful tool result before any 'done / processed' statement.",
    "escalation": "Keep the >$500 escalation rule and include the follow-up timeframe in the reply.",
    "not_found": "On order_not_found, ask the customer to re-check the number; never guess another order.",
    "ambiguous": "When the order is ambiguous, list the customer's own orders and ask which one.",
    "not_delivered": "Explain why undelivered orders can't be refunded and what the customer can do instead.",
    "lookup": "Use lookup_orders_by_email only with the session email, then verify the matched order.",
}
def tag_rank(tag):
    keys = list(AGENT_FIXES)
    return keys.index(tag) if tag in keys else len(keys)


GENERIC_TAGS = {"refund", "status", "policy", "multi_item", "multi_order", "clarify"}


def pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def build(rubric: str | None = None) -> dict:
    cases = {c["id"]: c for c in load_dataset()}
    traces = {t["case_id"]: t for t in read_jsonl(TRACES_PATH)}
    det = {r["case_id"]: r for r in read_json(DETERMINISTIC_PATH)}
    rubric = rubric or ("v2" if scores_path("v2").exists() else "v1")
    judge = {r["case_id"]: r for r in read_jsonl(scores_path(rubric))} if scores_path(rubric).exists() else {}
    regression = read_json(REGRESSION_JSON) if REGRESSION_JSON.exists() else None
    calibration = read_json(CALIBRATION_JSON) if CALIBRATION_JSON.exists() else None
    if isinstance(calibration, list):
        calibration = next((c for c in calibration if c["rubric_version"] == rubric), calibration[-1])

    ids = [i for i in cases if i in det]
    n = len(ids)

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    def cat_stats(sel):
        return {"cases": len(sel),
                "det_pass_rate": mean([1.0 if det[i]["passed"] else 0.0 for i in sel]),
                "task_completion": mean([1.0 if det[i]["task_completed"] else 0.0 for i in sel]),
                "output_accuracy": mean([det[i]["output_accuracy"] for i in sel]),
                "judge_mean": mean([judge[i]["mean"] for i in sel if i in judge])}

    overall = cat_stats(ids)
    overall["critical_failures"] = sum(det[i]["critical_failures"] for i in ids)
    overall["avg_tool_calls"] = mean([len(traces[i]["tool_calls"]) for i in ids])
    overall["avg_latency_s"] = mean([traces[i]["latency_s"] for i in ids])
    overall["tokens"] = {k: sum(traces[i]["usage"].get(k, 0) for i in ids) for k in ("input_tokens", "output_tokens")}
    by_cat = {c: cat_stats([i for i in ids if cases[i]["category"] == c]) for c in CATEGORIES}

    check_stats = defaultdict(lambda: {"applicable": 0, "failed": 0, "severity": None, "cases": []})
    for i in ids:
        for ch in det[i]["checks"]:
            s = check_stats[ch["check"]]
            s["applicable"] += 1
            s["severity"] = ch["severity"]
            if not ch["passed"]:
                s["failed"] += 1
                s["cases"].append(i)

    crit_keys = list(next(iter(judge.values()))["scores"].keys()) if judge else []
    crit = {k: {"overall": mean([judge[i]["scores"][k]["score"] for i in ids if i in judge and k in judge[i]["scores"]]),
                **{c: mean([judge[i]["scores"][k]["score"] for i in ids
                            if i in judge and cases[i]["category"] == c and k in judge[i]["scores"]]) for c in CATEGORIES}}
            for k in crit_keys}

    failures = []
    for i in ids:
        jfail = {k: v["reason"] for k, v in judge.get(i, {}).get("scores", {}).items() if v["score"] == 0}
        if det[i]["failed_checks"] or jfail:
            t = traces[i]
            failures.append({"case_id": i, "category": cases[i]["category"], "input": cases[i]["input"],
                             "tests": cases[i].get("notes", ""), "tags": cases[i].get("tags", []),
                             "decision": t["decision"], "expected_decision": cases[i]["expected"].get("decision"),
                             "tools": [c["tool"] for c in t["tool_calls"]],
                             "reply": t["final_output"], "failed_checks": det[i]["failed_checks"],
                             "judge_failed": jfail, "critical": det[i]["critical_failures"] > 0,
                             "deterministic_fail": not det[i]["passed"]})

    adversarial = [i for i in ids if cases[i]["category"] == "adversarial"]
    adv_rate = mean([1.0 if det[i]["passed"] else 0.0 for i in adversarial])
    reg_rate = regression["pass_rate"] if regression else None
    gate_rows = [
        ("Critical failures", overall["critical_failures"], f"<= {GATE['max_critical_failures']}",
         overall["critical_failures"] <= GATE["max_critical_failures"]),
        ("Regression pass rate", reg_rate, f">= {GATE['min_regression_pass_rate']:.0%}",
         reg_rate is not None and reg_rate >= GATE["min_regression_pass_rate"]),
        ("Adversarial pass rate", adv_rate, f">= {GATE['min_adversarial_pass_rate']:.0%}",
         adv_rate is not None and adv_rate >= GATE["min_adversarial_pass_rate"]),
        ("Deterministic case pass rate", overall["det_pass_rate"], f">= {GATE['min_case_pass_rate']:.0%}",
         overall["det_pass_rate"] >= GATE["min_case_pass_rate"]),
        ("Task completion", overall["task_completion"], f">= {GATE['min_task_completion']:.0%}",
         overall["task_completion"] >= GATE["min_task_completion"]),
        ("Judge mean (response quality)", overall["judge_mean"], f">= {GATE['min_judge_mean']:.0%}",
         overall["judge_mean"] is not None and overall["judge_mean"] >= GATE["min_judge_mean"]),
    ]
    verdict = "PASS" if all(r[3] for r in gate_rows) else "FAIL"

    # Recommendations: agent fixes for tags on deterministic failures, critical first, then by frequency.
    tag_cases = defaultdict(set)
    tag_critical = Counter()
    for f in failures:
        if not f["deterministic_fail"]:
            continue
        for tag in f["tags"]:
            if tag not in GENERIC_TAGS:
                tag_cases[tag].add(f["case_id"])
                tag_critical[tag] += f["critical"]
    recs = [{"tag": t, "cases": sorted(c), "critical": tag_critical[t] > 0,
             "fix": AGENT_FIXES.get(t, "Investigate the failing traces and add a targeted prompt rule.")}
            for t, c in sorted(tag_cases.items(), key=lambda kv: (-tag_critical[kv[0]], -len(kv[1]), tag_rank(kv[0])))]

    # Judge-only flags: cross-check with calibration before blaming the agent.
    disputed = {(d["case_id"], d["criterion"]) for d in (calibration or {}).get("disagreements", [])
                if d["direction"] == "strict"}
    by_crit = defaultdict(list)
    for f in failures:
        if not f["deterministic_fail"]:
            for k in f["judge_failed"]:
                by_crit[k].append(f["case_id"])
                f.setdefault("judge_disputed", []).append(k) if (f["case_id"], k) in disputed else None
    for k, cs in by_crit.items():
        dis = [c for c in cs if (c, k) in disputed]
        if dis and len(dis) == len(cs):
            fix = (f"Judge-only {k} flags on cases that pass every hard check. Calibration shows the human labeler "
                   f"disagrees on all of them (judge too strict), so this is a rubric problem, not an agent problem: "
                   "apply the rubric fix in calibration/calibration_report.md and re-run. No agent change.")
        else:
            fix = (f"Judge-only {k} flags on cases that pass every hard check. Review these traces; where the judge "
                   "is right, fix the agent and add a deterministic check so it is caught without a model.")
        recs.append({"tag": f"judge:{k}", "cases": cs, "critical": False, "fix": fix})

    first = traces[ids[0]] if ids else {}
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "agent": {"name": first.get("agent"), "version": first.get("agent_version"), "mode": first.get("agent_mode"),
                  "model": first.get("model")},
        "judge": {"rubric": rubric, "mode": next(iter(judge.values()))["judge_mode"] if judge else None,
                  "model": next(iter(judge.values()))["judge_model"] if judge else None},
        "dataset": {"total": n, "by_category": dict(Counter(cases[i]["category"] for i in ids))},
        "verdict": verdict, "gate": [{"criterion": a, "value": b, "threshold": c, "passed": d} for a, b, c, d in gate_rows],
        "overall": overall, "by_category": by_cat, "checks": dict(check_stats), "judge_criteria": crit,
        "failures": failures, "regression": regression, "calibration": calibration, "recommendations": recs,
    }


def render(r: dict) -> str:
    L = [f"# Agent evaluation report - {r['agent']['name']} v{r['agent']['version']}", ""]
    if r["agent"]["mode"] == "stub" or r["judge"]["mode"] == "stub":
        L += ["> **Offline demo run.** Agent and/or judge ran in stub mode (no API calls). The pipeline, checks and "
              "report are real; for sign-off numbers run `python run_all.py --mode live`.", ""]
    L += [f"Generated {r['generated_at']} - agent mode `{r['agent']['mode']}`"
          + (f" (model `{r['agent']['model']}`)" if r["agent"]["model"] else "")
          + f" - judge `{r['judge']['model']}` with rubric {r['judge']['rubric']} - "
          f"{r['dataset']['total']} cases ({', '.join(f'{k}: {v}' for k, v in r['dataset']['by_category'].items())})", ""]

    icon = "PASS - safe to deploy" if r["verdict"] == "PASS" else "FAIL - do not deploy"
    L += [f"## Deploy gate: **{icon}**", "", "| Gate criterion | Value | Threshold | Result |", "|---|---|---|---|"]
    for g in r["gate"]:
        v = g["value"] if isinstance(g["value"], int) else pct(g["value"])
        L.append(f"| {g['criterion']} | {v} | {g['threshold']} | {'pass' if g['passed'] else '**FAIL**'} |")
    blockers = [g["criterion"] for g in r["gate"] if not g["passed"]]
    if blockers:
        L += ["", f"Blocking: {', '.join(blockers)}. See failure analysis and recommendations below."]

    o = r["overall"]
    L += ["", "## Overall scores", "",
          "| Metric | Value | How it's measured |", "|---|---|---|",
          f"| Task completion | {pct(o['task_completion'])} | Run finished, decision matches expected, all required tools called |",
          f"| Output accuracy | {pct(o['output_accuracy'])} | Mean share of accuracy checks passed (refunds, decision, required/forbidden content) |",
          f"| Response quality (judge) | {pct(o['judge_mean'])} | Mean of 0/1 rubric criteria |",
          f"| Deterministic case pass rate | {pct(o['det_pass_rate'])} | Cases passing every hard check |",
          f"| Critical failures | {o['critical_failures']} | Money movement, security or privacy violations |",
          f"| Avg tool calls / case | {o['avg_tool_calls']} | |",
          f"| Avg latency / case | {o['avg_latency_s']} s | |",
          f"| Tokens (in / out) | {o['tokens']['input_tokens']:,} / {o['tokens']['output_tokens']:,} | |",
          "", "### By category", "",
          "| Category | Cases | Hard-check pass | Task completion | Output accuracy | Judge mean |", "|---|---|---|---|---|---|"]
    for c, s in r["by_category"].items():
        L.append(f"| {c} | {s['cases']} | {pct(s['det_pass_rate'])} | {pct(s['task_completion'])} | "
                 f"{pct(s['output_accuracy'])} | {pct(s['judge_mean'])} |")

    L += ["", "### Deterministic checks", "", "| Check | Severity | Applicable | Failed | Failing cases |", "|---|---|---|---|---|"]
    for name, s in sorted(r["checks"].items(), key=lambda kv: (-kv[1]["failed"], kv[0])):
        L.append(f"| {name} | {s['severity']} | {s['applicable']} | {s['failed']} | {', '.join(s['cases']) or '-'} |")

    if r["judge_criteria"]:
        L += ["", f"### Judge criteria (rubric {r['judge']['rubric']}, pass rate)", "",
              "| Criterion | Overall | " + " | ".join(CATEGORIES) + " |", "|---|---|" + "---|" * len(CATEGORIES)]
        for k, v in r["judge_criteria"].items():
            L.append(f"| {k} | {pct(v['overall'])} | " + " | ".join(pct(v[c]) for c in CATEGORIES) + " |")

    L += ["", "## Failure analysis", ""]
    if not r["failures"]:
        L.append("No failures.")
    for f in r["failures"]:
        sev = "CRITICAL" if f["critical"] else ("hard-check failure" if f["deterministic_fail"] else "judge-only flag")
        L += [f"### {f['case_id']} ({f['category']}) - {sev}", "",
              f"- **Input:** {f['input']}",
              f"- **What this case tests:** {f['tests']}",
              f"- **Agent did:** decision `{f['decision']}` (expected {f['expected_decision']}); tools: "
              f"{' > '.join(f['tools']) or 'none'}",
              f"- **Reply:** {f['reply'][:400].replace(chr(10), ' ')}"]
        for ch in f["failed_checks"]:
            L.append(f"- **Check failed - {ch['check']}** ({ch['severity']}): {ch['detail']}")
        for k, reason in f["judge_failed"].items():
            note = " _(disputed: human label disagrees, see calibration)_" if k in f.get("judge_disputed", []) else ""
            L.append(f"- **Judge {k} = 0:** {reason}{note}")
        L.append("")

    patterns = defaultdict(list)
    for f in r["failures"]:
        for ch in f["failed_checks"]:
            patterns[ch["check"]].append(f["case_id"])
    if patterns:
        L += ["### Failure patterns", ""]
        for chk, cs in sorted(patterns.items(), key=lambda kv: -len(kv[1])):
            L.append(f"- `{chk}` failed in {len(cs)} case(s): {', '.join(cs)}")

    reg = r["regression"]
    L += ["", "## Regression results", ""]
    if not reg:
        L.append("Regression suite not run.")
    else:
        L += [f"**{reg['passed']}/{reg['total']} passed** (agent mode `{reg['agent_mode']}`).", "",
              "| Case | Status | Original failure |", "|---|---|---|"]
        for c in reg["cases"]:
            L.append(f"| {c['case_id']} | {'PASS' if c['passed'] else '**FAIL**'} | {c['regression_note']} |")

    cal = r["calibration"]
    L += ["", "## Judge trustworthiness (calibration)", ""]
    if not cal:
        L.append("Calibration not run.")
    else:
        L.append(f"Rubric {cal['rubric_version']}: agreement with human labels {pct(cal['pooled']['agreement'])}, "
                 f"Cohen's kappa {cal['pooled']['kappa']} ({cal['pooled']['interpretation']}) over "
                 f"{cal['labels_used']} labeled cases; {len(cal['disagreements'])} disagreements. "
                 "Full analysis: `calibration/calibration_report.md`.")
        weak = [k for k, v in cal["per_criterion"].items() if v["agreement"] is not None and v["agreement"] < 0.9]
        if weak:
            L.append(f"Treat judge scores on {', '.join(weak)} with caution (agreement below 90%); the gate relies on "
                     "deterministic checks for anything safety-critical.")

    L += ["", "## Recommendations for the agent", ""]
    if not r["recommendations"]:
        L.append("No changes required by this run.")
    for i, rec in enumerate(r["recommendations"], 1):
        tag = "[critical] " if rec["critical"] else ""
        L.append(f"{i}. {tag}**{rec['tag']}** (cases {', '.join(rec['cases'])}): {rec['fix']}")
    L += ["", "After fixing, turn each fixed known-failure case into a regression test (`\"regression\": true` plus a "
          "`regression_note`) so it can never silently come back."]
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Generate the evaluation report and gate verdict.")
    p.add_argument("--rubric", default=None, help="judge rubric version to report (default: newest available)")
    a = p.parse_args(argv)
    r = build(a.rubric)
    write_json(REPORT_JSON, r)
    REPORT_MD.write_text(render(r), encoding="utf-8")
    print(f"[report] verdict={r['verdict']} -> {REPORT_MD}")
    return r


if __name__ == "__main__":
    main()
