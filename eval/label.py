"""Interactive CLI for writing your own human labels (do this BEFORE looking at judge scores).

    python -m eval.label                 # label every unlabeled case with rubric v1
    python -m eval.label --relabel       # re-label everything (e.g. after a new agent run)
    python -m eval.label --cases f03 f05 # label specific cases
    python -m eval.label --rubric v2     # write scores_v2 (adds I3)

For each case it shows the customer message, the trajectory and the reply, then asks 0/1 per
criterion and an optional note for every 0. Labels are saved after each case to
data/human_labels.jsonl together with a fingerprint of the trace you labeled.
"""
from __future__ import annotations

import argparse

from eval.config import HUMAN_LABELS_PATH, TRACES_PATH
from eval.fingerprint import trace_fingerprint
from eval.io_utils import read_jsonl, write_jsonl
from eval.judge import criteria_for, format_trajectory


def ask(prompt: str, allowed: set[str]) -> str:
    while True:
        v = input(prompt).strip().lower()
        if v in allowed:
            return v
        print(f"  enter one of {sorted(allowed)}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rubric", default="v1")
    p.add_argument("--labeler", default="me")
    p.add_argument("--relabel", action="store_true")
    p.add_argument("--cases", nargs="*")
    a = p.parse_args(argv)

    keys = criteria_for(a.rubric)
    score_field = "scores" if a.rubric == "v1" else f"scores_{a.rubric}"
    traces = read_jsonl(TRACES_PATH)
    labels = {r["case_id"]: r for r in (read_jsonl(HUMAN_LABELS_PATH) if HUMAN_LABELS_PATH.exists() else [])}

    for t in traces:
        cid = t["case_id"]
        if a.cases and cid not in a.cases:
            continue
        fp = trace_fingerprint(t)
        existing = labels.get(cid, {})
        if not a.relabel and not a.cases and score_field in existing and existing.get("fingerprint") == fp:
            continue
        print("\n" + "=" * 78 + f"\n{cid} [{t['category']}]  customer={t['customer_email']}")
        print(f"\nMESSAGE:\n{t['input']}\n\nTRAJECTORY:\n{format_trajectory(t, 400)}\n\nREPLY:\n{t['final_output']}\n")
        row = dict(existing) | {"case_id": cid, "labeler": a.labeler, "fingerprint": fp}
        scores, notes = {}, dict(existing.get("notes", {}))
        for k in keys:
            scores[k] = int(ask(f"  {k} (0/1): ", {"0", "1"}))
            if scores[k] == 0:
                note = input(f"    why is {k} a 0? ").strip()
                if note:
                    notes[k] = note
        row[score_field], row["notes"] = scores, notes
        labels[cid] = row
        write_jsonl(HUMAN_LABELS_PATH, [labels[k] for k in sorted(labels)])
        print("  saved.")
    print(f"\nLabels in {HUMAN_LABELS_PATH}: {len(labels)}")


if __name__ == "__main__":
    main()
