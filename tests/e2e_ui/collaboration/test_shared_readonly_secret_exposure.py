"""E2E: a shared read-only session must not expose workspace secrets.

A session's workspace routinely contains secret-bearing files (e.g. ``.env``).
Sharing the session view-only shares the *conversation*, not the raw
filesystem: without a gate, any secret file in the workspace is served
verbatim to a read-only collaborator, both through the Files surface in the
SPA and through the filesystem API the viewer uses.

The user journey this encodes:

1. The owner has a session whose workspace contains ``.env`` with a secret.
2. The owner shares the session read-only with a collaborator.
3. The collaborator opens the shared session with the secret file deep-linked
   — the raw secret value must not be displayed or served.

The assertions state the fixed behavior — the raw secret value must not be
rendered to (or served to) a read-only collaborator — so this test fails while
the exposure is live and passes once workspace reads require an edit-level
grant (or the contents are otherwise withheld from view-only collaborators).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, expect

from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    _ensure_runner_online,
    _server_state,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Permission level mirrored from omnigent/server/auth.py.
_LEVEL_READ = 1

# Deliberately fake but realistically-shaped secret material. The distinctive
# value is what the assertions key on: it must never reach a read-only viewer.
_SECRET_FILE = ".env"
_SECRET_VALUE = "sk-sharedview-FAKE-9f3b2c1d8e7a6f5049382716"
_SECRET_CONTENT = f"""\
# Local development credentials — do not share
OPENAI_API_KEY={_SECRET_VALUE}
AWS_SECRET_ACCESS_KEY=sharedviewFakeAwsSecretKey0123456789abcdef
"""

# The read-only composer placeholder a read-level collaborator sees
# (ChatPage.tsx) — proves the shared session loaded under Bob's identity
# regardless of how a future fix treats the Files surface.
_READONLY_PLACEHOLDER = "You have read-only access to this session"


@dataclass
class _SecretShareFixture:
    """A ``local``-owned session with a seeded secret, shared read-only.

    :param session_id: The runner-bound session id.
    :param bob: httpx client authenticated as the read-only collaborator.
    :param bob_email: Collaborator identity.
    """

    session_id: str
    bob: httpx.Client
    bob_email: str


@pytest.fixture
def secret_shared(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_SecretShareFixture]:
    """Owner-owned session + seeded ``.env`` secret + read-only grant to Bob.

    Mirrors the ``shared`` fixture in ``test_sharing_journey.py`` (headerless
    ``local`` owner — the runner-ownership rule requires it) plus a secret
    file seeded through the filesystem PUT endpoint and a read (view-only)
    grant for Bob.
    """
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    suffix = uuid.uuid4().hex[:6]
    # No keep-alive pooling: connections idle for the whole browser drive and
    # the spawned uvicorn closes them after ~5s, which flakes on reuse.
    no_pool = httpx.Limits(max_keepalive_connections=0)
    owner = httpx.Client(base_url=live_server, timeout=30.0, limits=no_pool)
    bob = httpx.Client(
        base_url=live_server,
        headers={"X-Forwarded-Email": f"bob-{suffix}@ui.test"},
        timeout=30.0,
        limits=no_pool,
    )
    create_resp = owner.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    owner.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": str(_server_state["runner_id"])},
    ).raise_for_status()
    # Seed the secret-bearing file into the session workspace (no agent run).
    owner.put(
        f"{live_server}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_SECRET_FILE}",
        json={"content": _SECRET_CONTENT, "encoding": "utf-8"},
    ).raise_for_status()
    # Share the session read-only with the collaborator.
    owner.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": bob.headers["X-Forwarded-Email"], "level": _LEVEL_READ},
    ).raise_for_status()
    try:
        yield _SecretShareFixture(
            session_id=session_id,
            bob=bob,
            bob_email=bob.headers["X-Forwarded-Email"],
        )
    finally:
        owner.delete(f"/v1/sessions/{session_id}")
        owner.close()
        bob.close()
        shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)
        # Restore the "found" state: if we respawned the runner (a prior test
        # had killed it), tear our copy down so it doesn't outlive us.
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def test_read_only_collaborator_cannot_see_raw_workspace_secrets(
    browser: Browser,
    live_server: str,
    secret_shared: _SecretShareFixture,
) -> None:
    """A read-only viewer of a shared session must not see raw secrets.

    Drives the collaborator's real journey: open the shared session with the
    secret file deep-linked (``?file=.env`` — the same viewer the Files panel
    opens), let the viewer settle, then assert the fixed behavior on both the
    UI surface and the filesystem API the viewer fetches through.
    """
    sid = secret_shared.session_id
    # The conftest ``_record_video`` hook only instruments the *async* API;
    # this test drives the sync API, so honor the record-dir contract here.
    ctx_kwargs: dict[str, object] = {}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        ctx_kwargs["record_video_dir"] = record_dir
    bob_ctx = browser.new_context(
        extra_http_headers={"X-Forwarded-Email": secret_shared.bob_email},
        **ctx_kwargs,
    )
    try:
        bob_page = bob_ctx.new_page()
        bob_page.goto(f"{live_server}/c/{sid}?file={_SECRET_FILE}")

        # The shared session loaded under Bob's read-only identity.
        readonly_composer = bob_page.get_by_placeholder(_READONLY_PLACEHOLDER)
        expect(readonly_composer).to_be_visible(timeout=30_000)

        # Give the deep-linked file viewer time to fetch and render whatever
        # it will show Bob for the secret file (also captures that state on
        # the recording). Deliberately not gated on the viewer being visible:
        # a fix may hide the Files surface from read-only viewers entirely.
        bob_page.wait_for_timeout(3_000)

        # ── Fixed behavior, UI surface ──────────────────────────────
        # The raw secret value must not be rendered anywhere on the page.
        assert bob_page.get_by_text(_SECRET_VALUE).count() == 0, (
            "Shared read-only session rendered the workspace secret verbatim "
            f"to the collaborator: {_SECRET_VALUE!r} is visible on the page "
            "(no secret scan/redaction, filesystem not hidden)."
        )

        # ── Fixed behavior, API boundary ────────────────────────────
        # The filesystem read the viewer relies on must not serve the raw
        # secret to a read-level grantee either (a UI-only fix would leave
        # the exposure one curl away).
        api = secret_shared.bob.get(
            f"/v1/sessions/{sid}/resources/environments/default/filesystem/{_SECRET_FILE}"
        )
        assert _SECRET_VALUE not in api.text, (
            "Filesystem API served the raw workspace secret to a read-only "
            f"collaborator (HTTP {api.status_code}): no secret scan/redaction "
            "and the file system is not hidden for shared viewers."
        )
    finally:
        bob_ctx.close()
