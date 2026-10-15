"""Small JSON / JSONL helpers and dataset validation."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from eval.config import CATEGORIES, DATASET_PATH, MIN_CASES

REQUIRED_CASE_KEYS = {"id", "category", "input", "customer_email", "expected", "tags", "regression"}


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_dataset(path: Path = DATASET_PATH, validate: bool = True) -> list[dict]:
    cases = read_jsonl(path)
    if validate:
        validate_dataset(cases)
    return cases


def validate_dataset(cases: list[dict]) -> dict:
    """Fail loudly if the dataset doesn't meet the brief's requirements."""
    problems = []
    ids = [c.get("id") for c in cases]
    if len(ids) != len(set(ids)):
        problems.append("duplicate case ids")
    for c in cases:
        missing = REQUIRED_CASE_KEYS - c.keys()
        if missing:
            problems.append(f"{c.get('id')}: missing keys {sorted(missing)}")
        if c.get("category") not in CATEGORIES:
            problems.append(f"{c.get('id')}: bad category {c.get('category')!r}")
        if c.get("regression") and not c.get("regression_note"):
            problems.append(f"{c.get('id')}: regression case without regression_note")
    counts = Counter(c.get("category") for c in cases)
    if len(cases) < MIN_CASES:
        problems.append(f"only {len(cases)} cases (< {MIN_CASES})")
    for cat in CATEGORIES:
        if counts[cat] == 0:
            problems.append(f"no cases in category {cat}")
    n_reg = sum(1 for c in cases if c.get("regression"))
    if n_reg < 3:
        problems.append(f"only {n_reg} regression cases (< 3)")
    if problems:
        raise ValueError("Dataset validation failed:\n  - " + "\n  - ".join(problems))
    return {"total": len(cases), "by_category": dict(counts), "regression_cases": n_reg}
