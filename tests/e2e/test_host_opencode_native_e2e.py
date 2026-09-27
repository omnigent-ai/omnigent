"""End-to-end tests for the ``opencode-native-ui`` built-in agent (full stack).

The runner-orchestration sibling of ``test_opencode_native_wire_contract_e2e.py``
(which drives ``OpenCodeNativeServer`` directly). This exercises the WHOLE
product path: list built-in agents -> find ``opencode-native-ui`` -> connect a
host daemon -> create a host-bound session -> the runner auto-creates the
``opencode serve --stdio`` + SSE forwarder + ``opencode --server`` TUI terminal
resource -> drive turns, approvals, model switches, compaction, interrupts,
forks and ``/clear`` through the server API.

Opt-in and run manually before merging opencode-native changes (needs
``@opencode/cli`` 2.0.x on PATH). OpenCode's free models need no credentials::

    npm install -g @opencode/cli@~2.0.18
    OMNIGENT_E2E_OPENCODE_NATIVE=1 OMNIGENT_E2E_OPENCODE_MODEL=opencode/big-pickle \
    uv run pytest tests/e2e/test_host_opencode_native_e2e.py -v

With gateway credentials instead::

    OMNIGENT_E2E_OPENCODE_NATIVE=1 \
    HOME=/tmp/omni-isolated DATABRICKS_CONFIG_FILE=$REAL_HOME/.databrickscfg \
    uv run pytest tests/e2e/test_host_opencode_native_e2e.py \
        --profile ai-devtools-prod \
        --llm-api-key "$(databricks auth token -p ai-devtools-prod \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v

Every test deletes the sessions it creates (which removes their
``~/.omnigent/opencode-native`` bridge dirs) and the module fixture stops the
host daemon, so a run under the real ``$HOME`` leaves nothing behind.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.opencode_native.bridge import (
    OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    opencode_db_path_for_bridge_dir,
    read_bridge_state,
)
from omnigent.native.native_coding_agents import OPENCODE_NATIVE_AGENT_NAME
from omnigent.onboarding.opencode_auth import opencode_db_path
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e._native_resume_helpers import (
    cli_env,
    omnigent_console_script,
    poll_for_pending_elicitation,
    resolve_elicitation,
)
from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE_NATIVE") != "1" or shutil.which("opencode") is None,
    reason=(
        "opencode-native host e2e needs `opencode` 2.x (npm @opencode/cli@~2.0.18) + LLM creds; "
        "set OMNIGENT_E2E_OPENCODE_NATIVE=1 (and pass --profile/--llm-api-key) to run"
    ),
)

# Default: a gateway-valid model via opencode's openai provider. Override with a
# free model such as opencode/big-pickle.
_MODEL = os.environ.get("OMNIGENT_E2E_OPENCODE_MODEL", "openai/databricks-claude-sonnet-4-6")
_TURN_TIMEOUT_S = 240.0
_TERMINAL_TIMEOUT_S = 90.0
_ASK_ON_OS_TOOLS = "omnigent.policies.builtins.safety.ask_on_os_tools"
_SHELL_PROMPT = "Run the shell command: ls -la and summarize the output."


def _spawn_host_daemon(*, log_dir: Path, live_server: str) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent host`` daemon pointed at the test server."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    daemon_log = log_dir / "host-daemon.log"
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


def _opencode_agent_id(client: httpx.Client) -> str:
    """Return the id of the seeded ``opencode-native-ui`` agent."""
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None, "opencode-native-ui agent not seeded"
    return str(agent_id)


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return every persisted item of *session_id* in position order."""
    resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 200, "order": "asc"})
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def _text(item: dict[str, Any]) -> str:
    """Concatenate an item's text blocks."""
    return "".join(
        blk["text"]
        for blk in item.get("content") or []
        if isinstance(blk, dict) and isinstance(blk.get("text"), str)
    )


