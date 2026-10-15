"""Offline test of the live Claude agent loop with a fake API client: checks that tool_use
blocks are executed against the backend, recorded in the trace, and the final text returned."""
from types import SimpleNamespace as NS

from agent.claude_agent import ClaudeSupportAgent
from agent.mock_backend import MockBackend
from eval.runner import parse_decision


def block_text(t):
    return NS(type="text", text=t)


def block_tool(i, name, inp):
    return NS(type="tool_use", id=i, name=name, input=inp)


class FakeMessages:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kw):
        self.calls.append(kw)
        content, stop = self.script.pop(0)
        return NS(content=content, stop_reason=stop, usage=NS(input_tokens=10, output_tokens=5))


def make_agent(script):
    agent = ClaudeSupportAgent.__new__(ClaudeSupportAgent)
    agent.client = NS(messages=FakeMessages(script))
    agent.model, agent.max_turns = "fake", 8
    return agent


def test_tool_loop_records_trajectory():
    agent = make_agent([
        ([block_text("Let me check."), block_tool("t1", "get_order_status", {"order_id": "1001"})], "tool_use"),
        ([block_tool("t2", "issue_refund", {"order_id": "1001", "amount": 29.99, "reason": "defective"})], "tool_use"),
        ([block_text("Refunded $29.99.\nDECISION: refund")], "end_turn"),
    ])
    backend = MockBackend()
    out = agent.run("refund 1001", "alice@example.com", backend)
    tools = [s["tool"] for s in out["steps"] if s["type"] == "tool_call"]
    assert tools == ["get_order_status", "issue_refund"]
    assert backend.refunds_issued[0]["amount"] == 29.99
    assert parse_decision(out["raw_output"])[0] == "refund"
    assert out["usage"] == {"input_tokens": 30, "output_tokens": 15}
    # tool results were sent back to the model
    last_msgs = agent.client.messages.calls[-1]["messages"]
    assert last_msgs[-2]["content"][0]["type"] == "tool_result"  # list is shared; [-1] is the final reply


def test_max_turns_is_recorded_as_error():
    agent = make_agent([([block_tool(f"t{i}", "search_policy", {"query": "x"})], "tool_use") for i in range(8)])
    out = agent.run("loop", "alice@example.com", MockBackend())
    assert out["stop_reason"] == "max_turns" and out["error"]
