"""Fixtures for namespaced ACP harness overrides."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.onboarding.provider_config import _config_path

GEMINI_NAME = "Fake Gemini"
GOOSE_NAME = "Fake Goose"
GEMINI_SLUG = "fake-gemini"
GOOSE_SLUG = "fake-goose"
GOOSE_OVERRIDE = f"acp:{GOOSE_SLUG}"
GEMINI_REPLY = f"ACP agent reply from {GEMINI_NAME}"
GOOSE_REPLY = f"ACP agent reply from {GOOSE_NAME}"

# Minimal stdio ACP agent that identifies itself in its reply.
_FAKE_ACP_AGENT = r"""
import sys, json

name = sys.argv[1] if len(sys.argv) > 1 else "ACP agent"

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "fake-acp-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": {
                  "sessionUpdate": "agent_message_chunk",
                  "content": {"type": "text",
                              "text": "ACP agent reply from " + name}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }})
"""


@pytest.fixture
def two_acp_agents_config(tmp_path: Path) -> Iterator[None]:
    """Configure two agents and restore the original config after the test."""
    import yaml

    agent_script = tmp_path / "fake_acp_agent.py"
    agent_script.write_text(_FAKE_ACP_AGENT)

    def _command(display: str) -> str:
        return shlex.join([sys.executable, str(agent_script), display])

    config_path = Path(_config_path())
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = config_path.read_text() if config_path.exists() else None

    config: dict[str, object] = {}
    if original:
        loaded = yaml.safe_load(original)
        if isinstance(loaded, dict):
            config = loaded
    config["acp"] = {
        "agents": [
            {"name": GEMINI_NAME, "command": _command(GEMINI_NAME)},
            {"name": GOOSE_NAME, "command": _command(GOOSE_NAME)},
        ]
    }
    config_path.write_text(yaml.safe_dump(config))
    try:
        yield
    finally:
        if original is not None:
            config_path.write_text(original)
        else:
            config_path.unlink(missing_ok=True)
