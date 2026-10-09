"""Real Cursor E2E for native sub-agent tool-result delivery.

This is the reported ``sys_session_send`` journey, not a forwarder mock and
not a top-level ``omnigent cursor`` smoke test:

1. A deterministic mock parent calls ``sys_session_send`` for a
   ``cursor-native`` child.
2. The official, authenticated ``cursor-agent`` runs its real ``Shell`` and
   ``Read`` tools in a temporary workspace.
3. Cursor completes, waking the parent, which drains the result through the
   real ``sys_read_inbox`` tool.

The parent model is mocked only so dispatch and inbox draining are
deterministic. The child is real Cursor using the ambient ``cursor-agent
login``. A runner-only scheduling shim holds Cursor's real final message for
one mirror read after its real stop marker exists. This deterministically
creates the reported production ordering without faking either event::

    transcript snapshot misses final reply
    -> completion marker becomes visible
    -> next transcript snapshot sees final reply

On the completion-order bug, the child transcript eventually contains both
probe values, but the parent inbox result is empty because ``idle`` was posted
before the final reply. The fixed forwarder delivers the reply first.

Opt in from a shell with an authenticated Cursor CLI::

    OMNIGENT_E2E_CURSOR_NATIVE=1 \
      .venv/bin/python -m pytest \
      tests/e2e/test_cursor_native_subagent_tool_result_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.cursor_native.bridge import bridge_dir_for_session_id, kill_session
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_CURSOR_NATIVE") != "1"
        or shutil.which("cursor-agent") is None
        or shutil.which("tmux") is None,
        reason=(
            "real cursor-native sub-agent e2e needs OMNIGENT_E2E_CURSOR_NATIVE=1, "
            "an authenticated cursor-agent, and tmux"
        ),
    ),
    pytest.mark.timeout(900, method="signal"),
]

_CHILD_TIMEOUT_S = 360.0
_INBOX_TIMEOUT_S = 180.0
_CURSOR_MODEL = "gpt-5.4-mini"
_FINAL_REPLY_MARKER = "OMNI5700_CURSOR_FINAL_REPLY"
_RACE_SHIM_SIGNAL = ".omni5700-cursor-race-observed"

_RACE_SITECUSTOMIZE = r'''\
"""Schedule the cursor-native transcript/completion race in the E2E runner."""

import json
import os
import time
from pathlib import Path


if os.environ.get("OMNIGENT_E2E_CURSOR_RACE_SHIM") == "1" and os.environ.get(
    "RUNNER_SERVER_URL"
):
    from omnigent.harnesses.cursor_native import forwarder
    from omnigent.harnesses.cursor_native import status

    _real_read_new_items = forwarder._read_new_items
    _real_count_turn_ends = status.count_turn_ends
    _final_reply_marker = os.environ["OMNIGENT_E2E_CURSOR_FINAL_REPLY_MARKER"]
    _signal_path = Path(os.environ["OMNIGENT_E2E_CURSOR_RACE_SIGNAL"])
    _bridge_dir = None
    _final_reply_hidden_once = False
    _completion_visible = False

    def _controlled_count_turn_ends(bridge_dir):
        global _bridge_dir
        _bridge_dir = bridge_dir
        if not _completion_visible:
            return 0
        return _real_count_turn_ends(bridge_dir)

    def _controlled_read_new_items(store_path, last_rowid, agent_name):
        global _completion_visible, _final_reply_hidden_once
        items = _real_read_new_items(store_path, last_rowid, agent_name)
        if _final_reply_hidden_once:
            return items

        for index, item in enumerate(items):
            if item.item_type != "message" or _final_reply_marker not in json.dumps(
                item.item_data
            ):
                continue
            if _bridge_dir is None:
                raise RuntimeError("cursor race shim did not observe a bridge directory")

            deadline = time.monotonic() + 30.0
            while _real_count_turn_ends(_bridge_dir) == 0:
                if time.monotonic() >= deadline:
                    raise RuntimeError("real Cursor stop marker did not arrive")
                time.sleep(0.01)

            # Do not return rows after the hidden reply: advancing their rowid
            # would cause the real reply to be skipped on the next poll.
            _completion_visible = True
            _final_reply_hidden_once = True
            _signal_path.write_text("real-final-reply-hidden-until-next-poll\n")
            return items[:index]
        return items

    status.count_turn_ends = _controlled_count_turn_ends
    forwarder._read_new_items = _controlled_read_new_items
'''


@pytest.fixture(scope="session")
def cursor_runner_workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Configure the native workspace and runner-only race scheduler.

    The shared E2E runner inherits this environment when ``http_client`` starts
    ``live_server``. Production runners already carry the setting; the generic
    test fixture does not because most E2Es never launch a native terminal.

    The ``sitecustomize`` shim loads only in the runner (identified by
    ``RUNNER_SERVER_URL``). It waits for Cursor's real stop hook before hiding
    the marked real assistant reply from exactly one transcript read.
    """
    workspace = tmp_path_factory.mktemp("cursor-native-subagent-workspace")
    shim_dir = tmp_path_factory.mktemp("cursor-native-race-shim")
    (shim_dir / "sitecustomize.py").write_text(_RACE_SITECUSTOMIZE, encoding="utf-8")
    signal_path = workspace / _RACE_SHIM_SIGNAL
    env_updates = {
        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
        "OMNIGENT_E2E_CURSOR_RACE_SHIM": "1",
        "OMNIGENT_E2E_CURSOR_FINAL_REPLY_MARKER": _FINAL_REPLY_MARKER,
        "OMNIGENT_E2E_CURSOR_RACE_SIGNAL": str(signal_path),
        "PYTHONPATH": os.pathsep.join(
            entry for entry in (str(shim_dir), os.environ.get("PYTHONPATH", "")) if entry
        ),
    }
    prior = {key: os.environ.get(key) for key in env_updates}
    os.environ.update(env_updates)
    try:
        yield workspace
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def cursor_e2e_rig(
    cursor_runner_workspace: Path,
    http_client: httpx.Client,
) -> tuple[httpx.Client, Path]:
    """Start the shared server/runner only after its workspace is configured."""
    return http_client, cursor_runner_workspace


