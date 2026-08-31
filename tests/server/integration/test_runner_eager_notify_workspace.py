"""The eager post-create runner notify must carry the session workspace.

Guards against the notify-payload regression: after ``POST /v1/sessions``
creates a child session, the server eagerly notifies the bound runner so
it can initialize per-session state before the first turn. That notify
must send the versioned session-init envelope every other init path sends
(:func:`omnigent.runner.session_init_protocol.build_runner_session_init_payload`),
not the legacy ``{session_id, agent_id, sub_agent_name}`` triple.

The runner treats an envelope-less body as a legacy init: it resolves the
spec, spawns the harness, and caches session state WITHOUT the persisted
workspace snapshot. When the runner initializes from this payload first
(the eager notify races ahead of any later envelope-carrying init), the
child harness is spawned with the runner's default cwd even though the
session row carries an explicit workspace — the child then runs in its
configured default directory instead of the workspace assigned to it.

The test drives the real journey through the create route: register a
parent bundle declaring a ``worker`` sub-agent, create the named child
with an explicit workspace, and capture the exact HTTP body the server
posts to the runner. It asserts the notify carries the standard
session-init snapshot with that workspace.

Run::

    pytest tests/server/integration/test_runner_eager_notify_workspace.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.server.routes import sessions as sessions_module
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_eager_runner_notify_carries_child_workspace(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The post-create runner notify must include the child's workspace.

    Journey (mirrors the report's steps): a parent agent declares a
    ``worker`` sub-agent; a per-task directory exists; the named child
    is created through ``POST /v1/sessions`` with that directory as its
    workspace. The server persists the workspace on the child row and
    then eagerly notifies the runner about the new session.

    The runner-side initialization contract
    (``omnigent/runner/session_init_protocol.py``) reads the workspace
    from ``session_init.snapshot.workspace``; a body without the
    envelope takes the legacy path, which initializes and caches the
    session state without the persisted workspace. So the eager notify
    must carry the standard envelope — asserting on the captured body is
    asserting exactly what the runner will initialize from.

    A regression back to the bare legacy triple
    (``omnigent/server/routes/sessions/routes_core.py``) would make a
    runner that initializes from it cache the child session with no
    workspace and spawn the harness in its default directory.

    :param client: The test HTTP client (real app, real stores).
    :param monkeypatch: Pytest monkeypatch for the runner-client stub.
    :param tmp_path: Per-test temp dir for the project/task directories.
    """
    task_dir = tmp_path / "project" / "task-a"
    task_dir.mkdir(parents=True)

    agent = await create_test_agent(
        client,
        name="eager-notify-ws-parent",
        sub_agents=[{"name": "worker"}],
    )

    notified: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        """
        Capture the body the server posts to the (fake) runner.

        :param request: HTTP request sent to the fake runner.
        :returns: Accepted response.
        """
        notified.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"status": "initialized"})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_capture),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        """
        Route the eager notify to the fake runner.

        :returns: The capturing fake runner client.
        """
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        resp = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "parent_session_id": agent["_session_id"],
                "title": "worker:task-a",
                "sub_agent_name": "worker",
                "workspace": str(task_dir),
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 201, resp.text
    child = resp.json()

    # Server-side persistence works: the row carries the workspace.
    assert child.get("workspace") == str(task_dir), (
        f"child create did not persist the requested workspace: {child.get('workspace')!r}"
    )

    session_inits = [n for n in notified if n["path"] == "/v1/sessions"]
    assert session_inits, (
        "the server never posted the eager session notify to the runner — "
        "the create path changed; update this test's capture point."
    )
    notify_body = session_inits[-1]["body"]
    assert notify_body.get("session_id") == child["id"]

    # The actual regression: the eager notify must carry the standard
    # session-init snapshot (workspace included) so a runner that
    # initializes from it caches the child's workspace. Today the body
    # is the bare legacy triple and the snapshot is absent entirely.
    envelope = notify_body.get("session_init")
    assert isinstance(envelope, dict), (
        f"eager runner notify for child {child['id']} sent the LEGACY "
        f"payload {sorted(notify_body)!r} with no 'session_init' envelope — "
        f"a runner initializing from it caches the session without its "
        f"persisted workspace and spawns the harness in its default "
        f"directory instead of {str(task_dir)!r}."
    )
    snapshot = envelope.get("snapshot")
    assert isinstance(snapshot, dict) and snapshot.get("workspace") == str(task_dir), (
        f"eager runner notify's session-init snapshot lost the child "
        f"workspace: expected {str(task_dir)!r}, got "
        f"{snapshot.get('workspace') if isinstance(snapshot, dict) else snapshot!r}."
    )
