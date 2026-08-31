"""Cross-harness ``harness_override`` must not be ignored on ``initial_items``.

User journey (from the ticket's Steps to reproduce):

1. An agent pinned to the ``claude-sdk`` harness exists.
2. ``POST /v1/sessions`` with ``harness_override="codex-native"`` AND one
   ``initial_items`` user message (the kickoff).
3. A runner binds (the external-host launch analog: ``PATCH`` ``runner_id``)
   and the automatic kickoff turn fires.
4. Observed failure: the kickoff executes on the agent spec's **claude-sdk**
   brain, not the overridden **codex-native** harness. A second, manually
   posted message *does* take the codex-native path — so only the
   ``initial_items`` kickoff ignores the override.

The test boots a real ``omnigent server`` + external runner + the mock LLM
(``tests/server/integration/mock_llm_server.py``) as subprocesses, drives the
journey over HTTP, and asserts the kickoff never reaches the spec's claude
brain. While the bug is live this fails: the mock records Anthropic-model
requests for the kickoff and the assistant answer carries the claude
fallback marker.

Run directly (excluded from the default run like the rest of ``tests/e2e``)::

    pytest tests/e2e/test_harness_override_initial_items_kickoff.py -x -q
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.identity import token_bound_runner_id

REPO = Path(__file__).resolve().parents[2]

CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MARKER = "KICKOFF-ANSWERED-BY-SPEC-CLAUDE-SDK-BRAIN"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[dict[str, Any]]:
    """
    Boot mock LLM + omnigent server + external runner subprocesses.

    Yields ``{"base_url", "mock_url", "runner_id"}`` once the server is
    healthy and the runner reports online. Tears all three down.
    """
    # Loopback must bypass any mandatory egress proxy in CI.
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = "127.0.0.1,localhost," + os.environ.get(var, "")

    mock_port, server_port = _free_port(), _free_port()
    mock_url = f"http://127.0.0.1:{mock_port}"
    base_url = f"http://127.0.0.1:{server_port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    procs: list[subprocess.Popen[bytes]] = []
    logs = tmp_path / "logs"
    logs.mkdir()

    try:
        mock_log = open(logs / "mock.log", "w")  # noqa: SIM115 — lives for Popen's lifetime
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(REPO / "tests/server/integration/mock_llm_server.py"),
                    str(mock_port),
                ],
                env={**os.environ, "PYTHONPATH": str(REPO)},
                stdout=mock_log,
                stderr=subprocess.STDOUT,
            )
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{mock_url}/stats", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.fail("mock LLM did not boot")

        # Distinct marker for any request that reaches the claude-sdk brain.
        httpx.post(
            f"{mock_url}/mock/set_fallback",
            json={"key": CLAUDE_MODEL, "text": CLAUDE_MARKER},
            timeout=5,
        )
        httpx.post(
            f"{mock_url}/mock/set_fallback",
            json={"key": "default", "text": "DEFAULT-QUEUE-ANSWER"},
            timeout=5,
        )

        # Agent pinned to the claude-sdk harness, brain pointed at the mock.
        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        spec = {
            "name": "sdk-chat-kickoff",
            "prompt": "You are a concise assistant.",
            "executor": {
                "harness": "claude-sdk",
                "model": CLAUDE_MODEL,
                "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_url},
            },
        }
        (agent_dir / "sdk-chat-kickoff.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))

        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(REPO), str(REPO / "sdks" / "python-client"), str(REPO / "sdks" / "ui")]
            ),
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{mock_url}/v1",
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
            "OMNIGENT_BUILTIN_AGENT_DIRS": str(agent_dir / "sdk-chat-kickoff.yaml"),
            "ANTHROPIC_API_KEY": "",
        }
        env.pop("CLAUDECODE", None)

        server_log = open(logs / "server.log", "w")  # noqa: SIM115 — lives for Popen's lifetime
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnigent.cli",
                    "server",
                    "--port",
                    str(server_port),
                    "--database-uri",
                    f"sqlite:///{tmp_path / 'test.db'}",
                    "--artifact-location",
                    str(tmp_path / "artifacts"),
                ],
                env=env,
                cwd=str(REPO),
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
        )

        runner_log = open(logs / "runner.log", "w")  # noqa: SIM115 — lives for Popen's lifetime
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "omnigent.runner._entry"],
                env={
                    **env,
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                },
                cwd=str(REPO),
                stdout=runner_log,
                stderr=subprocess.STDOUT,
            )
        )

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                h = httpx.get(f"{base_url}/health", timeout=2)
                s = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if h.status_code == 200 and s.status_code == 200 and s.json().get("online"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            pytest.fail("server/runner did not come online")

        yield {"base_url": base_url, "mock_url": mock_url, "runner_id": runner_id}
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


def _claude_requests(mock_url: str) -> list[dict[str, Any]]:
    """Requests the mock LLM served for the spec's claude model."""
    reqs = httpx.get(f"{mock_url}/mock/requests", timeout=5).json()["requests"]
    return [
        r for r in reqs if isinstance(r, dict) and str(r.get("model", "")).startswith("claude")
    ]