def _assistant_texts(items: list[dict[str, Any]]) -> list[str]:
    """Texts of the assistant message items, in order."""
    return [
        _text(it) for it in items if it.get("type") == "message" and it.get("role") == "assistant"
    ]


def _transcript(items: list[dict[str, Any]]) -> str:
    """Render items compactly for assertion messages."""
    lines = []
    for it in items:
        body = _text(it) or str(it.get("output") or it.get("name") or "")
        lines.append(f"{it.get('type')}/{it.get('role') or '-'}: {body[:200]!r}")
    return "\n".join(lines)


def _session(client: httpx.Client, session_id: str) -> dict[str, Any]:
    """Return the ``GET /v1/sessions/{id}`` snapshot."""
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return dict(resp.json())


def _send(client: httpx.Client, session_id: str, text: str) -> None:
    """Post a user message event, as the web composer does."""
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=30.0,
    ).raise_for_status()


def _wait_for_idle_after(
    client: httpx.Client,
    session_id: str,
    *,
    min_assistant: int,
    timeout: float = _TURN_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Wait until *min_assistant* assistant items exist and the session stays idle."""
    deadline = time.monotonic() + timeout
    idle_since: float | None = None
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        items = _items(client, session_id)
        snap = _session(client, session_id)
        done = (
            len(_assistant_texts(items)) >= min_assistant
            and snap.get("status") == "idle"
            and not snap.get("pending_elicitations")
        )
        if not done:
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since >= 1.5:
            return items
        time.sleep(0.5)
    raise AssertionError(
        f"turn for {session_id} did not finish within {timeout}s "
        f"(wanted {min_assistant} assistant items):\n{_transcript(items)}"
    )


def _turn(client: httpx.Client, session_id: str, text: str) -> list[dict[str, Any]]:
    """Send *text* and return the items once its assistant reply has settled."""
    before = len(_assistant_texts(_items(client, session_id)))
    _send(client, session_id, text)
    return _wait_for_idle_after(client, session_id, min_assistant=before + 1)


def _bridge_dir(client: httpx.Client, session_id: str) -> Path:
    """Resolve a session's opencode bridge dir the way the runner does."""
    labels = _session(client, session_id).get("labels") or {}
    return bridge_dir_for_bridge_id(labels.get(OPENCODE_NATIVE_BRIDGE_ID_LABEL_KEY) or session_id)


def _opencode_session_id(client: httpx.Client, session_id: str) -> str:
    """Return the OpenCode session id recorded in the bridge state."""
    state = read_bridge_state(_bridge_dir(client, session_id))
    assert state is not None and state.opencode_session_id, f"no bridge state for {session_id}"
    return state.opencode_session_id


class _SseRecorder:
    """Record ``(event, data, monotonic_ts)`` from a session's live SSE stream."""

    def __init__(self, live_server: str, session_id: str) -> None:
        self.events: list[tuple[str, dict[str, Any], float]] = []
        self._live_server = live_server
        self._session_id = session_id
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> _SseRecorder:
        self._thread.start()
        # The stream's first frame is a ready-ack, so later events are not missed.
        assert self._connected.wait(timeout=30.0), "SSE stream never connected"
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        timeout = httpx.Timeout(10.0, read=2.0)
        with httpx.Client(base_url=self._live_server, timeout=timeout) as client:
            while not self._stop.is_set():
                try:
                    with client.stream("GET", f"/v1/sessions/{self._session_id}/stream") as resp:
                        self._consume(resp)
                except httpx.HTTPError:
                    continue

    def _consume(self, resp: httpx.Response) -> None:
        event_type = ""
        for line in resp.iter_lines():
            if self._stop.is_set():
                return
            if line.startswith("event:"):
                event_type = line[len("event:") :].strip()
                self._connected.set()
            elif line.startswith("data:"):
                try:
                    data = json.loads(line[len("data:") :].strip())
                except ValueError:
                    data = {}
                self.events.append(
                    (event_type, data if isinstance(data, dict) else {}, time.monotonic())
                )
                self._connected.set()

    def of_type(self, event_type: str) -> list[tuple[str, dict[str, Any], float]]:
        """Recorded events of *event_type*."""
        return [e for e in list(self.events) if e[0] == event_type]

    def wait_for(self, event_type: str, *, count: int = 1, timeout: float) -> None:
        """Block until *count* events of *event_type* have been recorded."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.of_type(event_type)) >= count:
                return
            time.sleep(POLL_INTERVAL_S)
        seen = sorted({e[0] for e in list(self.events)})
        raise AssertionError(f"never saw {count}x {event_type!r} within {timeout}s; saw {seen}")


@dataclass
class _OpenCodeHost:
    """A live host daemon plus the sessions a test created on it."""

    client: httpx.Client
    live_server: str
    host_id: str
    agent_id: str
    root: Path
    created: list[str] = field(default_factory=list)

    def workspace(self, name: str) -> Path:
        """Create a fresh workspace dir with a known marker file."""
        path = self.root / f"{name}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True)
        (path / "omnigent_marker.txt").write_text("marker\n")
        return path

    def create_session(self, workspace: Path, *, model: str = _MODEL) -> str:
        """Create a host-bound session and wait for its OpenCode terminal."""
        create = self.client.post(
            "/v1/sessions",
            json={
                "agent_id": self.agent_id,
                "host_id": self.host_id,
                "workspace": str(workspace),
                "model_override": model,
            },
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = str(create.json()["id"])
        self.created.append(session_id)
        _poll_for_terminal(
            self.client,
            session_id=session_id,
            resource_id=terminal_resource_id("opencode", "main"),
            timeout=_TERMINAL_TIMEOUT_S,
        )
        return session_id

    def cleanup(self) -> None:
        """Delete every tracked session while the runner can still reap its bridge dir."""
        for session_id in reversed(self.created):
            bridge = _bridge_dir(self.client, session_id) if self._exists(session_id) else None
            self.client.delete(f"/v1/sessions/{session_id}", timeout=60.0)
            if bridge is not None:
                _wait_gone(bridge, timeout=20.0)
        self.created.clear()

    def _exists(self, session_id: str) -> bool:
        return self.client.get(f"/v1/sessions/{session_id}").status_code == 200


def _wait_gone(path: Path, *, timeout: float) -> None:
    """Wait for the runner to remove *path*; remove it ourselves as a fallback."""
    deadline = time.monotonic() + timeout
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def opencode_daemon(
    http_client: httpx.Client, live_server: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[tuple[str, Path]]:
    """One host daemon shared by the module's live tests; stopped at module end."""
    log_dir = tmp_path_factory.mktemp("opencode-host")
    daemon = _spawn_host_daemon(log_dir=log_dir, live_server=live_server)
    try:
        yield _online_host_id(http_client), log_dir
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=15)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)


