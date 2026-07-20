"""End-to-end test for the codex-native-ui built-in agent.

Verifies the host-spawned Web UI flow for ``codex-native-ui``: list
the built-in agents -> find ``codex-native-ui`` -> connect a host
daemon -> create a session bound to that agent and host -> send a
user message -> poll until an assistant response appears containing a
marker token.

Run with Databricks credentials (opt-in via env var)::

    OMNIGENT_E2E_CODEX_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_host_codex_native_e2e.py \
        --profile oss \
        --llm-api-key "$(databricks auth token -p oss \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

_CODEX_NATIVE_AGENT_NAME = "codex-native-ui"

# Marker file the cwd-resolution tests ask Codex to read back. It is placed
# only in the session's intended cwd (worktree / picked workspace) and never
# in the runner's spec-bundle dir, so a correct read proves Codex launched in
# that directory.
_CWD_MARKER_FILE = "CWD_MARKER.txt"

# Checked-in test image: a 100x100 red square with a blue center. Reused by
# the image-routing regression test so the model has a deterministic color
# to name back.
_TEST_IMAGE_PATH = Path(__file__).resolve().parents[2] / "tests" / "resources" / "test_image.png"


def _spawn_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
) -> subprocess.Popen[bytes]:
    """
    Spawn an ``omnigent host`` daemon for the codex-native test.

    :param tmp_path: Per-test temp dir for the daemon log.
    :param live_server: Test server URL.
    :returns: The spawned daemon subprocess handle.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            # Compat-aware: pinned OLD host venv in runner compat mode (Config 2).
            # apply_runner_env drops this test's repo_root PYTHONPATH in that mode
            # so the old host build resolves; no-op in normal runs.
            [
                runner_executable(),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _online_host_id(client: httpx.Client, timeout: float = 30.0) -> str:
    """
    Poll ``GET /v1/hosts`` until at least one host is online.

    :param client: HTTP client pointed at the test server.
    :param timeout: Max seconds to wait.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _codex_native_agent_id(client: httpx.Client) -> str:
    """
    Return the durable id of the auto-registered ``codex-native-ui``.

    :param client: HTTP client pointed at the test server.
    :returns: The ``"ag_..."`` id for ``codex-native-ui``.
    :raises AssertionError: If the server did not auto-register it.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == _CODEX_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(
        f"{_CODEX_NATIVE_AGENT_NAME!r} not registered on the server "
        "(expected from _ensure_default_agents at startup)"
    )


def _assistant_text(item: dict[str, object]) -> str:
    """
    Extract concatenated assistant text from a session item.

    :param item: One element from session items data.
    :returns: Joined text of all assistant text blocks, or ``""``.
    """
    if item.get("role") != "assistant":
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _ordered_message_items(
    client: httpx.Client,
    *,
    session_id: str,
) -> list[dict[str, object]]:
    """
    Return the session's user/assistant message items in ascending order.

    Filters ``GET /v1/sessions/{id}/items`` to ``type == "message"`` items
    with a ``user`` / ``assistant`` role, preserving the server's position
    order — which is exactly the order the web UI renders bubbles in.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :returns: Ordered message items, each a raw items-API dict with at
        least ``"role"`` and ``"content"`` keys.
    """
    resp = client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 50, "order": "asc"},
    )
    resp.raise_for_status()
    return [
        item
        for item in resp.json().get("data", [])
        if item.get("type") == "message" and item.get("role") in ("user", "assistant")
    ]


def _user_text(item: dict[str, object]) -> str:
    """
    Extract concatenated user text from a session item.

    :param item: One element from session items data.
    :returns: Joined text of all user text blocks, or ``""``.
    """
    if item.get("role") != "user":
        return ""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _content_types(item: dict[str, object]) -> list[object]:
    """
    Return the ``type`` of each content block in a message item.

    :param item: One element from session items data.
    :returns: List of block ``type`` values, e.g.
        ``["input_image", "input_text"]``; empty when content is absent.
    """
    content = item.get("content")
    if not isinstance(content, list):
        return []
    return [block.get("type") for block in content if isinstance(block, dict)]


def _send_user_text(
    client: httpx.Client,
    *,
    session_id: str,
    text: str,
) -> None:
    """
    Post a user text message to a session (the web "send" action).

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param text: User message text to deliver to the agent.
    :returns: None.
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        timeout=30.0,
    )
    resp.raise_for_status()


def _poll_for_assistant_marker(
    client: httpx.Client,
    *,
    session_id: str,
    marker: str,
    timeout: float,
) -> str:
    """
    Poll session items until an assistant message contains *marker*.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param marker: Literal string the agent was asked to echo.
    :param timeout: Max seconds to wait.
    :returns: The matching assistant message text.
    :raises AssertionError: If no match within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(
            f"/v1/sessions/{session_id}/items",
            params={"limit": 50, "order": "asc"},
        )
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                text = _assistant_text(item)
                if marker in text:
                    return text
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"No assistant message containing {marker!r} within {timeout}s — "
        "the codex-native message was not answered."
    )


