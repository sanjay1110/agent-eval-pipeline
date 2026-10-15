"""Agent under test plus its mock tool backend."""
import os


def get_agent(mode: str = "auto"):
    """mode: 'live' (Claude API), 'stub' (offline deterministic), or 'auto'."""
    if mode == "auto":
        mode = "live" if os.environ.get("ANTHROPIC_API_KEY") else "stub"
    if mode == "live":
        from agent.claude_agent import ClaudeSupportAgent
        return ClaudeSupportAgent()
    if mode == "stub":
        from agent.stub_agent import StubAgent
        return StubAgent()
    raise ValueError(f"unknown agent mode: {mode}")