@pytest.fixture
def opencode_host(
    http_client: httpx.Client,
    live_server: str,
    opencode_daemon: tuple[str, Path],
    tmp_path: Path,
) -> Iterator[_OpenCodeHost]:
    """Per-test handle that deletes the sessions the test created."""
    host = _OpenCodeHost(
        client=http_client,
        live_server=live_server,
        host_id=opencode_daemon[0],
        agent_id=_opencode_agent_id(http_client),
        root=tmp_path,
    )
    try:
        yield host
    finally:
        host.cleanup()


def _attach_ask_on_os_tools(client: httpx.Client, session_id: str) -> None:
    """Attach the built-in policy that ASKs before every file/shell tool call."""
    resp = client.post(
        f"/v1/sessions/{session_id}/policies",
        json={"name": "ask_on_os_tools", "type": "python", "handler": _ASK_ON_OS_TOOLS},
    )
    assert resp.status_code == 200, f"policy attach failed: {resp.status_code} {resp.text[:300]}"


def _shell_elicitation(host: _OpenCodeHost, session_id: str) -> dict[str, object]:
    """Ask for ``ls -la`` until a ``shell`` approval card appears (two attempts)."""
    for _attempt in range(2):
        before = len(_assistant_texts(_items(host.client, session_id)))
        _send(host.client, session_id, _SHELL_PROMPT)
        deadline = time.monotonic() + _TURN_TIMEOUT_S
        while time.monotonic() < deadline:
            snap = _session(host.client, session_id)
            pending = snap.get("pending_elicitations") or []
            if pending:
                return poll_for_pending_elicitation(
                    host.client, conversation_id=session_id, timeout=5.0
                )
            finished = (
                snap.get("status") == "idle"
                and len(_assistant_texts(_items(host.client, session_id))) > before
            )
            if finished:
                break
            time.sleep(0.5)
    items = _items(host.client, session_id)
    raise AssertionError(
        f"model never requested the shell tool after two attempts:\n{_transcript(items)}"
    )


