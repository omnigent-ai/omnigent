"""E2E: a shared session's composer host badge names the host for collaborators.

The badge (``web/src/components/HostBadge.tsx``) resolves its label from
``GET /v1/hosts``, which lists only hosts the *viewer* owns. A collaborator
opening a session shared with them does not own the owner's host, so the
badge falls back to the raw ``host_id`` hex. The user-facing contract pinned
here: a collaborator sees the host's friendly name, same as the owner.

No route patching: a real host daemon registers against the live server under
a friendly name, the owner binds a session to it through the host launch flow
(``metadata.host_id`` on create) and grants a header-identified collaborator
edit access — the same identity split as ``test_sharing_journey``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Browser, expect

from tests.e2e_ui.conftest import _REPO_ROOT, _build_hello_world_bundle

# Permission level mirrored from omnigent/server/auth.py.
_LEVEL_EDIT = 2


@dataclass
class _HostBoundShared:
    """A host-bound session shared with a collaborator.

    :param session_id: The host-bound session id.
    :param host_id: Raw id of the connected host.
    :param host_name: Friendly name the host registered under.
    :param bob_email: Collaborator identity granted edit access.
    """

    session_id: str
    host_id: str
    host_name: str
    bob_email: str


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* reports online.

    :param client: Owner-identity HTTP client pointed at the server.
    :param host_id: Host id to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200 and any(
            h["host_id"] == host_id and h["status"] == "online"
            for h in resp.json().get("hosts", [])
        ):
            return
        time.sleep(0.5)
    raise AssertionError(f"host {host_id!r} never came online within {timeout}s")


def _wait_for_host_binding(
    client: httpx.Client, session_id: str, host_id: str, timeout: float
) -> None:
    """Poll the session snapshot until it carries *host_id*.

    :param client: Owner-identity HTTP client pointed at the server.
    :param session_id: Session whose binding to await.
    :param host_id: Expected bound host id.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the binding never lands.
    """
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        if resp.status_code == 200:
            last = resp.json().get("host_id")
            if last == host_id:
                return
        time.sleep(0.5)
    raise AssertionError(
        f"session {session_id!r} never bound to host {host_id!r} (last host_id={last!r})"
    )


@pytest.fixture
def host_bound_shared(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[_HostBoundShared]:
    """Connect a real host, bind a session to it, and share it with Bob.

    A host daemon registers headlessly (owned by the ``local`` user, same as
    the headerless browser/owner), a hello_world session is created with
    ``metadata.host_id`` so the server runs the real host launch flow, and
    the collaborator is granted edit via the permissions API.

    :param live_server: The spawned e2e server's base URL.
    :param mock_llm_server_url: Mock LLM base URL for the host's runners.
    :param tmp_path: Per-test dir for the daemon HOME, workspace, and log.
    :yields: The shared session's ids and identities.
    """
    host_id = uuid.uuid4().hex
    # Unique per test: the host store enforces a unique (owner, name) row on
    # the shared server.
    host_name = f"alices-macbook-{uuid.uuid4().hex[:6]}"
    home = tmp_path / "host-home"
    (home / ".omnigent").mkdir(parents=True)
    (home / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": host_name}})
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    daemon_log = tmp_path / "host-daemon.log"
    # Ambient OMNIGENT_* vars (a CI harness's OMNIGENT_CONFIG_HOME, runner
    # tunnel vars) would override the per-test identity, so drop them all.
    daemon_env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT")}
    daemon_env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "OPENAI_API_KEY": "mock-key",
        }
    )
    with open(daemon_log, "w") as log_fh:
        daemon = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=daemon_env,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )

    owner = httpx.Client(
        base_url=live_server,
        timeout=30.0,
        limits=httpx.Limits(max_keepalive_connections=0),
    )
    bob_email = f"bob-{uuid.uuid4().hex[:6]}@ui.test"
    session_id: str | None = None
    try:
        # Startup capability probing can hold the hello frame for a while on
        # a loaded CI runner, so allow well past the interactive norm.
        _wait_for_host_online(owner, host_id, timeout=120.0)
        create = owner.post(
            "/v1/sessions",
            data={"metadata": json.dumps({"host_id": host_id, "workspace": str(workspace)})},
            files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
            timeout=120.0,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        _wait_for_host_binding(owner, session_id, host_id, timeout=60.0)
        owner.put(
            f"/v1/sessions/{session_id}/permissions",
            json={"user_id": bob_email, "level": _LEVEL_EDIT},
        ).raise_for_status()
        yield _HostBoundShared(
            session_id=session_id,
            host_id=host_id,
            host_name=host_name,
            bob_email=bob_email,
        )
    finally:
        if session_id is not None:
            owner.delete(f"/v1/sessions/{session_id}")
        owner.close()
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)


def test_collaborator_host_badge_shows_host_name(
    browser: Browser,
    live_server: str,
    host_bound_shared: _HostBoundShared,
) -> None:
    """A collaborator's composer host badge names the host, not its raw id.

    The owner's view is asserted first as a control: the badge resolves the
    friendly name for the identity that owns the host, so a failure on the
    collaborator assertions below is specifically the shared-viewer
    resolution.

    :param browser: Session-scoped Playwright browser; two contexts stand in
        for the owner and the collaborator.
    :param live_server: The spawned e2e server's base URL.
    :param host_bound_shared: Host-bound session shared with the collaborator.
    """
    sid = host_bound_shared.session_id
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    name_re = re.compile(re.escape(host_bound_shared.host_name))

    owner_ctx = browser.new_context(record_video_dir=record_dir)
    bob_ctx = browser.new_context(
        extra_http_headers={"X-Forwarded-Email": host_bound_shared.bob_email},
        record_video_dir=record_dir,
    )
    owner_page = bob_page = None
    try:
        owner_page = owner_ctx.new_page()
        owner_page.goto(f"{live_server}/c/{sid}")
        owner_badge = owner_page.get_by_test_id("composer-host-select")
        expect(owner_badge).to_be_visible(timeout=30_000)
        expect(owner_badge).to_have_attribute("aria-label", name_re, timeout=30_000)

        bob_page = bob_ctx.new_page()
        bob_page.goto(f"{live_server}/c/{sid}")
        bob_badge = bob_page.get_by_test_id("composer-host-select")
        expect(bob_badge).to_be_visible(timeout=30_000)
        # The binding reached the page (label switches off "No host bound")
        # before the name assertions, so a failure below is name resolution,
        # not a slow snapshot.
        expect(bob_badge).to_have_attribute("aria-label", re.compile(r"^Host "), timeout=30_000)

        # The host menu row is where the user reads the label text.
        bob_badge.click()
        menu = bob_page.get_by_test_id("composer-host-menu")
        expect(menu).to_be_visible(timeout=15_000)
        expect(menu).to_contain_text(host_bound_shared.host_name, timeout=15_000)
        expect(bob_badge).to_have_attribute("aria-label", name_re)
        expect(bob_badge).not_to_have_attribute(
            "aria-label", re.compile(re.escape(host_bound_shared.host_id))
        )
    finally:
        owner_ctx.close()
        bob_ctx.close()
        # Hash-named videos are indistinguishable by role; label them so the
        # recording workflow can pick the collaborator's clip.
        if record_dir:
            for role, page in (("owner", owner_page), ("collaborator", bob_page)):
                if page is not None and page.video:
                    Path(page.video.path()).rename(Path(record_dir) / f"{role}.webm")
