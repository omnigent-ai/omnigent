"""E2E guard: the environment shell proxy must be owner-only.

Sharing a session at ``edit`` lets a collaborator send messages and touch
files *inside* the shared workspace, but the runner runs on the owner's own
machine. The filesystem proxy already enforces this split — a relative path is
``edit``, an absolute path (``/etc/passwd``, ``~/.ssh/id_rsa``) needs
``owner``. The shell proxy at
``POST /v1/sessions/{id}/resources/environments/{env}/shell`` skipped that rule
and gated at ``edit``, so an edit collaborator could run any command on the
owner's host — reading their secrets or executing arbitrary code — bypassing
every policy/approval gate the agent path enforces.

This drives the real cross-user HTTP journey against a live server + runner:
the owner shares an executing, runner-bound session at ``edit``, and the
collaborator hits the raw shell endpoint. The endpoint runs commands on the
owner's runner (proven by the owner's own call returning real ``stdout``), so a
``403`` for the editor is decisive: the command never reaches that shell.

Run::

    pytest tests/e2e/test_environment_shell_owner_gate_e2e.py --llm-api-key mock -v
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    register_inline_agent,
    reset_mock_llm,
)

# Mirrored from omnigent/server/auth.py rather than imported, so a
# server-side renumbering trips these tests instead of tracking silently.
_LEVEL_READ = 1
_LEVEL_EDIT = 2


def _client_for(base_url: str, email: str) -> httpx.Client:
    """An httpx client authenticated as *email* via header identity."""
    return httpx.Client(
        base_url=base_url,
        headers={"X-Forwarded-Email": email},
        timeout=300,
    )


@dataclass
class _OwnedShellSession:
    """A runner-bound session owned by the headerless ``local`` identity.

    The owner must be ``local`` because the fixture runner is owned by the
    identity that registered it (headerless), and runner-ownership forbids
    binding a session to another user's runner.
    """

    owner: httpx.Client
    session_id: str
    shell_url: str


@pytest.fixture
def owned_shell_session(
    live_server: str,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> Iterator[_OwnedShellSession]:
    """A runner-bound session whose ``default`` environment shell executes.

    Polls the owner's own shell call until the runner is attached (a session
    with no connected runner returns 502), so collaborator assertions run
    against a shell that can actually reach the owner's host.
    """
    suffix = uuid.uuid4().hex[:6]
    owner = httpx.Client(base_url=live_server, timeout=300)
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        owner,
        name=f"shell-owner-gate-{suffix}",
        harness="openai-agents",
        model=f"mock-shell-gate-{suffix}",
        profile="",
        prompt="You are a terse assistant. Follow instructions exactly.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
        # An explicit unsandboxed environment so the runner materializes a
        # real cwd and the shell actually executes on the owner's host.
        extra_config={
            "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}
        },
    )
    session_id = create_runner_bound_session(
        owner, agent_name=agent_name, runner_id=live_runner_id
    )
    shell_url = f"/v1/sessions/{session_id}/resources/environments/default/shell"

    deadline = time.time() + 120
    last: httpx.Response | None = None
    while time.time() < deadline:
        last = owner.post(shell_url, json={"command": "echo runner_ready"})
        if last.status_code == 200:
            break
        # 502/404 == runner/environment not attached yet; keep polling.
        assert last.status_code in (404, 502), last.text
        time.sleep(1.0)
    assert last is not None and last.status_code == 200, (
        f"runner never attached: {last.status_code if last else 'no response'} "
        f"{last.text if last else ''}"
    )
    assert last.json()["stdout"].strip() == "runner_ready"

    yield _OwnedShellSession(owner=owner, session_id=session_id, shell_url=shell_url)
    owner.close()


def test_editor_cannot_run_environment_shell(
    live_server: str, owned_shell_session: _OwnedShellSession
) -> None:
    """An EDIT collaborator must be rejected before the command reaches the
    runner. On the vulnerable build the shell gates at EDIT, so Bob gets 200
    and the command's ``stdout`` back — arbitrary code on the owner's host."""
    sid = owned_shell_session.session_id
    marker = f"pwned-{uuid.uuid4().hex[:8]}"
    with _client_for(live_server, f"bob-{uuid.uuid4().hex[:6]}@e2e.test") as bob:
        owned_shell_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": bob.headers["X-Forwarded-Email"], "level": _LEVEL_EDIT},
        ).raise_for_status()

        resp = bob.post(owned_shell_session.shell_url, json={"command": f"echo {marker}"})

        assert resp.status_code == 403, (
            f"edit collaborator ran shell on the owner's host: {resp.status_code} {resp.text}"
        )
        # Decisive: the editor never received command output from the runner.
        assert marker not in resp.text


def test_reader_cannot_run_environment_shell(
    live_server: str, owned_shell_session: _OwnedShellSession
) -> None:
    """A READ collaborator is likewise rejected; the shell stays owner-scoped."""
    sid = owned_shell_session.session_id
    with _client_for(live_server, f"carol-{uuid.uuid4().hex[:6]}@e2e.test") as carol:
        owned_shell_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": carol.headers["X-Forwarded-Email"], "level": _LEVEL_READ},
        ).raise_for_status()

        resp = carol.post(owned_shell_session.shell_url, json={"command": "echo nope"})
        assert resp.status_code == 403, resp.text


def test_owner_can_run_environment_shell(
    owned_shell_session: _OwnedShellSession,
) -> None:
    """Positive control: the owner runs the shell and gets real ``stdout``.

    Proves the editor 403 is an authorization gate, not a broken endpoint —
    the same call from the owner reaches the runner and executes verbatim."""
    marker = f"owner-ok-{uuid.uuid4().hex[:8]}"
    resp = owned_shell_session.owner.post(
        owned_shell_session.shell_url, json={"command": f"echo {marker}"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stdout"].strip() == marker
    assert body["exit_code"] == 0