def _outputs(items: list[dict[str, Any]]) -> list[str]:
    """Outputs of every function_call_output item."""
    return [
        str(it.get("output") or "") for it in items if it.get("type") == "function_call_output"
    ]


# ── Original smoke tests ─────────────────────────────────────────────────────


def test_opencode_native_multiturn_item_order(opencode_host: _OpenCodeHost) -> None:
    """Three user turns persist strictly interleaved user/assistant messages."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("order"))
    for i, prompt in enumerate(["say ONE", "say TWO", "say THREE"]):
        _send(client, session_id, prompt)
        _wait_for_idle_after(client, session_id, min_assistant=i + 1)

    data = _items(client, session_id)
    roles = [
        it.get("role")
        for it in data
        if it.get("type") == "message" and it.get("role") in ("user", "assistant")
    ]
    expected = ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert roles == expected, f"messages not interleaved by turn:\n{_transcript(data)}"


def test_opencode_native_builtin_registered_at_startup(http_client: httpx.Client) -> None:
    """The server auto-registers ``opencode-native-ui`` as a built-in agent."""
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    names = {a["name"] for a in resp.json()["data"]}
    assert OPENCODE_NATIVE_AGENT_NAME in names, (
        f"Expected {OPENCODE_NATIVE_AGENT_NAME!r} in built-ins {names}; "
        "_ensure_default_native_agents did not run."
    )


def test_opencode_native_host_session_auto_creates_terminal(
    opencode_host: _OpenCodeHost,
) -> None:
    """A host-bound session registers the tmux-backed ``terminal_opencode_main``.

    The ``omnigent opencode`` CLI launcher attaches its TTY to the runner-owned
    tmux pane, so the resource must expose the socket and target.
    """
    session_id = opencode_host.create_session(opencode_host.workspace("terminal"))
    terminal_id = terminal_resource_id("opencode", "main")
    detail = opencode_host.client.get(
        f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}"
    )
    detail.raise_for_status()
    meta = detail.json().get("metadata", {})
    assert meta.get("tmux_socket"), f"terminal has no tmux_socket: {meta}"
    assert meta.get("tmux_target"), f"terminal has no tmux_target: {meta}"


# ── Live product-path coverage ───────────────────────────────────────────────


def test_opencode_native_assistant_text_streams(opencode_host: _OpenCodeHost) -> None:
    """Assistant text reaches the web stream as several deltas before the final item."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("stream"))
    with _SseRecorder(opencode_host.live_server, session_id) as sse:
        items = _turn(client, session_id, "Write three short sentences about rivers.")
        deltas = sse.of_type("response.output_text.delta")

    final = _assistant_texts(items)[-1]
    assert len(deltas) > 1, f"expected several text deltas, got {len(deltas)}: {deltas!r}"
    streamed = "".join(str(d[1].get("delta") or "") for d in deltas)
    head = streamed.strip()[:20]
    assert head and head in final, f"deltas {streamed!r} do not match the final text {final!r}"
    assert deltas[-1][2] > deltas[0][2], "deltas all arrived at once"


