"""Host and session helpers the claude-native and claude-sdk host E2E modules share."""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from tests._helpers.polling import poll_until
from tests._helpers.session import send_user_text as _send_user_message
from tests.e2e.test_host_claude_native_e2e import (
    _assistant_text,
    _poll_for_assistant_marker,
    _spawn_host_daemon,
)


def _builtin_agent_id(client: httpx.Client, name: str) -> str:
    """
    Return the id of a built-in agent by name from ``GET /v1/agents``.

    :param client: HTTP client pointed at the test server.
    :param name: Built-in agent name, e.g. ``"sdk-chat-builtin"``.
    :returns: The agent id.
    :raises AssertionError: If no built-in with that name is registered.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == name:
            return str(agent["id"])
    raise AssertionError(f"built-in agent {name!r} not registered on the server")


def _launch_runner(
    client: httpx.Client,
    *,
    host_id: str,
    session_id: str,
    workspace: Path,
    git: dict[str, str] | None = None,
) -> dict[str, object]:
    """
    Bind a clone to a host by launching a runner (the resume step).

    Mirrors the directory-picker's ``POST /v1/hosts/{id}/runners`` call.
    When *git* is set the host creates a worktree off *workspace* and the
    runner starts there instead.

    :param client: HTTP client pointed at the test server.
    :param host_id: The online host's id.
    :param session_id: The clone's session id to bind.
    :param workspace: Source repo / working directory on the host.
    :param git: Optional ``{"branch_name": ...}`` worktree block. ``None``
        binds *workspace* directly (same-dir resume).
    :returns: The launch response JSON.
    """
    body: dict[str, object] = {"session_id": session_id, "workspace": str(workspace)}
    if git is not None:
        body["git"] = git
    resp = client.post(f"/v1/hosts/{host_id}/runners", json=body, timeout=120.0)
    resp.raise_for_status()
    return dict(resp.json())


@contextmanager
def _host_daemon(tmp_path: Path, live_server: str) -> Iterator[None]:
    """
    Spawn an ``omnigent connect`` daemon for the test's duration.

    :param tmp_path: Per-test temp dir for the daemon log.
    :param live_server: Test server URL the daemon registers with.
    :returns: Iterator yielding once the daemon is spawned; SIGTERM'd on
        exit.
    """
    daemon = _spawn_host_daemon(tmp_path=tmp_path, live_server=live_server)
    try:
        yield
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


def _plant_code_words(client: httpx.Client, *, session_id: str, words: list[str]) -> None:
    """Plant each code word in its own turn and wait for Claude's ACK."""
    for index, word in enumerate(words):
        ack = f"ACK{index}"
        _send_user_message(
            client,
            session_id=session_id,
            text=f"Remember this code word for later: {word}. Reply with exactly: {ack}",
        )
        _poll_for_assistant_marker(client, session_id=session_id, marker=ack, timeout=180.0)


def _wait_until_idle(client: httpx.Client, *, session_id: str) -> None:
    """Wait for *session_id*'s turn to end; the turn-end edge can trail the reply."""
    poll_until(
        lambda: (
            client.get(f"/v1/sessions/{session_id}", timeout=30.0).json()["status"] != "running"
        ),
        timeout_s=60.0,
    )


def _prompt_item_id(client: httpx.Client, *, session_id: str, fragment: str) -> str:
    """
    Return the id of *session_id*'s user prompt containing *fragment*.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session to search.
    :param fragment: Text the prompt contains, e.g. a planted code word.
    :returns: The prompt's item id.
    """
    resp = client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc", "type": "message"},
        timeout=30.0,
    )
    resp.raise_for_status()
    for item in resp.json()["data"]:
        texts = [part.get("text", "") for part in item.get("content", [])]
        if item.get("role") == "user" and any(fragment in text for text in texts):
            return str(item["id"])
    raise AssertionError(f"no user prompt containing {fragment!r} in {session_id}")


def _recall_code_words(client: httpx.Client, *, session_id: str, expected: str) -> str:
    """Ask for every planted code word and return the newer reply naming *expected*.

    An earlier reply, such as a planting ACK, can name *expected* too, so only
    items after the newest one before the question count.
    """
    newest = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 1, "order": "desc"}, timeout=30.0
    )
    newest.raise_for_status()
    boundary = next((str(item["id"]) for item in newest.json()["data"]), None)
    _send_user_message(
        client,
        session_id=session_id,
        text=(
            "List every code word I gave you earlier in this conversation, separated by "
            "spaces, and nothing else."
        ),
    )
    return _poll_for_assistant_marker(
        client, session_id=session_id, marker=expected, timeout=180.0, after=boundary
    )


@dataclass(frozen=True)
class _Rewound:
    """What a ``/rewind`` changed, observed through the API."""

    rewound_id: str
    prompt: str
    # The source before the rewind, the rewound session, and the source after it.
    placed: dict[str, Any]
    rewound: dict[str, Any]
    source: dict[str, Any]
    source_notices: list[str]


def _rewind(client: httpx.Client, *, source_id: str, item_id: str) -> _Rewound:
    """Rewind *source_id* to *item_id* once its turn ends, recording placements around it."""
    # /rewind refuses a running turn.
    _wait_until_idle(client, session_id=source_id)
    # /rewind moves the runner to the new session, so capture it first.
    placed = client.get(f"/v1/sessions/{source_id}", timeout=30.0).json()
    resp = client.post(f"/v1/sessions/{source_id}/rewind", json={"item_id": item_id}, timeout=60.0)
    assert resp.status_code == 200, resp.text
    rewound_id = str(resp.json()["session_id"])
    items = client.get(
        f"/v1/sessions/{source_id}/items", params={"limit": 5, "order": "desc"}, timeout=30.0
    ).json()["data"]
    return _Rewound(
        rewound_id=rewound_id,
        prompt=str(resp.json()["prompt"]),
        placed=placed,
        rewound=client.get(f"/v1/sessions/{rewound_id}", timeout=30.0).json(),
        source=client.get(f"/v1/sessions/{source_id}", timeout=30.0).json(),
        source_notices=[_assistant_text(item) for item in items],
    )


def _placement(session: dict[str, Any]) -> tuple[Any, Any, Any]:
    """A session's runner, host and workspace."""
    return (session["runner_id"], session["host_id"], session["workspace"])
