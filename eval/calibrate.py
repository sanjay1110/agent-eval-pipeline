"""Stage 5: judge calibration against human labels.

Reports, per rubric version:
  * percent agreement and Cohen's kappa, per criterion and pooled over all (case, criterion) pairs
  * a confusion matrix (judge vs human)
  * every disagreement with the judge's reason and the human's note
  * direction of each disagreement (judge too lenient / too strict) and the rubric change it implies
  * v1 -> v2 comparison when judge_scores_v2.jsonl exists

Labels whose trace fingerprint no longer matches the current trace are reported as STALE and
excluded, so the judge is never compared against a human label for a different reply.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from eval.config import CALIBRATION_JSON, CALIBRATION_MD, HUMAN_LABELS_PATH, TRACES_PATH
from eval.fingerprint import trace_fingerprint
from eval.io_utils import read_jsonl, write_json
from eval.judge import criteria_for, scores_path

MIN_LABELS = 20

# Rubric change implied by each disagreement pattern (criterion, direction).
RUBRIC_FIXES = {
    ("C1", "lenient"): "Make the judge list every distinct request first and score C1 from that list; "
                       "long, fluent replies were passing although a request was dropped.",
    ("C1", "strict"): "State that explicitly declined or deferred requests count as addressed.",
    ("R2", "lenient"): "Require the judge to restate the decisive fact and policy rule and check the decision "
                       "against it; a plausible-sounding reason should only count if it is correct.",
    ("R2", "strict"): "Allow obvious reasons for routine successful actions instead of demanding an explicit one.",
    ("H1", "strict"): "Define 'resolved': a correct, explained denial or a completed action is resolved; only "
                      "pending or dropped items need a next step.",
    ("H1", "lenient"): "Require the next step to name a concrete action or timeframe; 'let me know' is not enough.",
    ("I1", "lenient"): "Count wrongful denials (refusing what the policy entitles the customer to) as policy "
                       "violations, not only wrongful refunds.",
    ("I1", "strict"): "Clarify that a refusal grounded in a correct policy rule is compliant.",
    ("I2", "lenient"): "Split bundled sub-checks: language match becomes its own criterion so a satisfied scope/"
                       "privacy check can't mask a language failure.",
    ("I2", "strict"): "Clarify that listing the customer's own order numbers is not a privacy leak.",
    ("R1", "lenient"): "Ask the judge to quote the tool field that supports each number or date in the reply.",
    ("R1", "strict"): "Clarify that restating policy text from search_policy counts as grounded.",
    ("C2", "lenient"): "Require the refund amount or the explicit denial in the reply text for C2 = 1.",
    ("C2", "strict"): "Accept any unambiguous statement of the outcome, not a fixed phrase.",
    ("H2", "lenient"): "Give a hard word limit and list filler patterns (greetings, repeated apologies).",
    ("H2", "strict"): "Raise the word allowance when several requests must be answered.",
}


def cohen_kappa(a: list[int], b: list[int]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pa1, pb1 = sum(a) / n, sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return round((po - pe) / (1 - pe), 3)


def interpret_kappa(k: float | None) -> str:
    if k is None:
        return "n/a"
    for lim, word in [(0.2, "slight"), (0.4, "fair"), (0.6, "moderate"), (0.8, "substantial")]:
        if k < lim:
            return word
    return "almost perfect"


def calibrate(rubric_version: str = "v1") -> dict:
    keys = criteria_for(rubric_version)
    score_field = "scores" if rubric_version == "v1" else f"scores_{rubric_version}"
    traces = {t["case_id"]: t for t in read_jsonl(TRACES_PATH)}
    judge = {r["case_id"]: r for r in read_jsonl(scores_path(rubric_version))}
    labels = [r for r in read_jsonl(HUMAN_LABELS_PATH) if score_field in r]

    used, stale, missing = [], [], []
    for lab in labels:
        cid = lab["case_id"]
        if cid not in judge or not judge[cid]["scores"] or cid not in traces:
            missing.append(cid)
        elif lab.get("fingerprint") and lab["fingerprint"] != trace_fingerprint(traces[cid]):
            stale.append(cid)
        else:
            used.append(lab)

    per = {}
    all_h, all_j = [], []
    disagreements = []
    confusion = {"j1_h1": 0, "j1_h0": 0, "j0_h1": 0, "j0_h0": 0}
    for k in keys:
        h, j = [], []
        for lab in used:
            if k not in lab[score_field] or k not in judge[lab["case_id"]]["scores"]:
                continue
            hv, jv = int(lab[score_field][k]), int(judge[lab["case_id"]]["scores"][k]["score"])
            h.append(hv); j.append(jv)
            confusion[f"j{jv}_h{hv}"] += 1
            if hv != jv:
                disagreements.append({
                    "case_id": lab["case_id"], "category": traces[lab["case_id"]]["category"], "criterion": k,
                    "human": hv, "judge": jv, "direction": "lenient" if jv > hv else "strict",
                    "judge_reason": judge[lab["case_id"]]["scores"][k]["reason"],
                    "human_note": lab.get("notes", {}).get(k, ""),
                })
        all_h += h; all_j += j
        n = len(h)
        per[k] = {"n": n, "agreement": round(sum(x == y for x, y in zip(h, j)) / n, 3) if n else None,
                  "kappa": cohen_kappa(j, h), "human_pass_rate": round(sum(h) / n, 3) if n else None,
                  "judge_pass_rate": round(sum(j) / n, 3) if n else None,
                  "judge_lenient": sum(1 for x, y in zip(j, h) if x > y),
                  "judge_strict": sum(1 for x, y in zip(j, h) if x < y)}

    case_exact = sum(1 for lab in used
                     if all(int(lab[score_field][k]) == judge[lab["case_id"]]["scores"][k]["score"]
                            for k in keys if k in lab[score_field] and k in judge[lab["case_id"]]["scores"]))
    patterns = defaultdict(list)
    for d in disagreements:
        patterns[(d["criterion"], d["direction"])].append(d["case_id"])
    fixes = [{"criterion": c, "direction": dr, "cases": ids,
              "fix": RUBRIC_FIXES.get((c, dr), "Tighten the criterion wording with a worked example.")}
             for (c, dr), ids in sorted(patterns.items(), key=lambda kv: -len(kv[1]))]

    return {
        "rubric_version": rubric_version,
        "judge_mode": next(iter(judge.values()))["judge_mode"] if judge else None,
        "judge_model": next(iter(judge.values()))["judge_model"] if judge else None,
        "labels_used": len(used), "labels_stale": stale, "labels_missing_judge": missing,
        "meets_min_labels": len(used) >= MIN_LABELS,
        "pooled": {"n": len(all_h), "agreement": round(sum(x == y for x, y in zip(all_h, all_j)) / len(all_h), 3) if all_h else None,
                   "kappa": cohen_kappa(all_j, all_h), "interpretation": interpret_kappa(cohen_kappa(all_j, all_h))},
        "case_exact_match": round(case_exact / len(used), 3) if used else None,
        "confusion": confusion, "per_criterion": per, "disagreements": disagreements, "rubric_fixes": fixes,
    }


def render_md(results: list[dict]) -> str:
    L = ["# Judge calibration report", ""]
    first = results[0]
    if first["judge_mode"] == "stub":
        L += ["> **Offline demo numbers.** These scores come from the heuristic stub judge, not Claude. "
              "Re-run `python run_all.py --mode live` and re-label with `python -m eval.label` for real calibration.", ""]
    L += ["Human labels are compared with the judge on the same traces (label fingerprints checked). "
          "Agreement = share of (case, criterion) pairs where judge and human give the same 0/1. "
          "Cohen's kappa corrects for chance agreement, which matters here because most criteria pass most of the time.", ""]
    L += ["## Summary", "", "| Rubric | Labels used | Pooled agreement | Pooled kappa | Case exact-match | Disagreements |",
          "|---|---|---|---|---|---|"]
    for r in results:
        L.append(f"| {r['rubric_version']} | {r['labels_used']} | {r['pooled']['agreement']:.1%} | "
                 f"{r['pooled']['kappa']} ({r['pooled']['interpretation']}) | {r['case_exact_match']:.1%} | {len(r['disagreements'])} |")
    for r in results:
        if r["labels_stale"]:
            L += ["", f"**Stale labels excluded ({r['rubric_version']}):** {', '.join(r['labels_stale'])} - the agent's reply "
                  "changed since these were labeled. Re-label with `python -m eval.label`."]
        if not r["meets_min_labels"]:
            L += ["", f"**Warning:** only {r['labels_used']} usable labels for {r['rubric_version']} (brief requires >= {MIN_LABELS})."]

    for r in results:
        c = r["confusion"]
        L += ["", f"## Rubric {r['rubric_version']} (judge: {r['judge_model']})", "",
              "Confusion matrix over all (case, criterion) pairs:", "",
              "| | Human 1 | Human 0 |", "|---|---|---|",
              f"| **Judge 1** | {c['j1_h1']} | {c['j1_h0']} (judge too lenient) |",
              f"| **Judge 0** | {c['j0_h1']} (judge too strict) | {c['j0_h0']} |", "",
              "| Criterion | n | Agreement | Kappa | Human pass | Judge pass | Lenient | Strict |",
              "|---|---|---|---|---|---|---|---|"]
        for k, v in r["per_criterion"].items():
            L.append(f"| {k} | {v['n']} | {v['agreement']:.0%} | {v['kappa']} | {v['human_pass_rate']:.0%} | "
                     f"{v['judge_pass_rate']:.0%} | {v['judge_lenient']} | {v['judge_strict']} |")
        L += ["", "### Disagreements", ""]
        if not r["disagreements"]:
            L.append("None.")
        else:
            L += ["| Case | Crit. | Human | Judge | Direction | Judge's reason | Human's note |", "|---|---|---|---|---|---|---|"]
            for d in r["disagreements"]:
                L.append(f"| {d['case_id']} | {d['criterion']} | {d['human']} | {d['judge']} | {d['direction']} | "
                         f"{d['judge_reason']} | {d['human_note'] or '-'} |")
        L += ["", "### What to change in the rubric", ""]
        if not r["rubric_fixes"]:
            L.append("No systematic disagreement patterns.")
        for f in r["rubric_fixes"]:
            L.append(f"- **{f['criterion']} - judge too {f['direction']}** (cases {', '.join(f['cases'])}): {f['fix']}")

    if len(results) > 1:
        a, b = results[0], results[-1]
        L += ["", f"## {a['rubric_version']} -> {b['rubric_version']}", "",
              f"Pooled agreement {a['pooled']['agreement']:.1%} -> {b['pooled']['agreement']:.1%}; "
              f"kappa {a['pooled']['kappa']} -> {b['pooled']['kappa']}; "
              f"disagreements {len(a['disagreements'])} -> {len(b['disagreements'])}.", ""]
        fixed = {(d["case_id"], d["criterion"]) for d in a["disagreements"]} - {(d["case_id"], d["criterion"]) for d in b["disagreements"]}
        remaining = b["disagreements"]
        if fixed:
            L.append("Resolved by the rubric change: " + ", ".join(f"{c}/{k}" for c, k in sorted(fixed)) + ".")
        if remaining:
            L.append("Still disagreeing: " + ", ".join(f"{d['case_id']}/{d['criterion']}" for d in remaining)
                     + ". These need a further rubric change or are genuine judge limitations - see the table above.")
    return "\n".join(L) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="Compare judge scores with human labels.")
    p.add_argument("--rubrics", nargs="*", default=None, help="rubric versions to calibrate (default: all with scores)")
    a = p.parse_args(argv)
    versions = a.rubrics or [v for v in ("v1", "v2") if scores_path(v).exists()]
    results = [calibrate(v) for v in versions]
    write_json(CALIBRATION_JSON, results if len(results) > 1 else results[0])
    CALIBRATION_MD.write_text(render_md(results), encoding="utf-8")
    for r in results:
        print(f"[calibrate] {r['rubric_version']}: labels={r['labels_used']} agreement={r['pooled']['agreement']} "
              f"kappa={r['pooled']['kappa']} disagreements={len(r['disagreements'])} stale={r['labels_stale']}")
    print(f"[calibrate] wrote {CALIBRATION_MD}")


if __name__ == "__main__":
    main()