def test_opencode_native_shell_approval_approve(opencode_host: _OpenCodeHost) -> None:
    """An ASK policy parks a web card for ``shell``; approving runs ``ls -la``."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("approve"))
    _attach_ask_on_os_tools(client, session_id)

    elicitation = _shell_elicitation(opencode_host, session_id)
    resolve_elicitation(
        client,
        conversation_id=session_id,
        elicitation_id=str(elicitation["elicitation_id"]),
        action="accept",
    )
    items = _wait_for_idle_after(client, session_id, min_assistant=1)
    # The model may answer later cards too; approve them so the turn can finish.
    while _session(client, session_id).get("pending_elicitations"):
        extra = poll_for_pending_elicitation(client, conversation_id=session_id, timeout=5.0)
        resolve_elicitation(
            client, conversation_id=session_id, elicitation_id=str(extra["elicitation_id"])
        )
        items = _wait_for_idle_after(client, session_id, min_assistant=1)

    assert any("omnigent_marker.txt" in out for out in _outputs(items)), (
        f"no tool output listing the workspace:\n{_transcript(items)}"
    )
    last_output = max(i for i, it in enumerate(items) if it.get("type") == "function_call_output")
    assert any(
        it.get("type") == "message" and it.get("role") == "assistant" and _text(it).strip()
        for it in items[last_output + 1 :]
    ), f"no assistant summary after the tool output:\n{_transcript(items)}"


def test_opencode_native_shell_approval_deny(opencode_host: _OpenCodeHost) -> None:
    """Declining the ``shell`` card rejects the call: no listing, the turn still ends."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("deny"))
    _attach_ask_on_os_tools(client, session_id)

    elicitation = _shell_elicitation(opencode_host, session_id)
    resolve_elicitation(
        client,
        conversation_id=session_id,
        elicitation_id=str(elicitation["elicitation_id"]),
        action="decline",
    )
    # A persistent model may retry the tool; decline every retry.
    deadline = time.monotonic() + _TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        snap = _session(client, session_id)
        pending = snap.get("pending_elicitations") or []
        if pending:
            resolve_elicitation(
                client,
                conversation_id=session_id,
                elicitation_id=str(pending[0]["elicitation_id"]),
                action="decline",
            )
        elif snap.get("status") == "idle":
            break
        time.sleep(0.5)
    assert _session(client, session_id)["status"] == "idle", "turn never ended after Deny"
    items = _items(client, session_id)
    outputs = _outputs(items)
    # A reject without a message ends the OpenCode turn; the call reports an error.
    assert outputs and all(out.startswith("[error]") for out in outputs), _transcript(items)
    assert not any("omnigent_marker.txt" in out for out in outputs), _transcript(items)

    after = _turn(client, session_id, "Reply with the single word PONG.")
    assert "PONG" in _assistant_texts(after)[-1].upper(), _transcript(after)


def _second_free_model(client: httpx.Client, session_id: str, current: str) -> str | None:
    """Pick another ``opencode/*`` model from the live catalog, preferring free ones."""
    deadline = time.monotonic() + 60.0
    ids: list[str] = []
    while time.monotonic() < deadline and not ids:
        options = _session(client, session_id).get("model_options") or []
        ids = [str(o.get("id")) for o in options if isinstance(o, dict) and o.get("id")]
        if not ids:
            time.sleep(1.0)
    candidates = [m for m in ids if m.startswith("opencode/") and m != current]
    free = [m for m in candidates if "free" in m or m.endswith("big-pickle")]
    return (free or candidates or [None])[0]


