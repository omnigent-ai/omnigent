"""Shared fixture: two configured generic ACP agents for override tests.

Both tests in this package drive the same precondition — two generic ACP
agents in the global ``acp:`` block, with Gemini listed *before* Goose — so a
slug-losing fallback (bare ``acp`` -> first configured agent) observably picks
the wrong one.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.onboarding.provider_config import _config_path

# Two configured ACP agents. Gemini is listed FIRST so a slug-losing fallback
# (bare ``acp`` -> first configured agent) launches Gemini, which is the bug.
GEMINI_NAME = "Fake Gemini"
GOOSE_NAME = "Fake Goose"
# Derived slugs (see omnigent.onboarding.acp_auth.slugify): lowercased,
# non-alphanumeric runs collapsed to ``-``.
GEMINI_SLUG = "fake-gemini"
GOOSE_SLUG = "fake-goose"

# The override the user picks: the SECOND (Goose) agent, by slug.
GOOSE_OVERRIDE = f"acp:{GOOSE_SLUG}"

# Distinctive reply text each fake agent streams, so the rendered chat reveals
# which ACP agent the runner actually launched.
GEMINI_REPLY = f"ACP agent reply from {GEMINI_NAME}"
GOOSE_REPLY = f"ACP agent reply from {GOOSE_NAME}"

# A minimal ACP agent speaking the Agent Client Protocol over stdio, mirroring
# the hermetic fake in tests/e2e_ui/files/test_files_tab_survives_acp_reply.py
# (initialize -> session/new -> session/prompt streams one deterministic
# agent_message_chunk and completes the turn). It echoes its own display name
# (argv[1]) in the reply so the transcript names the launched agent. Stdlib
# only, so any Python interpreter on the runner host can run it.
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
    """Configure Fake Gemini (first) + Fake Goose (second) in the global config.

    Writes the hermetic fake ACP agent script to disk and adds an ``acp:``
    block naming both agents to ``~/.omnigent/config.yaml`` (the path the
    server and runner read via :func:`omnigent.onboarding.acp_auth.acp_agents`).
    The original config (if any) is restored on teardown so the shared
    session-scoped server isn't polluted for other tests.

    :param tmp_path: Per-test dir for the fake agent script.
    """
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
    # Gemini FIRST, Goose SECOND — the ordering that makes a slug-losing
    # fallback launch the wrong (Gemini) agent.
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
