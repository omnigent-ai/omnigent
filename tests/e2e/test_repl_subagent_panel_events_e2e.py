"""E2E for the data contract behind the REPL's live sub-agent display.

The CLI's ``state: N agents running`` badge and ``↓`` sub-agents menu are
fed by two server-side sources, both exercised here against a real LLM:

* the **parent SSE stream** emits ``session.created`` and
  ``session.child_session.updated`` (with ``busy`` / ``current_task_status``)
  for the active session's direct children — the live fast-path; and
* ``GET /v1/sessions/{id}/child_sessions`` lists those children — the source
  the recursive tree poll reads for deeper levels.

If the server stops emitting either, the CLI panel goes blank even while
sub-agents are running. The first test asserts that data contract against a
real LLM. The second drives the real CLI under a PTY against the mock LLM and
reads the rendered toolbar, for the case the runner's own fan-out cannot
cover: a child whose status changes without its parent's runner knowing.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke::

    pytest tests/e2e/test_repl_subagent_panel_events_e2e.py \\
        --llm-api-key "$(cat /tmp/mykey)" -v
    pytest tests/e2e/test_repl_subagent_panel_events_e2e.py -k toolbar -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from omnigent_client._sessions import SessionsNamespace

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    lookup_agent_id,
    poll_session_until_terminal,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Wide enough that the right-aligned ``state:`` badge fits after the hint
# row; at 120 columns it falls off the edge and never reaches the PTY.
_REPL_DIMENSIONS = (40, 200)


def _iter_sse(response: httpx.Response):
    """Yield decoded SSE event dicts from a streaming response."""
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")),
                None,
            )
            if data_line is None:
                continue
            payload = data_line[len("data:") :].strip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue


def _frame_of_type(ev: dict[str, Any], event_type: str) -> dict[str, Any] | None:
    """Return the event body if *ev* (flat or enveloped) has *event_type*."""
    if ev.get("type") == event_type:
        return ev
    data = ev.get("data")
    if isinstance(data, dict) and data.get("type") == event_type:
        return data
    return None


def test_parent_stream_and_child_sessions_expose_subagents(
    live_server: str,
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    llm_api_key: str,
    using_mock_llm: bool,
) -> None:
    """A real sub-agent run emits the SSE child events the badge consumes and
    lists the child via ``child_sessions`` (the tree poll's source).

    :param live_server: Base URL for a side client that tails the stream
        without head-of-line-blocking the main client.
    :param http_client: HTTP client pointed at the live server.
    :param archer_agent: Uploaded archer agent (has fact_checker / summarizer
        server-side sub-agents — no native CLI required).
    :param live_runner_id: Registered runner the session binds to.
    :param llm_api_key: Gates the test on a configured LLM key.
    :param using_mock_llm: True when no ``--llm-api-key`` was passed. The mock
        LLM returns canned text, never the ``sys_session_send`` tool call that
        spawns the sub-agent, so this test needs a real LLM to exercise its
        data contract — skip cleanly rather than failing on a missing child.
    """
    if using_mock_llm:
        pytest.skip(
            "needs a real --llm-api-key: the mock LLM never emits the "
            "sys_session_send tool call that spawns the sub-agent this test "
            "asserts on."
        )
    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    # Tail the parent stream in a side thread so we capture the transient
    # child events while the turn is in flight (they are SSE-only).
    saw_created = threading.Event()
    saw_busy_child = threading.Event()
    saw_status_field = threading.Event()
    stop = threading.Event()

    def _tail_stream() -> None:
        try:
            with httpx.Client(base_url=live_server, timeout=240.0) as side:
                with side.stream("GET", f"/v1/sessions/{session_id}/stream") as resp:
                    if resp.status_code != 200:
                        return
                    for ev in _iter_sse(resp):
                        if stop.is_set():
                            return
                        created = _frame_of_type(ev, "session.created")
                        if created and created.get("child_session_id"):
                            saw_created.set()
                        updated = _frame_of_type(ev, "session.child_session.updated")
                        if updated:
                            child = updated.get("child") or {}
                            if child.get("busy") is True:
                                saw_busy_child.set()
                            if "current_task_status" in child:
                                saw_status_field.set()
        except Exception:
            return

    tail = threading.Thread(target=_tail_stream, daemon=True)
    tail.start()

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use sys_session_send to spawn the summarizer sub-agent and ask "
            "it to summarize the concept of photosynthesis in exactly two "
            "sentences. Wait for its result before you finish."
        ),
    )
    # Runner-native session: poll the session snapshot, NOT
    # ``GET /v1/responses/{id}`` (which a runner-bound turn never creates — that
    # route falls through to the web SPA and returns HTML, 200, so a naive
    # ``.json()`` blows up before any assertion). The snapshot helper reports
    # terminal as ``idle``.
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=240
    )
    assert body["status"] in ("idle", "completed"), f"Sub-agent run failed: {body.get('error')}"

    stop.set()
    tail.join(timeout=10)

    # The tree-poll source: the parent must list the spawned child.
    resp = http_client.get(f"/v1/sessions/{session_id}/child_sessions")
    resp.raise_for_status()
    children = resp.json().get("data", [])
    assert children, (
        "GET /v1/sessions/{id}/child_sessions returned no children — the ↓ "
        "menu / tree poll would render nothing despite a sub-agent running."
    )
    first = children[0]
    assert "busy" in first and "current_task_status" in first, (
        "child_sessions rows are missing the busy / current_task_status fields "
        f"the badge + menu render; got keys: {sorted(first)}"
    )

    # The SDK rollup (subtree_busy / tree_busy) an SDK driver consumes: against
    # the same real server, child_sessions_tree must list the spawned child and
    # subtree_busy must settle to False now the run is terminal — the SDK-side
    # mirror of the data contract asserted above.
    async def _sdk_rollup() -> tuple[list[dict[str, Any]], bool]:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            ns = SessionsNamespace(ac, live_server)
            tree = await ns.child_sessions_tree(session_id)
            busy = await ns.subtree_busy(session_id)
            return tree, busy

    tree, subtree_busy = asyncio.run(_sdk_rollup())
    assert {c["id"] for c in children} <= {n["id"] for n in tree}, (
        "child_sessions_tree did not surface the spawned child the one-level "
        "endpoint returned — the SDK rollup would miss it."
    )
    assert subtree_busy is False, (
        "subtree_busy stayed True after the run reached a terminal state — an "
        "SDK eval driver would never resume injecting 'your turn'."
    )

    # The live fast-path: the parent stream emitted the transient child events.
    assert saw_created.is_set(), (
        "parent stream never emitted session.created for the spawned child — "
        "the badge would not flip to 'agents running' until the next poll."
    )
    assert saw_busy_child.is_set(), (
        "no session.child_session.updated reported busy=True — the badge's "
        "running count would never light."
    )
    assert saw_status_field.is_set(), (
        "child updates carried no current_task_status — the menu's per-agent "
        "status word would be blank."
    )


def _attach_repl_env(tmp_home: Path) -> dict[str, str]:
    """Build the env for spawning ``omnigent attach`` under a PTY.

    An isolated ``HOME`` / config home keeps the developer's real config out
    of the run, and the persisted theme skips the interactive theme picker
    that would otherwise stand in for the prompt.

    :param tmp_home: Per-test directory to use as ``HOME``.
    :returns: The subprocess environment.
    """
    from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env

    config_home = tmp_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n",
    )
    sdk_paths = [
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    existing_pp = os.environ.get("PYTHONPATH", "")
    env = {
        **os.environ,
        "HOME": str(tmp_home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "PYTHONPATH": os.pathsep.join([*sdk_paths, existing_pp] if existing_pp else sdk_paths),
        "TERM": "xterm-256color",
        "LINES": str(_REPL_DIMENSIONS[0]),
        "COLUMNS": str(_REPL_DIMENSIONS[1]),
        "PROMPT_TOOLKIT_NO_CPR": "1",
    }
    return ensure_repl_test_theme_env(env)


# The settled frame lists the seeded child (``↓ agents``) with the badge
# asleep on the same toolbar line; the lit frame is the running badge.
_SETTLED_TOOLBAR = re.compile(r"↓ agents.*state: sleeping")
_RUNNING_TOOLBAR = re.compile(r"state: 1 agent running")


def _wait_for_toolbar(repl: Any, pattern: re.Pattern[str], timeout: float) -> str:
    """Repaint the REPL until its toolbar matches *pattern* or *timeout* elapses.

    prompt-toolkit repaints only the cells that changed, so a badge flip
    reaches the PTY as scattered fragments. Ctrl+L forces a full frame each
    poll so the toolbar can be matched as one contiguous line.

    :param repl: Live ``pexpect.spawn`` REPL sitting at its prompt.
    :param pattern: Toolbar text to wait for, e.g. :data:`_RUNNING_TOOLBAR`.
    :param timeout: Seconds to keep polling.
    :returns: The last full frame, ANSI-stripped, whether or not it matched;
        callers assert on it so a miss shows the real screen.
    """
    from tests.e2e.omnigent._pexpect_harness import strip_ansi
    from tests.e2e.omnigent._repl_test_helpers import drain_for

    frame = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        repl.sendcontrol("l")
        frame = strip_ansi(drain_for(repl, 1.0))
        if pattern.search(frame):
            break
    return frame


@pytest.mark.posix_only
def test_repl_toolbar_shows_child_driven_outside_parent_runner(
    live_server: str,
    http_client: httpx.Client,
    coder_agent: str,
    live_runner_id: str,
    mock_llm_server_url: str,
    using_mock_llm: bool,
    tmp_path: Path,
) -> None:
    """The real CLI toolbar lights up for a child the parent's runner never
    registered.

    Reproduces the shape behind a reused or directly-driven sub-agent: the
    child exists before the REPL attaches, so the REPL seeds it idle and
    parks its tree poll; then a turn is posted to the child directly. The
    runner's in-process child→parent fan-out knows nothing about this child,
    so the parent's stream only learns the child went busy if the server
    mirrors the status edge. Without that the badge sits on
    ``state: sleeping`` while the child works.

    :param live_server: Base URL of the live server the REPL attaches to.
    :param http_client: HTTP client pointed at the live server.
    :param coder_agent: Uploaded agent both sessions bind to.
    :param live_runner_id: Registered runner the parent binds to; the child
        inherits it.
    :param mock_llm_server_url: Mock LLM whose gate holds the child's turn
        open while the toolbar is read.
    :param using_mock_llm: True when no ``--llm-api-key`` was passed.
    :param tmp_path: Per-test directory for the REPL's isolated ``HOME``.
    """
    pexpect = pytest.importorskip("pexpect")
    if not using_mock_llm:
        pytest.skip(
            "relies on the mock LLM's gate to hold the child's turn open while "
            "the toolbar is read."
        )

    token = f"child-turn-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "child done", "block": True}],
        match=token,
    )
    parent_id = create_runner_bound_session(
        http_client, agent_name=coder_agent, runner_id=live_runner_id
    )
    resp = http_client.post(
        "/v1/sessions",
        json={
            "agent_id": lookup_agent_id(http_client, coder_agent),
            "parent_session_id": parent_id,
            "title": "probe:worker",
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    child_id = str(resp.json()["id"])

    repl = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "attach", parent_id, "--server", live_server],
        env=_attach_repl_env(tmp_path / "home"),
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        timeout=120,
        dimensions=_REPL_DIMENSIONS,
    )
    response_id: str | None = None
    try:
        repl.expect("❯", timeout=90)
        # The REPL discovered the pre-existing child and, seeing it idle,
        # parked its tree poll: the ↓ menu is offered but nothing is running.
        settled = _wait_for_toolbar(repl, _SETTLED_TOOLBAR, timeout=20.0)
        assert _SETTLED_TOOLBAR.search(settled), (
            f"REPL never listed the seeded child with the badge asleep; saw:\n{settled}"
        )

        response_id = send_user_message_to_session(http_client, session_id=child_id, content=token)
        lit = _wait_for_toolbar(repl, _RUNNING_TOOLBAR, timeout=25.0)
        assert _RUNNING_TOOLBAR.search(lit), (
            "toolbar stayed asleep while the child ran: the parent stream never "
            f"carried the child's busy edge; saw:\n{lit}"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            release_mock_gate(mock_llm_server_url)
        with contextlib.suppress(pexpect.ExceptionPexpect):
            repl.sendcontrol("d")
            repl.expect(pexpect.EOF, timeout=15)
        if repl.isalive():
            repl.terminate(force=True)
        reset_mock_llm(mock_llm_server_url)
    if response_id is not None:
        poll_session_until_terminal(
            http_client, session_id=child_id, response_id=response_id, timeout=60
        )
