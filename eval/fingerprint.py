"""Stable fingerprint of what a human labeled: the reply text plus the tool sequence.

Human labels store this so calibration can detect stale labels (the agent's output changed
after the human labeled it) instead of silently comparing the judge to the wrong reply.
"""
import hashlib
import json


def trace_fingerprint(trace: dict) -> str:
    payload = {"reply": trace.get("final_output", ""),
               "tools": [(c["tool"], c["args"]) for c in trace.get("tool_calls", [])]}
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]
