"""Trajectory recorder shared by every agent implementation.

Every tool call goes through `TraceRecorder.call`, so tool selection, parameters,
results and ordering are captured identically regardless of which agent runs.
"""
from __future__ import annotations

from typing import Any


class TraceRecorder:
    def __init__(self, backend) -> None:
        self.backend = backend
        self.steps: list[dict[str, Any]] = []

    def call(self, tool: str, args: dict) -> dict:
        result = self.backend.call(tool, args)
        self.steps.append({"type": "tool_call", "tool": tool, "args": dict(args), "result": result})
        return result

    def text(self, text: str) -> None:
        """Intermediate assistant text (visible reasoning / key decisions between tool calls)."""
        if text and text.strip():
            self.steps.append({"type": "assistant_text", "text": text.strip()})