def test_initial_items_kickoff_honors_cross_harness_override(
    rig: dict[str, Any],
) -> None:
    """
    The ``initial_items`` kickoff must NOT execute on the agent spec's
    claude-sdk brain when the session was created with
    ``harness_override="codex-native"``.

    While the bug is live, the kickoff turn runs on the spec harness:
    the mock records claude-model requests and the assistant answer is
    the claude fallback marker. Post-fix, the kickoff takes the
    codex-native path (like the second manual message already does) and
    zero claude requests are recorded.
    """
    base, mock = rig["base_url"], rig["mock_url"]
    c = httpx.Client(base_url=base, timeout=30)

    agents = c.get("/v1/agents", params={"limit": 100}).json()["data"]
    agent_id = next(a["id"] for a in agents if a["name"] == "sdk-chat-kickoff")

    # Step 2 of the journey: create with cross-harness override + kickoff.
    create = c.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "harness_override": "codex-native",
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "kickoff: which model are you?"}
                        ],
                    },
                }
            ],
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    sid = body["id"]
    # Sanity: the override is recorded on the session snapshot.
    assert body.get("harness") == "codex-native"

    # Step 3: the runner binds (external-host launch analog) and the
    # automatic kickoff turn fires.
    patch = c.patch(f"/v1/sessions/{sid}", json={"runner_id": rig["runner_id"]})
    assert patch.status_code == 200, patch.text

    # Let the kickoff turn settle: assistant reply, error item, or timeout.
    deadline = time.monotonic() + 90
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        snap = c.get(f"/v1/sessions/{sid}").json()
        items = snap.get("items", [])
        settled = any(
            (i.get("type") == "message" and i.get("data", {}).get("role") == "assistant")
            or i.get("type") == "error"
            for i in items
        )
        if settled:
            break
        time.sleep(2)

    claude_hits = _claude_requests(mock)
    assistant_texts = [
        " ".join(
            str(block.get("text", ""))
            for block in i.get("data", {}).get("content", [])
            if isinstance(block, dict)
        )
        for i in items
        if i.get("type") == "message" and i.get("data", {}).get("role") == "assistant"
    ]

    # THE bug: the kickoff must not have executed on the spec's
    # claude-sdk brain. While the bug is live this fails with the
    # kickoff answered by the claude brain.
    assert not claude_hits, (
        f"initial_items kickoff executed on the spec claude-sdk brain despite "
        f"harness_override=codex-native: {len(claude_hits)} claude request(s) "
        f"hit the mock LLM; assistant said {assistant_texts!r}"
    )
    assert all(CLAUDE_MARKER not in t for t in assistant_texts), (
        f"kickoff assistant reply came from the claude-sdk brain: {assistant_texts!r}"
    )