def test_opencode_native_model_switch(opencode_host: _OpenCodeHost) -> None:
    """A web model switch reaches OpenCode: the next turn reports the new model."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("model"))
    target = _second_free_model(client, session_id, _MODEL)
    if target is None:
        pytest.skip(f"live catalog lists no second opencode/* model besides {_MODEL}")

    resp = client.patch(f"/v1/sessions/{session_id}", json={"model_override": target})
    assert resp.status_code == 200, resp.text[:300]
    _turn(client, session_id, "Reply with the single word OK.")

    deadline = time.monotonic() + 30.0
    snap = _session(client, session_id)
    while time.monotonic() < deadline and not str(snap.get("llm_model") or "").endswith(
        target.split("/", 1)[1]
    ):
        time.sleep(0.5)
        snap = _session(client, session_id)
    assert snap.get("model_override") == target, snap.get("model_override")
    assert str(snap.get("llm_model") or "").endswith(target.split("/", 1)[1]), (
        f"model badge shows {snap.get('llm_model')!r}, expected {target!r}"
    )
    state = read_bridge_state(_bridge_dir(client, session_id))
    assert state is not None and state.last_applied_model == target, state


def test_opencode_native_compact(opencode_host: _OpenCodeHost) -> None:
    """``/compact`` compacts the OpenCode session and later turns keep context."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("compact"))
    _turn(client, session_id, "Remember the codeword PELICAN-42. Reply with just OK.")
    _turn(client, session_id, "What is 2 + 2? Reply with just the number.")

    with _SseRecorder(opencode_host.live_server, session_id) as sse:
        resp = client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "compact"}, timeout=90.0
        )
        assert resp.status_code in (200, 202), resp.text[:300]
        sse.wait_for("response.compaction.completed", timeout=_TURN_TIMEOUT_S)
        assert not sse.of_type("response.compaction.failed")
    _wait_for_idle_after(client, session_id, min_assistant=2)

    items = _turn(client, session_id, "What was the codeword? Reply with just the codeword.")
    assert "PELICAN" in _assistant_texts(items)[-1].upper(), _transcript(items)


