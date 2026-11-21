"""Optional live end-to-end test for the boxd managed-host lifecycle.

Targets an existing Omnigent server configured with ``sandbox.provider: boxd``.
The lifecycle test creates one managed session without an LLM prompt, waits for
the host and runner inside the boxd machine to connect, deletes the session and
checks the machine is deleted. With ``OMNIGENT_E2E_BOXD_PROMPT=1`` a second test
runs a real agent turn, then hibernates the machine and later stops it, and
checks each time that the next message wakes the same machine with the
workspace intact and exactly one host daemon; the server must forward an LLM
credential (``sandbox.boxd.env``) for that one.

``OMNIGENT_E2E_BOXD_SERVER_TOKEN`` is optional for auth-disabled servers and is
sent as an Omnigent server Bearer token when present.

    OMNIGENT_E2E_BOXD=1 \
    OMNIGENT_E2E_BOXD_SERVER_URL=https://omnigent.example.com \
    OMNIGENT_E2E_BOXD_AGENT_ID=ag_example \
    OMNIGENT_E2E_BOXD_SERVER_TOKEN=... \
    BOXD_API_KEY=bxd_... [BOXD_ORG=my-org] [OMNIGENT_E2E_BOXD_PROMPT=1] \
    .venv/bin/python -m pytest tests/e2e/integrations/deploy/boxd/test_boxd_managed_lifecycle.py -v
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from urllib.parse import quote

import httpx
import pytest

from omnigent.onboarding.sandboxes.boxd import (
    DEFAULT_LABELS,
    ORG_ENV_VAR,
    BoxdSandboxLauncher,
    machine_name_for,
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_BOXD") != "1",
        reason="set OMNIGENT_E2E_BOXD=1 to provision a live boxd machine",
    ),
    pytest.mark.timeout(1200),
]

_READY_TIMEOUT_S = 600.0
_TURN_TIMEOUT_S = 600.0
_RELEASE_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0
_PROOF_FILE = "boxd-e2e-proof.txt"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} must be set when OMNIGENT_E2E_BOXD=1")
    return value


def _server_headers() -> dict[str, str]:
    token = os.environ.get("OMNIGENT_E2E_BOXD_SERVER_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


@pytest.fixture
def client() -> Iterator[httpx.Client]:
    """A client for the target server that advertises boxd."""
    server_url = _required_env("OMNIGENT_E2E_BOXD_SERVER_URL").rstrip("/")
    _required_env("BOXD_API_KEY")
    with httpx.Client(base_url=f"{server_url}/", headers=_server_headers(), timeout=30.0) as http:
        info = http.get("/v1/info")
        info.raise_for_status()
        assert "boxd" in (info.json().get("sandbox_providers") or []), (
            "the target Omnigent server does not advertise boxd as a managed sandbox provider"
        )
        yield http


@pytest.fixture
def session_id(client: httpx.Client) -> Iterator[str]:
    """One managed boxd session, deleted at the end if the test did not."""
    response = client.post(
        "/v1/sessions",
        json={
            "agent_id": _required_env("OMNIGENT_E2E_BOXD_AGENT_ID"),
            "host_type": "managed",
            "sandbox_provider": "boxd",
            "title": f"boxd managed lifecycle E2E {int(time.time())}",
        },
    )
    response.raise_for_status()
    created = response.json()["id"]
    try:
        yield created
    finally:
        cleanup = client.delete(f"/v1/sessions/{quote(created, safe='')}", timeout=120.0)
        assert cleanup.status_code in {200, 404}, f"cleanup failed: HTTP {cleanup.status_code}"


def _snapshot(client: httpx.Client, session_id: str, *, items: bool = False) -> dict[str, object]:
    response = client.get(
        f"/v1/sessions/{quote(session_id, safe='')}",
        params={"include_items": "true" if items else "false"},
    )
    response.raise_for_status()
    return response.json()


def _wait_until_ready(client: httpx.Client, session_id: str) -> dict[str, object]:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    snapshot: dict[str, object] = {}
    while time.monotonic() < deadline:
        snapshot = _snapshot(client, session_id)
        sandbox_status = snapshot.get("sandbox_status") or {}
        if isinstance(sandbox_status, dict) and sandbox_status.get("stage") == "failed":
            pytest.fail(f"boxd managed launch failed: {sandbox_status.get('error')}")
        if snapshot.get("host_online") is True and snapshot.get("runner_online") is True:
            return snapshot
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"boxd host and runner did not become ready within {_READY_TIMEOUT_S:.0f}s; "
        f"last sandbox status was {snapshot.get('sandbox_status')!r}"
    )


def _machine_id(client: httpx.Client, host_id: object) -> str:
    """
    Resolve the session's machine from its host name.

    Machines are named ``<prefix>-<host name>-<suffix>`` and labelled
    ``created-by=omnigent``; matching on the middle part works whatever
    ``name_prefix`` the server is configured with.
    """
    from boxd import Boxd

    response = client.get(f"/v1/hosts/{quote(str(host_id), safe='')}")
    response.raise_for_status()
    # The slug the launcher derives from the host name, without prefix and suffix.
    slug = machine_name_for(str(response.json()["name"]), "x")[2:-7]
    pattern = re.compile(rf"^[a-z0-9-]+-{re.escape(slug)}-[0-9a-f]{{6}}$")
    with Boxd() as boxd_client:
        machines = boxd_client.machines.list(
            org=os.environ.get(ORG_ENV_VAR) or None, labels=DEFAULT_LABELS
        )
    matches = [machine.id for machine in machines if pattern.match(machine.name)]
    assert len(matches) == 1, f"expected one boxd machine for host {slug!r}, found {matches}"
    return matches[0]


def _host_daemons(launcher: BoxdSandboxLauncher, machine_id: str) -> list[str]:
    """The ``omnigent host`` processes in the machine (not their restart supervisor)."""
    listing = launcher.run(machine_id, "ps -eo args").stdout.splitlines()
    return [
        line
        for line in listing
        if " host --server" in line and not line.startswith(("sh ", "/bin/sh "))
    ]


def _settle(machine_id: str, status: str) -> None:
    """Wait until the machine reports *status*."""
    from boxd import Boxd

    deadline = time.monotonic() + 120
    with Boxd() as boxd_client:
        while boxd_client.machines.get(machine_id).status != status:
            assert time.monotonic() < deadline, f"machine never reached {status!r}"
            time.sleep(1.0)


def _run_turn(client: httpx.Client, session_id: str, text: str) -> str:
    """Send one user message and return the assistant's reply text."""
    seen = len(_snapshot(client, session_id, items=True).get("items") or [])
    response = client.post(
        f"/v1/sessions/{quote(session_id, safe='')}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
        timeout=300.0,
    )
    response.raise_for_status()
    deadline = time.monotonic() + _TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        snapshot = _snapshot(client, session_id, items=True)
        replies = [
            part.get("text", "")
            for item in (snapshot.get("items") or [])[seen:]
            if isinstance(item, dict) and (item.get("data") or {}).get("role") == "assistant"
            for part in (item.get("data") or {}).get("content") or []
            if isinstance(part, dict)
        ]
        if replies and snapshot.get("status") == "idle":
            return "\n".join(replies)
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"no assistant reply within {_TURN_TIMEOUT_S:.0f}s")


