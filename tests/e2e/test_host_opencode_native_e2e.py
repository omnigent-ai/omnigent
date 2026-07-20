"""End-to-end test for the ``opencode-native-ui`` built-in agent (full stack).

The runner-orchestration sibling of ``test_opencode_native_wire_contract_e2e.py``
(which drives ``OpenCodeNativeServer`` directly). This exercises the WHOLE
product path: list built-in agents -> find ``opencode-native-ui`` -> connect a
host daemon -> create a host-bound session -> the runner auto-creates the
``opencode serve`` + SSE forwarder + ``opencode attach`` terminal resource ->
send a user message -> poll session items until the assistant echoes a marker.

Opt-in (needs a pinned ``opencode`` binary + LLM credentials)::

    OMNIGENT_E2E_OPENCODE_NATIVE=1 \
    HOME=/tmp/omni-isolated DATABRICKS_CONFIG_FILE=$REAL_HOME/.databrickscfg \
    .venv/bin/python -m pytest tests/e2e/test_host_opencode_native_e2e.py \
        --profile ai-devtools-prod \
        --llm-api-key "$(databricks auth token -p ai-devtools-prod \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v

Running under an isolated ``$HOME`` keeps the runner-owned ``opencode serve``
bridge dirs (``~/.omnigent/opencode-native``) and the daemon registry off the
developer's real ones, so a co-resident daemon is never disturbed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

_OPENCODE_NATIVE_AGENT_NAME = "opencode-native-ui"

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE_NATIVE") != "1" or shutil.which("opencode") is None,
    reason=(
        "opencode-native host e2e needs a pinned `opencode` binary + LLM creds; "
        "set OMNIGENT_E2E_OPENCODE_NATIVE=1 (and pass --profile/--llm-api-key) to run"
    ),
)


def _spawn_host_daemon(*, tmp_path: Path, live_server: str) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent host`` daemon pointed at the test server."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _online_host_id(client: httpx.Client, timeout: float = 30.0) -> str:
    """Poll ``GET /v1/hosts`` until at least one host is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _poll_for_terminal(
    client: httpx.Client, *, session_id: str, resource_id: str, timeout: float
) -> None:
    """Poll resources until the runner registers the opencode terminal."""
    deadline = time.monotonic() + timeout
    last: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last = [r.get("id") for r in data]
            if any(r.get("id") == resource_id and r.get("type") == "terminal" for r in data):
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Terminal {resource_id!r} never appeared for {session_id} within {timeout}s; saw {last!r}"
    )


def test_opencode_native_multiturn_item_order(
    http_client: httpx.Client,
    tmp_path: Path,
    live_server: str,
) -> None:
    """DIAGNOSTIC: dump /items for a 3-turn conversation to inspect ordering.

    Reproduces the reported web-chat bug (assistant messages clustered, user
    messages out of order/missing). Sends 3 user turns through a real
    host-bound opencode session and prints every persisted item's
    role/position/response_id so we can see exactly how user vs assistant items
    land. Asserts strict user/assistant interleaving.
    """
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == _OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None
    workspace = tmp_path / "ws"
    workspace.mkdir()

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client)
        create = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
                # gateway-valid model via opencode's openai provider (the daemon
                # has OPENAI_BASE_URL/OPENAI_API_KEY pointed at the gateway).
                "model_override": "openai/databricks-claude-sonnet-4-6",
            },
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]
        _poll_for_terminal(
            http_client,
            session_id=session_id,
            resource_id=terminal_resource_id("opencode", "main"),
            timeout=90.0,
        )

        prompts = ["say ONE", "say TWO", "say THREE"]
        for i, prompt in enumerate(prompts):
            http_client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                },
                timeout=30.0,
            ).raise_for_status()
            # Wait until at least i+1 assistant message items exist.
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                data = (
                    http_client.get(
                        f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
                    )
                    .json()
                    .get("data", [])
                )
                n_asst = sum(
                    1
                    for it in data
                    if it.get("type") == "message" and it.get("role") == "assistant"
                )
                if n_asst >= i + 1:
                    break
                time.sleep(POLL_INTERVAL_S)

        data = (
            http_client.get(
                f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
            )
            .json()
            .get("data", [])
        )
        print("\n===ITEMS_DUMP_START===")
        for it in data:
            text = ""
            for blk in it.get("content", []) or []:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    text += blk["text"]
            print(
                f"pos={it.get('position')!s:>4} type={it.get('type')!s:18} "
                f"role={it.get('role')!s:10} rid={str(it.get('response_id'))[:24]:24} "
                f"text={text[:40]!r}"
            )
        print("===ITEMS_DUMP_END===\n")

        roles = [
            it.get("role")
            for it in data
            if it.get("type") == "message" and it.get("role") in ("user", "assistant")
        ]
        expected = ["user", "assistant", "user", "assistant", "user", "assistant"]
        assert roles == expected, f"messages not interleaved by turn: {roles}"
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()


def test_opencode_native_builtin_registered_at_startup(http_client: httpx.Client) -> None:
    """The server auto-registers ``opencode-native-ui`` as a built-in agent."""
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    names = {a["name"] for a in resp.json()["data"]}
    assert _OPENCODE_NATIVE_AGENT_NAME in names, (
        f"Expected {_OPENCODE_NATIVE_AGENT_NAME!r} in built-ins {names}; "
        "_ensure_default_opencode_agent did not run."
    )


def test_opencode_native_host_session_auto_creates_terminal(
    http_client: httpx.Client,
    tmp_path: Path,
    live_server: str,
) -> None:
    """A host-bound opencode-native session auto-creates the opencode terminal.

    Exercises the runner-orchestration path end-to-end: an online host daemon
    runs the session, and the runner's session-creation dispatch must call
    :func:`_auto_create_opencode_terminal` (boot ``opencode serve`` + SSE
    forwarder + ``opencode attach``) and register ``terminal_opencode_main`` as
    a streamable resource — so the Web UI has a terminal+chat view to embed,
    exactly as it does for claude/codex/pi/cursor.

    (The LLM turn itself is covered by ``test_opencode_native_wire_contract_e2e``
    and the standalone gateway round-trip; out-of-box turns through the built-in
    agent additionally need its default model gateway-wired — tracked
    separately.)
    """
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == _OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None, "opencode-native-ui agent not seeded"

    workspace = tmp_path / "ws"
    workspace.mkdir()

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client)
        create = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        # The runner's _auto_create_opencode_terminal must register the TUI on
        # session creation (the dispatch branch this PR adds alongside the other
        # natives) — otherwise the Web UI would have no terminal to attach to.
        terminal_id = terminal_resource_id("opencode", "main")
        _poll_for_terminal(
            http_client,
            session_id=session_id,
            resource_id=terminal_id,
            timeout=90.0,
        )
        # The `omnigent opencode` CLI launcher attaches this TTY directly to the
        # runner-owned tmux pane, so the terminal resource must expose the tmux
        # socket + target — assert that prerequisite is present.
        detail = http_client.get(f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}")
        detail.raise_for_status()
        meta = detail.json().get("metadata", {})
        assert meta.get("tmux_socket"), f"terminal has no tmux_socket: {meta}"
        assert meta.get("tmux_target"), f"terminal has no tmux_target: {meta}"
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