def _poll_for_assistant_reply(
    client: httpx.Client,
    *,
    session_id: str,
    timeout: float,
) -> str:
    """
    Poll session items until any non-empty assistant message appears.

    A liveness gate: it confirms the turn ran to a reply (so the image
    attachment did not crash the turn) and that the forwarder has mirrored
    the user message into the durable items, which the caller then
    inspects. It deliberately does NOT assert on the reply's content —
    naming the test image's color does not distinguish a natively-delivered
    image from a base64-serialized one (the model answers in both cases),
    so the freeze regression is caught by the user-text check at the call
    site, not here.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param timeout: Max seconds to wait.
    :returns: The first non-empty assistant message text.
    :raises AssertionError: If no assistant reply appears within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(
            f"/v1/sessions/{session_id}/items",
            params={"limit": 50, "order": "asc"},
        )
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                text = _assistant_text(item)
                if text:
                    return text
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"No assistant reply within {timeout}s — the image-bearing turn "
        "never completed (the attachment may have crashed the turn)."
    )


def _poll_for_terminal_resource(
    client: httpx.Client,
    *,
    session_id: str,
    resource_id: str,
    timeout: float,
) -> dict[str, object]:
    """
    Poll ``GET /v1/sessions/{id}/resources`` until *resource_id* appears.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param resource_id: Expected terminal resource id, e.g.
        ``"terminal_codex_main"``.
    :param timeout: Max seconds to wait.
    :returns: The matching terminal resource object.
    :raises AssertionError: If the resource never appears within *timeout*.
    """
    deadline = time.monotonic() + timeout
    last_seen: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last_seen = [r.get("id") for r in data]
            for resource in data:
                if resource.get("id") == resource_id and resource.get("type") == "terminal":
                    return resource
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Terminal resource {resource_id!r} never appeared for session "
        f"{session_id} within {timeout}s; saw {last_seen!r}. The "
        "host-spawned codex-native auto-create did not register the "
        "Codex terminal, so the web UI would have no terminal to attach to."
    )


def _init_repo_with_marker_file(repo: Path, marker: str) -> None:
    """
    Initialize a git repo containing a single committed marker file.

    The marker file is committed so it materializes in any git worktree
    checked out from the branch — but it never exists in the runner's
    spec-bundle extraction dir. A Codex agent that can read it back is
    therefore proof the terminal launched in the worktree, not the
    bundle dir.

    :param repo: Directory to initialize as a git repo, e.g.
        ``Path("/tmp/x/repo")``. Must already exist.
    :param marker: Unique token written as the file's contents, e.g.
        ``"WT_3F9A2C"``.
    :returns: None.
    """
    (repo / _CWD_MARKER_FILE).write_text(marker + "\n")
    # ``-c user.*`` keeps the commit independent of any global git
    # identity config on the host running the test.
    git_base = [
        "git",
        "-c",
        "user.email=e2e@example.com",
        "-c",
        "user.name=e2e",
    ]
    # ``git init`` then ``checkout -b`` rather than ``init -b main`` so the
    # opt-in test runs on Git older than 2.28 (no ``init -b`` flag). The
    # branch name is cosmetic — the worktree is cut from a fresh API branch.
    subprocess.run([*git_base, "init"], cwd=repo, check=True)
    subprocess.run([*git_base, "checkout", "-b", "main"], cwd=repo, check=True)
    subprocess.run([*git_base, "add", _CWD_MARKER_FILE], cwd=repo, check=True)
    subprocess.run([*git_base, "commit", "-m", "add marker"], cwd=repo, check=True)


def _create_codex_host_session(
    http_client: httpx.Client,
    *,
    agent_id: str,
    host_id: str,
    workspace: str,
    git_branch: str | None = None,
) -> str:
    """
    Create a host-bound codex-native session and wait for its terminal.

    :param http_client: HTTP client pointed at the test server.
    :param agent_id: The ``codex-native-ui`` agent id.
    :param host_id: Online host id that runs the session.
    :param workspace: Source workspace directory, e.g. ``"/tmp/x/repo"``.
        For worktree sessions this is the source repo; the created
        worktree becomes the stored workspace.
    :param git_branch: When set, create a git worktree on this new
        branch, e.g. ``"codex-wt-3f9a"``. ``None`` uses *workspace*
        directly (no worktree).
    :returns: The created session id.
    """
    body: dict[str, object] = {
        "agent_id": agent_id,
        "host_id": host_id,
        "workspace": workspace,
    }
    if git_branch is not None:
        body["git"] = {"branch_name": git_branch}
    create = http_client.post("/v1/sessions", json=body, timeout=60.0)
    create.raise_for_status()
    session_id = create.json()["id"]
    # The host-spawned auto-create registers the Codex TUI as a terminal
    # resource; the read-marker turn needs it ready before sending.
    _poll_for_terminal_resource(
        http_client,
        session_id=session_id,
        resource_id=terminal_resource_id("codex", "main"),
        timeout=30.0,
    )
    return session_id


def _assert_codex_reads_cwd_marker(
    http_client: httpx.Client,
    *,
    session_id: str,
    marker: str,
) -> None:
    """
    Ask Codex to read the cwd marker file and assert it returns *marker*.

    The marker file (``_CWD_MARKER_FILE``) exists only in the session's
    intended cwd, never in the spec-bundle dir, so a correct read proves
    Codex's terminal launched in that directory.

    :param http_client: HTTP client pointed at the test server.
    :param session_id: The codex-native session id.
    :param marker: Unique token the file contains, e.g. ``"WT_3F9A2C"``.
    :returns: None.
    """
    event = http_client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"Read the file {_CWD_MARKER_FILE} in your current "
                            "directory and reply with its exact contents and "
                            "nothing else."
                        ),
                    }
                ],
            },
        },
        timeout=30.0,
    )
    event.raise_for_status()
    # _poll_for_assistant_marker returns only once the marker appears and
    # raises AssertionError on timeout — re-raise with the cwd-focused
    # context so a failure points at the workspace resolution, not a generic
    # "no reply" timeout.
    try:
        _poll_for_assistant_marker(
            http_client,
            session_id=session_id,
            marker=marker,
            timeout=180.0,
        )
    except AssertionError as exc:
        raise AssertionError(
            f"Codex did not return marker {marker!r} from {_CWD_MARKER_FILE} — "
            "its cwd is likely the spec bundle dir, not the session workspace."
        ) from exc


def test_codex_native_builtin_registered_at_startup(
    http_client: httpx.Client,
) -> None:
    """
    The server auto-registers ``codex-native-ui`` as a built-in agent.

    ``_ensure_default_codex_agent`` runs during lifespan startup and
    inserts the agent into the store. ``GET /v1/agents`` must list it
    so the Web UI new-session picker can offer Codex alongside Claude.
    """
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    agent_names = {a["name"] for a in resp.json()["data"]}
    assert _CODEX_NATIVE_AGENT_NAME in agent_names, (
        f"Expected {_CODEX_NATIVE_AGENT_NAME!r} in built-in agents "
        f"{agent_names}. _ensure_default_codex_agent did not run or "
        f"used a different name."
    )


def test_codex_native_builtin_session_can_be_created(
    http_client: httpx.Client,
    live_runner_id: str,
) -> None:
    """
    A session created against codex-native-ui gets the wrapper label.
    """
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = None
    for agent in resp.json()["data"]:
        if agent["name"] == _CODEX_NATIVE_AGENT_NAME:
            agent_id = agent["id"]
            break
    assert agent_id is not None

    create_resp = http_client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        timeout=30.0,
    )
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]

    session_resp = http_client.get(f"/v1/sessions/{session_id}")
    session_resp.raise_for_status()
    session_data = session_resp.json()
    assert session_data["agent_id"] == agent_id
    labels = session_data.get("labels", {})
    assert labels.get("omnigent.wrapper") == "codex-native-ui", (
        f"Expected wrapper label 'codex-native-ui', got {labels.get('omnigent.wrapper')!r}"
    )


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native round-trip e2e needs `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_codex_native_builtin_session_round_trip(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A codex-native-ui session returns an LLM response.

    Golden path: connect host -> discover codex-native-ui built-in ->
    create session with agent_id + host_id + workspace -> send a user
    message -> poll for the assistant response containing the marker.
    """
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()
    marker = f"CODEX_{uuid.uuid4().hex[:6].upper()}"

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
    )
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)

        create = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
            },
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        # The host-spawned auto-create must register the Codex TUI as a
        # streamable terminal resource (``terminal_codex_main``). Without
        # it the chat works (forwarder-driven) but the web UI has no
        # terminal to attach to — the bug this change fixes. Checked before
        # the message round-trip: the terminal is created at session
        # creation, independent of any turn. The poll raises a descriptive
        # AssertionError if the resource never appears — that IS the check.
        _poll_for_terminal_resource(
            http_client,
            session_id=session_id,
            resource_id=terminal_resource_id("codex", "main"),
            timeout=30.0,
        )

        event = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (f"Reply with exactly one word: {marker}"),
                        }
                    ],
                },
            },
            timeout=30.0,
        )
        event.raise_for_status()

        text = _poll_for_assistant_marker(
            http_client,
            session_id=session_id,
            marker=marker,
            timeout=180.0,
        )
        assert marker in text, f"marker {marker!r} missing from response: {text!r}"

        # Regression guard for message ordering: the user message MUST be
        # persisted before the assistant reply. The codex forwarder mirrors
        # a turn's items from two racing sources (the live event stream and
        # the ``thread/resume`` backfill); on a fresh thread the live
        # ``userMessage`` event can stream past before the subscription
        # lands, so it was recovered only via the later backfill and landed
        # AFTER the assistant reply — inverting the web bubbles, since AP
        # assigns position by POST arrival order and the UI renders strictly
        # in that order. ``_ensure_user_message_posted`` in
        # ``codex_native_forwarder`` recovers the missed user message before
        # the reply; this asserts the resulting durable order.
        messages = _ordered_message_items(http_client, session_id=session_id)
        roles = [item.get("role") for item in messages]
        assert "user" in roles and "assistant" in roles, (
            f"expected both a user and assistant message item, got roles={roles}"
        )
        first_user = roles.index("user")
        first_assistant = roles.index("assistant")
        assert first_user < first_assistant, (
            f"user message must precede the assistant reply in persisted order, "
            f"got roles={roles} — the codex forwarder inverted the turn."
        )
        # The user message also survived the mirror (not dropped to empty):
        # the prompt text carries the marker, so its presence proves the
        # user bubble holds the real prompt.
        assert marker in _user_text(messages[first_user]), (
            f"user message text missing the prompt marker {marker!r}; "
            f"got {_user_text(messages[first_user])!r}"
        )
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native streaming-order e2e needs `codex` on PATH and "
        "OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_codex_native_user_message_streams_before_assistant_delta(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    The user message streams to the web client BEFORE the assistant text.

    Regression for the TRANSIENT render bug: on a fresh codex thread the
    live ``userMessage`` event can be missed, so the forwarder recovers it
    via a resume. If recovery waited for the assistant's ``item/completed``,
    the assistant's ``response.output_text.delta`` events would reach the
    web SSE stream first and render a transient assistant bubble ABOVE the
    still-pending user bubble until the turn reconciled. The forwarder
    recovers at the assistant's ``item/started`` instead, so the user
    message's ``session.input.consumed`` event reaches the stream before
    the first assistant delta.

    The durable ``items`` API (asserted in
    ``test_codex_native_builtin_session_round_trip``) cannot catch this —
    it never contains the transient deltas. This asserts the live SSE
    event order the web UI actually renders from. With the
    ``item/started`` recovery removed (leaving only the ``item/completed``
    backstop), the first delta precedes ``session.input.consumed`` and
    this fails.

    :param live_server: Test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir for the workspace and daemon log.
    :returns: None.
    """
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()
    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)
        session_id = _create_codex_host_session(
            http_client, agent_id=agent_id, host_id=host_id, workspace=str(workspace)
        )

        event_types: list[str] = []
        stop = threading.Event()
        connected = threading.Event()
        both_seen = threading.Event()

        def _consume_stream() -> None:
            """
            Record SSE ``event:`` types in arrival order off the live stream.

            Sets ``connected`` on the first event — the stream's
            ``session.heartbeat`` ready-ack, emitted right after the
            subscriber slot registers — so the caller posts the turn only
            once the subscription is live (the stream replays no history).
            Sets ``both_seen`` once both the user-consumed event and an
            assistant text delta have arrived, so the caller waits
            event-driven (no polling).

            :returns: None.
            """
            with (
                httpx.Client(base_url=live_server, timeout=httpx.Timeout(120.0)) as stream_client,
                stream_client.stream("GET", f"/v1/sessions/{session_id}/stream") as resp,
            ):
                for line in resp.iter_lines():
                    if stop.is_set():
                        return
                    if line.startswith("event:"):
                        event_types.append(line[len("event:") :].strip())
                        connected.set()
                        if (
                            "session.input.consumed" in event_types
                            and "response.output_text.delta" in event_types
                        ):
                            both_seen.set()
                            return

        consumer = threading.Thread(target=_consume_stream, daemon=True)
        consumer.start()
        # Event-driven gate: the stream yields a ``session.heartbeat``
        # ready-ack at subscriber registration, so the first event proves
        # the subscription is live and the turn's events won't be missed.
        assert connected.wait(timeout=30.0), "SSE stream never connected"

        http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Reply with a short sentence describing what you are.",
                        }
                    ],
                },
            },
            timeout=30.0,
        )

        # Event-driven wait: the consumer sets this once both the user
        # message and the first assistant delta have streamed.
        both_seen.wait(timeout=180.0)
        stop.set()

        assert "session.input.consumed" in event_types, (
            f"user message never streamed to the web client; saw: {event_types}"
        )
        assert "response.output_text.delta" in event_types, (
            "assistant text never streamed as deltas; the prompt did not "
            f"produce streamed output. saw: {event_types}"
        )
        consumed_idx = event_types.index("session.input.consumed")
        first_delta_idx = event_types.index("response.output_text.delta")
        assert consumed_idx < first_delta_idx, (
            "assistant text delta streamed BEFORE the user message "
            f"(consumed at index {consumed_idx}, first delta at {first_delta_idx}) "
            "— the web UI would render the reply above the question. "
            f"event order: {event_types}"
        )
    finally:
        stop.set()
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1"
    or shutil.which("codex") is None
    or shutil.which("git") is None,
    reason=(
        "codex-native worktree e2e needs `codex` + `git` on PATH and "
        "OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_codex_native_worktree_session_runs_in_worktree(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A codex-native worktree session runs Codex inside the worktree.

    Regression for the bug where codex-native resolved its terminal cwd
    from ``ResolvedSpec.workdir`` — the runner's spec-bundle extraction
    dir (``runner-specs-<id>/ag_<id>-v<ver>``) — instead of the session
    workspace. Worktree sessions therefore launched Codex in a temp dir
    with no ``.git`` and never touched the worktree, while claude-native
    worked because it reads ``OMNIGENT_RUNNER_WORKSPACE`` directly.

    Golden path: init a real git repo with a committed marker file ->
    create a session with a git worktree branch -> ask Codex to read the
    marker file back. The file exists ONLY in the repo/worktree, never in
    the bundle dir, so the marker can appear in the response only if Codex
    launched in the worktree. With the bug present, Codex runs in the
    bundle dir, cannot read the file, and the poll times out.

    :param live_server: Test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir for the repo and daemon log.
    :returns: None.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = f"WT_{uuid.uuid4().hex[:6].upper()}"
    _init_repo_with_marker_file(repo, marker)
    branch = f"codex-wt-{uuid.uuid4().hex[:6]}"

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)

        session_id = _create_codex_host_session(
            http_client,
            agent_id=agent_id,
            host_id=host_id,
            workspace=str(repo),
            git_branch=branch,
        )

        # The stored workspace must be the created worktree, NOT the source
        # repo and NOT the bundle dir. ``_resolve_worktree_path`` places it
        # at ``<repo-parent>/<repo-name>-worktrees/<branch>``. If this
        # regresses, worktree creation itself is broken and the cwd test
        # below would be meaningless.
        session = http_client.get(f"/v1/sessions/{session_id}")
        session.raise_for_status()
        workspace = session.json().get("workspace")
        assert workspace is not None, "session has no workspace"
        assert workspace != str(repo), (
            f"workspace {workspace!r} equals the source repo — no worktree "
            "was created for the branch."
        )
        assert "-worktrees" in workspace, (
            f"workspace {workspace!r} is not a worktree path; expected the "
            "'<repo>-worktrees/<branch>' layout from _resolve_worktree_path."
        )
        # The committed marker file is checked out in the worktree.
        assert (Path(workspace) / _CWD_MARKER_FILE).is_file(), (
            f"marker file missing from worktree {workspace!r}; the worktree "
            "checkout did not include the committed file."
        )

        # The marker lives only in the repo/worktree, never in the spec
        # bundle dir. Its presence proves Codex's cwd is the worktree —
        # the exact behavior this fix restores.
        _assert_codex_reads_cwd_marker(http_client, session_id=session_id, marker=marker)
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native workspace e2e needs `codex` on PATH and OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_codex_native_session_uses_workspace_dir_without_worktree(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    With no worktree selected, Codex runs in the picked workspace dir.

    The non-worktree companion to
    ``test_codex_native_worktree_session_runs_in_worktree``: a host
    session created with just a ``workspace`` (no ``git``) must launch
    Codex in that workspace, not the runner's spec-bundle dir. Same
    bug class — ``ResolvedSpec.workdir`` (bundle dir) wrongly winning
    over the session workspace — would strand Codex in the temp bundle
    dir here too.

    Golden path: write a marker file into the workspace dir -> create a
    session pointing at it (no git) -> ask Codex to read the marker. The
    file exists only in the workspace, never in the bundle dir, so the
    marker can come back only if Codex's cwd is the workspace.

    :param live_server: Test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir for the workspace and daemon log.
    :returns: None.
    """
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()
    marker = f"WS_{uuid.uuid4().hex[:6].upper()}"
    (workspace / _CWD_MARKER_FILE).write_text(marker + "\n")

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)

        session_id = _create_codex_host_session(
            http_client,
            agent_id=agent_id,
            host_id=host_id,
            workspace=str(workspace),
        )

        # No worktree was requested, so the stored workspace must be the
        # picked directory itself (resolved), never a ``-worktrees`` path
        # and never the bundle dir.
        session = http_client.get(f"/v1/sessions/{session_id}")
        session.raise_for_status()
        resolved = session.json().get("workspace")
        assert resolved is not None, "session has no workspace"
        assert Path(resolved).resolve() == workspace.resolve(), (
            f"stored workspace {resolved!r} is not the picked dir "
            f"{str(workspace)!r}; a non-worktree session must run in the "
            "selected workspace."
        )
        assert "-worktrees" not in resolved, (
            f"workspace {resolved!r} looks like a worktree path, but no git branch was requested."
        )

        # Marker is only in the workspace dir — reading it proves cwd.
        _assert_codex_reads_cwd_marker(http_client, session_id=session_id, marker=marker)
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native image-routing e2e needs `codex` on PATH and "
        "OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_codex_native_image_routed_natively_not_as_base64_text(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    An image reaches Codex as a native image item, not base64-in-text.

    Regression for the web-UI freeze. ``CodexNativeExecutor`` used to
    ``json.dumps`` an ``input_image`` block — base64 ``image_url`` data URI
    and all — into the turn's *text* input. Codex echoed that multi-KB
    base64 string back as the ``userMessage``; the forwarder mirrored it
    into the durable user-message text, and the web UI hung rendering one
    giant unbroken token. The fix routes image/file blocks through
    ``_to_codex_input_items`` as native ``{"type": "image", "url": ...}``
    items.

    The regression discriminator is the **persisted user-message text**: it
    must not contain a ``data:`` / ``base64,`` URI. Under the old code the
    serialized image block put the full data URI into that text — exactly
    the blob that froze the UI. (Verified by reintroducing the old
    serialization: this assertion turns red, the durable user text then
    holding ``data:image/png;base64,...``.) The assistant-reply poll is only
    a liveness gate — naming the image's color does not by itself prove
    native delivery, since the model answers under both code paths.

    :param live_server: Test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir for the workspace and daemon log.
    :returns: None.
    """
    assert _TEST_IMAGE_PATH.exists(), (
        f"Test image missing at {_TEST_IMAGE_PATH}; restore it from git — "
        "its absence is a broken setup, not a skip."
    )
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)
        session_id = _create_codex_host_session(
            http_client,
            agent_id=agent_id,
            host_id=host_id,
            workspace=str(workspace),
        )

        # Upload the image to the session's file store; the server resolves
        # the file_id into a base64 data URI before the runner sees it, so
        # the native executor receives exactly the input_image block this
        # fix must route as a native image item.
        file_resp = http_client.post(
            f"/v1/sessions/{session_id}/resources/files",
            files={"file": ("test_image.png", _TEST_IMAGE_PATH.read_bytes(), "image/png")},
        )
        file_resp.raise_for_status()
        file_id = file_resp.json()["id"]

        event = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "What is the single dominant color of this image? "
                                "Reply with just the color word."
                            ),
                        },
                        {"type": "input_image", "file_id": file_id},
                    ],
                },
            },
            timeout=30.0,
        )
        event.raise_for_status()

        # Liveness gate: the image-bearing turn ran to a reply (the
        # attachment did not crash it) and the forwarder has mirrored the
        # user message into the durable items inspected below.
        _poll_for_assistant_reply(http_client, session_id=session_id, timeout=180.0)

        # Regression discriminator: no base64 data URI in the durable
        # user-message text. With the bug, the serialized image block put a
        # 'data:image/png;base64,...' URI into this text and the web UI hung.
        messages = _ordered_message_items(http_client, session_id=session_id)
        user_text = " ".join(_user_text(item) for item in messages if item.get("role") == "user")
        assert user_text, (
            "no user-message text persisted — the prompt should still appear "
            "as the user bubble even when an image is attached."
        )
        assert "base64," not in user_text and "data:image" not in user_text, (
            "a base64 data URI leaked into the rendered user-message text — "
            "the image block was serialized into text instead of routed as a "
            f"native image item. user text was: {user_text[:200]!r}..."
        )
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native image-only persistence e2e needs `codex` on PATH and "
        "OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_codex_native_image_only_persists_user_bubble_and_does_not_bleed(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    An image-only turn persists its own user bubble; a later turn is clean.

    Regression for two linked symptoms of an image-only (no text) message.
    The forwarder used to skip persisting a user message with no text, so:

    1. **Reply rendered above the image.** No durable ``user`` item was
       created for the image-only turn, leaving only the assistant reply;
       the image lived on as a dangling optimistic bubble that the web UI
       rendered *after* the reply.
    2. **The next message absorbed the prior image.** The image-only turn's
       optimistic pending-input entry was never drained, so the *next*
       message drained that stale entry (FIFO), folding the earlier image
       into the later message's bubble (and the later text duplicated into
       its own dangling bubble).

    The fix posts the user message when it carries a non-text block even
    with no text, so the server drains the correct pending entry and folds
    the image in by ``file_id``.

    Sequence — turn 1: image only; turn 2: text only. Assertions:

    - The first persisted message is a ``user`` item carrying an
      ``input_image`` block (the image bubble exists and precedes the
      reply — symptom 1).
    - Exactly one user message carries an image, and it is turn 1's; turn
      2's user message carries the marker text and **no** image block (the
      image did not bleed across turns — symptom 2).

    :param live_server: Test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir for the workspace and daemon log.
    :returns: None.
    """
    assert _TEST_IMAGE_PATH.exists(), (
        f"Test image missing at {_TEST_IMAGE_PATH}; restore it from git — "
        "its absence is a broken setup, not a skip."
    )
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()
    marker = f"PONG_{uuid.uuid4().hex[:6].upper()}"

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)
        session_id = _create_codex_host_session(
            http_client,
            agent_id=agent_id,
            host_id=host_id,
            workspace=str(workspace),
        )

        file_resp = http_client.post(
            f"/v1/sessions/{session_id}/resources/files",
            files={"file": ("test_image.png", _TEST_IMAGE_PATH.read_bytes(), "image/png")},
        )
        file_resp.raise_for_status()
        file_id = file_resp.json()["id"]

        # Turn 1: image only, no text block.
        turn1 = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_image", "file_id": file_id}],
                },
            },
            timeout=30.0,
        )
        turn1.raise_for_status()
        # Wait for turn 1 to finish before steering a second message in.
        _poll_for_assistant_reply(http_client, session_id=session_id, timeout=180.0)

        # Turn 2: text only.
        turn2 = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"Reply with exactly one word: {marker}"}
                    ],
                },
            },
            timeout=30.0,
        )
        turn2.raise_for_status()
        _poll_for_assistant_marker(
            http_client, session_id=session_id, marker=marker, timeout=180.0
        )

        messages = _ordered_message_items(http_client, session_id=session_id)
        user_items = [m for m in messages if m.get("role") == "user"]

        # Symptom 1: the image-only turn produced a durable user bubble, and
        # it is the first message overall — so it renders ABOVE the reply,
        # not below it. With the bug there was no user item at all and the
        # first message was the assistant reply.
        assert messages, "no messages persisted at all"
        assert messages[0].get("role") == "user", (
            "first persisted message is not the user's — the image-only user "
            f"bubble was not persisted before the reply. roles="
            f"{[m.get('role') for m in messages]}"
        )
        assert "input_image" in _content_types(messages[0]), (
            "the first user message carries no image block — the image-only "
            f"message was not persisted with its image. content_types="
            f"{_content_types(messages[0])}"
        )

        # Symptom 2: exactly one user message carries an image (turn 1), and
        # the later text-only turn did NOT absorb it via the stale pending
        # FIFO entry. With the bug, turn 2's user item held the prior image.
        image_user_items = [u for u in user_items if "input_image" in _content_types(u)]
        assert len(image_user_items) == 1, (
            f"expected exactly one user message to carry an image (turn 1), got "
            f"{len(image_user_items)}; the image bled across turns via the "
            "undrained pending-input FIFO entry."
        )
        text_user_items = [u for u in user_items if marker in _user_text(u)]
        assert len(text_user_items) == 1, (
            f"the marker {marker!r} should appear in exactly one user message; "
            f"got {len(text_user_items)}. user texts="
            f"{[_user_text(u) for u in user_items]}"
        )
        assert "input_image" not in _content_types(text_user_items[0]), (
            "the later text-only message absorbed the earlier image — the "
            "image-only turn's pending entry leaked and was folded into the "
            f"next message. content_types={_content_types(text_user_items[0])}"
        )
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native model/effort override e2e needs `codex` on PATH and "
        "OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)