def _wait_until_deleted(launcher: BoxdSandboxLauncher, machine_id: str) -> None:
    deadline = time.monotonic() + _RELEASE_TIMEOUT_S
    while time.monotonic() < deadline:
        if launcher.is_running(machine_id) is False:
            return
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"boxd machine {machine_id!r} still running after session deletion")


def test_boxd_managed_session_lifecycle(client: httpx.Client, session_id: str) -> None:
    """A managed session provisions a resumable boxd host and deletes it with the session."""
    snapshot = _wait_until_ready(client, session_id)
    assert snapshot.get("host_resumable") is True
    machine_id = _machine_id(client, snapshot.get("host_id"))
    launcher = BoxdSandboxLauncher()
    assert launcher.is_running(machine_id) is True
    assert "omnigent host" in launcher.run(machine_id, "ps -eo args").stdout

    response = client.delete(f"/v1/sessions/{quote(session_id, safe='')}", timeout=120.0)
    response.raise_for_status()
    assert response.json()["deleted"] is True
    _wait_until_deleted(launcher, machine_id)


@pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_BOXD_PROMPT") != "1",
    reason="set OMNIGENT_E2E_BOXD_PROMPT=1 to run LLM turns on the boxd host",
)
def test_boxd_managed_host_wakes_in_place(client: httpx.Client, session_id: str) -> None:
    """An agent turn runs in the machine; a hibernated and then a stopped machine wake in place."""
    snapshot = _wait_until_ready(client, session_id)
    machine_id = _machine_id(client, snapshot.get("host_id"))
    launcher = BoxdSandboxLauncher()

    _run_turn(
        client,
        session_id,
        f"Use your shell tool to run `echo boxd-ok > ~/workspace/{_PROOF_FILE}`, "
        "then reply with the single word done.",
    )
    assert launcher.run(machine_id, f"cat ~/workspace/{_PROOF_FILE}").stdout == "boxd-ok\n"

    from boxd import Boxd

    for sleep_state in ("hibernated", "stopped"):
        with Boxd() as boxd_client:
            if sleep_state == "hibernated":
                boxd_client.machines.hibernate(machine_id)
            else:
                boxd_client.machines.stop(machine_id)
        _settle(machine_id, sleep_state)
        assert launcher.is_running(machine_id) is False

        reply = _run_turn(
            client,
            session_id,
            f"Use your shell tool to run `cat ~/workspace/{_PROOF_FILE}` "
            "and reply with its output.",
        )
        assert "boxd-ok" in reply, f"after {sleep_state}: {reply!r}"
        assert launcher.is_running(machine_id) is True
        # The same machine came back, and the daemon a wake preserved was stopped.
        assert _machine_id(client, snapshot.get("host_id")) == machine_id
        daemons = _host_daemons(launcher, machine_id)
        assert len(daemons) == 1, f"after {sleep_state}: {daemons}"