def test_opencode_native_interrupt(opencode_host: _OpenCodeHost) -> None:
    """Stop mid-stream ends the turn (session idle) and the next prompt works."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("interrupt"))
    with _SseRecorder(opencode_host.live_server, session_id) as sse:
        _send(client, session_id, "Count from 1 to 400, one number per line.")
        sse.wait_for("response.output_text.delta", count=3, timeout=_TURN_TIMEOUT_S)
        client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "interrupt"}, timeout=30.0
        ).raise_for_status()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and _session(client, session_id)["status"] != "idle":
            time.sleep(0.5)
        assert _session(client, session_id)["status"] == "idle", "turn kept running after Stop"
        streamed = "".join(
            str(d[1].get("delta") or "") for d in sse.of_type("response.output_text.delta")
        )
    assert "400" not in streamed.split(), "the count finished; the interrupt landed too late"

    items = _turn(client, session_id, "Reply with the single word PONG.")
    assert "PONG" in _assistant_texts(items)[-1].upper(), _transcript(items)


def _mentions_topic(text: str) -> bool:
    return "mangosteen" in text.lower()


def test_opencode_native_fork_clones_session(opencode_host: _OpenCodeHost) -> None:
    """Forking into the same workspace natively forks the source OpenCode session."""
    client = opencode_host.client
    workspace = opencode_host.workspace("fork")
    source_id = opencode_host.create_session(workspace)
    _turn(client, source_id, "My favorite fruit is the mangosteen. Reply with just OK.")
    source_ses = _opencode_session_id(client, source_id)

    fork = client.post(f"/v1/sessions/{source_id}/fork", json={}, timeout=60.0)
    assert fork.status_code == 201, fork.text[:300]
    clone_id = str(fork.json()["id"])
    opencode_host.created.append(clone_id)
    launch = client.post(
        f"/v1/hosts/{opencode_host.host_id}/runners",
        json={"session_id": clone_id, "workspace": str(workspace)},
        timeout=60.0,
    )
    assert launch.status_code < 300, launch.text[:300]
    _poll_for_terminal(
        client,
        session_id=clone_id,
        resource_id=terminal_resource_id("opencode", "main"),
        timeout=_TERMINAL_TIMEOUT_S,
    )

    clone_ses = _opencode_session_id(client, clone_id)
    assert clone_ses != source_ses, "clone reuses the source OpenCode session"
    db = opencode_db_path_for_bridge_dir(_bridge_dir(client, clone_id))
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT id FROM session_v2").fetchall()
    assert (source_ses,) in rows, f"clone DB lacks source session {source_ses}: {rows}"

    before = len(_assistant_texts(_items(client, clone_id)))
    _send(client, clone_id, "Summarize our conversation so far.")
    items = _wait_for_idle_after(client, clone_id, min_assistant=before + 1)
    assert _mentions_topic(_assistant_texts(items)[-1]), _transcript(items)


@pytest.mark.xfail(
    strict=True,
    reason="the server /events route rejects type 'clear', so the runner's "
    "opencode-native clear handler is unreachable over HTTP",
)
def test_opencode_native_clear_starts_fresh_session(opencode_host: _OpenCodeHost) -> None:
    """``/clear`` relaunches OpenCode on a new, empty session."""
    client = opencode_host.client
    session_id = opencode_host.create_session(opencode_host.workspace("clear"))
    _turn(client, session_id, "My favorite fruit is the mangosteen. Reply with just OK.")
    old_ses = _opencode_session_id(client, session_id)

    resp = client.post(f"/v1/sessions/{session_id}/events", json={"type": "clear"}, timeout=90.0)
    assert resp.status_code < 300, f"clear rejected: {resp.status_code} {resp.text[:300]}"
    deadline = time.monotonic() + _TERMINAL_TIMEOUT_S
    new_ses = old_ses
    while time.monotonic() < deadline and new_ses == old_ses:
        time.sleep(0.5)
        state = read_bridge_state(_bridge_dir(client, session_id))
        new_ses = state.opencode_session_id if state is not None else old_ses
    assert new_ses != old_ses, "clear did not start a new OpenCode session"

    items = _turn(client, session_id, "Summarize our conversation so far.")
    assert not _mentions_topic(_assistant_texts(items)[-1]), _transcript(items)


def _user_opencode_has_sessions() -> bool:
    """Whether the user's own OpenCode store exists and holds a root session."""
    db = opencode_db_path()
    if db is None or not db.is_file():
        return False
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
            table = "session_v2" if "session_v2" in tables else "session"
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def _session_ids(client: httpx.Client) -> set[str]:
    """Ids of every session visible on the test server."""
    resp = client.get("/v1/sessions", params={"visibility": "all", "limit": 100})
    resp.raise_for_status()
    return {str(s["id"]) for s in resp.json().get("data", [])}


def test_opencode_native_import_last_session(http_client: httpx.Client, live_server: str) -> None:
    """``omnigent import --harness opencode --last 1`` imports from the user's store.

    Import snapshots the user's ``opencode.db`` before reading it, so the store
    is never modified.
    """
    if not _user_opencode_has_sessions():
        pytest.skip("no OpenCode sessions in the user's own store to import")
    before = _session_ids(http_client)
    result = subprocess.run(
        [
            str(omnigent_console_script()),
            "import",
            "--harness",
            "opencode",
            "--last",
            "1",
            "--server",
            live_server,
        ],
        env=cli_env(),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + result.stderr
    after = _session_ids(http_client)
    imported = sorted(after - before)
    try:
        if "already imported" in output.lower() and not imported:
            pytest.skip(f"newest OpenCode session was already imported: {output[-300:]}")
        assert result.returncode == 0, output[-2000:]
        assert len(imported) == 1, f"expected one imported session, got {imported}: {output}"
        items = _items(http_client, imported[0])
        assert any(it.get("type") == "message" for it in items), _transcript(items)
    finally:
        for session_id in imported:
            http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
    assert not re.search(r"Traceback", output), output[-2000:]