def _tool_call(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
    """Build one deterministic mock Responses API tool call."""
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _session_items(client: httpx.Client, session_id: str) -> list[dict[str, object]]:
    """Return a session's durable items in chronological order."""
    response = client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
    )
    response.raise_for_status()
    return response.json()["data"]


def _wait_for_child(client: httpx.Client, parent_id: str) -> str:
    """Wait for ``sys_session_send`` to create its cursor child."""
    deadline = time.monotonic() + _CHILD_TIMEOUT_S
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{parent_id}/child_sessions")
        response.raise_for_status()
        children = response.json().get("data", [])
        if children:
            child = children[0]
            return str(child.get("session_id") or child["id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"cursor child was not created for parent {parent_id}")


def _wait_for_markers(
    client: httpx.Client,
    session_id: str,
    markers: tuple[str, ...],
    *,
    timeout: float,
) -> list[dict[str, object]]:
    """Wait until every marker is present in a session's durable items."""
    deadline = time.monotonic() + timeout
    items: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        items = _session_items(client, session_id)
        blob = json.dumps(items)
        if all(marker in blob for marker in markers):
            return items
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"markers {markers!r} did not appear in session {session_id}; "
        f"last items={json.dumps(items, indent=2)}"
    )


def _wait_for_inbox_output(
    client: httpx.Client,
    parent_id: str,
) -> tuple[list[object], list[dict[str, object]]]:
    """Wait for the parent to execute ``sys_read_inbox`` and return its output."""
    deadline = time.monotonic() + _INBOX_TIMEOUT_S
    items: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        items = _session_items(client, parent_id)
        call_ids = {
            item.get("call_id")
            for item in items
            if item.get("type") == "function_call" and item.get("name") == "sys_read_inbox"
        }
        outputs = [
            item.get("output")
            for item in items
            if item.get("type") == "function_call_output" and item.get("call_id") in call_ids
        ]
        if outputs:
            return outputs, items
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"parent {parent_id} never drained sys_read_inbox; "
        f"last items={json.dumps(items, indent=2)}"
    )


