"""The agent under test: a Claude tool-use support agent.

To evaluate a different agent from an earlier module, give it the same interface:

    agent.run(user_message: str, customer_email: str, backend) -> dict
        {"raw_output": str, "steps": [...], "stop_reason": str, "turns": int,
         "usage": {"input_tokens": int, "output_tokens": int}, "error": str | None}

and route every tool call through `TraceRecorder.call` so trajectories are captured.
"""
from __future__ import annotations

import json
import os
import time

from agent.mock_backend import TODAY
from agent.prompts import AGENT_VERSION, SYSTEM_PROMPT
from agent.tools_schema import TOOLS
from agent.trace import TraceRecorder

DEFAULT_AGENT_MODEL = "claude-sonnet-5-5"


class ClaudeSupportAgent:
    name = "claude-support-agent"
    version = AGENT_VERSION
    mode = "live"

    def __init__(self, model: str | None = None, max_turns: int = 8, max_retries: int = 3) -> None:
        import anthropic  # imported lazily so offline mode works without the SDK configured

        self.client = anthropic.Anthropic(max_retries=max_retries)
        self.model = model or os.environ.get("AGENT_MODEL", DEFAULT_AGENT_MODEL)
        self.max_turns = max_turns

    def run(self, user_message: str, customer_email: str, backend) -> dict:
        rec = TraceRecorder(backend)
        system = SYSTEM_PROMPT.format(today=TODAY.isoformat(), customer_email=customer_email)
        messages: list[dict] = [{"role": "user", "content": user_message}]
        usage = {"input_tokens": 0, "output_tokens": 0}

        for turn in range(1, self.max_turns + 1):
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=1024, system=system, tools=TOOLS, messages=messages,
                )
            except Exception as exc:  # API failure is recorded, never crashes the eval run
                return {"raw_output": "", "steps": rec.steps, "stop_reason": "api_error",
                        "turns": turn, "usage": usage, "error": repr(exc)}

            usage["input_tokens"] += resp.usage.input_tokens
            usage["output_tokens"] += resp.usage.output_tokens
            messages.append({"role": "assistant", "content": resp.content})
            text = "".join(b.text for b in resp.content if b.type == "text")

            if resp.stop_reason != "tool_use":
                return {"raw_output": text, "steps": rec.steps, "stop_reason": resp.stop_reason,
                        "turns": turn, "usage": usage, "error": None}

            rec.text(text)
            tool_results = []
            for block in resp.content:
                if block.type == "tool_use":
                    result = rec.call(block.name, block.input)
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                         "content": json.dumps(result)})
            messages.append({"role": "user", "content": tool_results})
            time.sleep(0)  # yield; keeps loop friendly when run in threads

        return {"raw_output": "", "steps": rec.steps, "stop_reason": "max_turns",
                "turns": self.max_turns, "usage": usage, "error": "max_turns_exceeded"}