def test_codex_native_web_model_effort_override_survives_turn(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A web model/effort pick applied mid-session does not break the turn.

    A model or reasoning-effort change
    made in the Omnigent web picker reaches the runner as
    ``ExecutorConfig.model`` / ``extra["reasoning_effort"]``, and
    ``CodexNativeExecutor.run_turn`` now applies it via a
    ``thread/settings/update`` request (whose ``ThreadSettingsUpdateParams``
    carries ``model`` / ``effort``) ahead of a bare ``turn/start``.

    The original fix tried to send ``model``/``effort`` ON ``turn/start`` —
    where the app-server schema does not accept them. If those fields are
    rejected by the real app-server (``client.request`` raises ->
    ``ExecutorError``), *every web turn after a picker change fails*. This
    test exercises the real codex app-server: it switches the model and
    reasoning effort via ``PATCH /v1/sessions`` (exactly what the SPA does),
    then sends a turn and asserts it runs to a reply. A reply proves the
    app-server accepted the ``thread/settings/update`` the override path
    emits — i.e. the catastrophic "every web turn fails" mode is gone.

    The exact RPC shape (``thread/settings/update`` precedes a bare
    ``turn/start``, with overrides omitted when unset and invalid effort
    dropped) is pinned deterministically by
    ``tests/inner/test_codex_native_executor.py``; this is the live
    counterpart that the unit fake cannot provide — that the override is
    *honored*, not rejected.

    The target model defaults to the session's own running model (always a
    valid id, profile-independent) so the override path is exercised on any
    profile; set ``OMNIGENT_E2E_CODEX_SWITCH_MODEL`` to a different valid
    codex model to drive a genuine cross-model switch.

    :param live_server: Test server URL.
    :param http_client: HTTP client pointed at the test server.
    :param tmp_path: Per-test temp dir for the workspace and daemon log.
    :returns: None.
    """
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()
    boot_marker = f"BOOT_{uuid.uuid4().hex[:6].upper()}"
    switched_marker = f"CODEX_{uuid.uuid4().hex[:6].upper()}"

    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _codex_native_agent_id(http_client)
        session_id = _create_codex_host_session(
            http_client,
            agent_id=agent_id,
            host_id=host_id,
            workspace=str(workspace),
        )

        # Turn 1 establishes the native thread on its launch-pinned model,
        # so the override below is a genuine mid-session change (the only
        # path that must ride thread/settings/update — a fresh thread could
        # otherwise be launch-pinned).
        _send_user_text(
            http_client,
            session_id=session_id,
            text=f"Reply with exactly one word: {boot_marker}",
        )
        _poll_for_assistant_marker(
            http_client, session_id=session_id, marker=boot_marker, timeout=180.0
        )

        # Resolve a valid target model: an explicit env override, else the
        # session's current running model (guaranteed valid on any profile).
        session = http_client.get(f"/v1/sessions/{session_id}")
        session.raise_for_status()
        session_data = session.json()
        current_model = session_data.get("model_override") or session_data.get("llm_model")
        target_model = os.environ.get("OMNIGENT_E2E_CODEX_SWITCH_MODEL") or current_model
        assert target_model, (
            "could not resolve a model to override with — the session reported "
            f"neither model_override nor llm_model: {session_data!r}"
        )

        # The web picker action: PATCH the session's model + reasoning effort.
        # "high" is a valid codex effort (CODEX_EFFORTS) and a real settings
        # change, so run_turn emits thread/settings/update with both fields.
        patch = http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"model_override": target_model, "reasoning_effort": "high"},
            timeout=30.0,
        )
        patch.raise_for_status()

        # Turn 2 carries the override. run_turn sends thread/settings/update
        # {model, effort} before turn/start; if the app-server rejected those
        # fields (the pre-fix turn-path behavior), this turn would raise an
        # ExecutorError and the marker would never arrive.
        _send_user_text(
            http_client,
            session_id=session_id,
            text=f"Reply with exactly one word: {switched_marker}",
        )
        text = _poll_for_assistant_marker(
            http_client, session_id=session_id, marker=switched_marker, timeout=180.0
        )
        assert switched_marker in text, (
            f"override turn produced no marker {switched_marker!r} — the web "
            "model/effort pick broke the turn. response: "
            f"{text!r}"
        )

        # The picked model persisted on the session (keeps the web dropdown
        # and the cost gate in sync). With target == the running model, the
        # inbound forwarder mirror re-affirms the same id, so this is stable.
        after = http_client.get(f"/v1/sessions/{session_id}")
        after.raise_for_status()
        assert after.json().get("model_override") == target_model, (
            "model_override did not persist after the override turn; got "
            f"{after.json().get('model_override')!r}, expected {target_model!r}"
        )
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()