def test_real_cursor_tool_reply_reaches_parent_inbox(
    cursor_e2e_rig: tuple[httpx.Client, Path],
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A real Cursor child's post-tool reply reaches its parent before completion."""
    http_client, workspace = cursor_e2e_rig
    nonce = uuid.uuid4().hex
    shell_source = f"shell-{nonce}"
    shell_result = shell_source[::-1]
    read_result = f"read-{nonce}"
    (workspace / "SHELL_PROBE.txt").write_text(shell_source, encoding="utf-8")
    (workspace / "READ_PROBE.txt").write_text(read_result, encoding="utf-8")

    child_task = (
        "Use Cursor's Shell tool to run exactly: "
        "python -c 'from pathlib import Path; "
        'print(Path("SHELL_PROBE.txt").read_text().strip()[::-1])\'. '
        "Then use Cursor's Read tool to read READ_PROBE.txt. After both tools "
        f"finish, reply starting with exactly {_FINAL_REPLY_MARKER}, followed "
        "by the Shell output and the complete Read output. Do not use the "
        "final-reply marker before both tool calls finish and do not omit "
        "either value."
    )
    suffix = nonce[:8]
    parent_model = f"mock-cursor-parent-{suffix}"
    mock_base_url = f"{mock_llm_server_url}/v1"
    parent_name = register_inline_agent(
        http_client,
        name=f"cursor-parent-{suffix}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Dispatch the cursor sub-agent, then drain its inbox result when woken.",
        mock_llm_base_url=mock_base_url,
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": str(workspace),
                "sandbox": {"type": "none"},
            },
            "tools": {
                "cursor": {
                    "type": "agent",
                    "description": "Real Cursor CLI coding sub-agent.",
                    "executor": {"harness": "cursor-native", "model": _CURSOR_MODEL},
                    "os_env": "inherit",
                    "prompt": "Use the requested Cursor tools and report their exact outputs.",
                }
            },
        },
    )

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _tool_call(
                        "sys_session_send",
                        {
                            "agent": "cursor",
                            "title": "cursor-tool-result",
                            "args": {
                                "purpose": "explore",
                                "model": _CURSOR_MODEL,
                                "input": child_task,
                            },
                        },
                        "dispatch-cursor",
                    )
                ]
            },
            {"text": "Cursor dispatched; waiting for its inbox result."},
            {"tool_calls": [_tool_call("sys_read_inbox", {}, "read-cursor-inbox")]},
            {"text": "Cursor inbox drained."},
        ],
        key=parent_model,
    )

    parent_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    child_id: str | None = None
    try:
        dispatch_id = send_user_message_to_session(
            http_client,
            session_id=parent_id,
            content="Dispatch cursor to run the Shell and Read probe task.",
        )
        poll_session_until_terminal(
            http_client,
            session_id=parent_id,
            response_id=dispatch_id,
            timeout=180,
        )
        child_id = _wait_for_child(http_client, parent_id)

        # This proves the official Cursor child ran the task and its final reply
        # eventually reached its own durable transcript. It passes even on the
        # buggy ordering and makes an empty parent result diagnostic, not an
        # ambiguous Cursor/auth/tool failure.
        _wait_for_markers(
            http_client,
            child_id,
            (_FINAL_REPLY_MARKER, shell_result, read_result),
            timeout=_CHILD_TIMEOUT_S,
        )
        signal_path = workspace / _RACE_SHIM_SIGNAL
        assert signal_path.read_text(encoding="utf-8").strip() == (
            "real-final-reply-hidden-until-next-poll"
        ), "runner race shim did not schedule the completion-order window"

        inbox_outputs, parent_items = _wait_for_inbox_output(http_client, parent_id)
        inbox_blob = json.dumps(inbox_outputs)
        assert shell_result in inbox_blob and read_result in inbox_blob, (
            "The real cursor-native child completed Shell and Read and its own "
            "transcript contains both probe values, but sys_read_inbox received "
            "an empty/stale completion payload. The forwarder posted child "
            "completion before delivering the final reply.\n"
            f"Expected Shell marker: {shell_result!r}\n"
            f"Expected Read marker: {read_result!r}\n"
            f"Inbox outputs: {json.dumps(inbox_outputs, indent=2)}\n"
            f"Parent items: {json.dumps(parent_items, indent=2)}"
        )
    finally:
        if child_id is not None:
            with contextlib.suppress(OSError, RuntimeError):
                kill_session(bridge_dir_for_session_id(child_id), timeout_s=5.0)
        with contextlib.suppress(httpx.HTTPError):
            http_client.delete(f"/v1/sessions/{parent_id}")
