"""Browser e2e: forking a coding session as a *different* user.

A coding (workspace-bound) session owned by one user, shared read to another
and forked by that other user, lands an *unbound* clone. Opening the clone and
trying to continue routes the forking user into ``ResumeWithDirectoryDialog``,
which is meant to bind the clone to a host + directory. But its host prefill and
CLI fallback are hard-wired to the SOURCE session's host: the forking user —
who neither owns nor can even see that host (``GET /v1/hosts`` is owner-scoped)
— is shown the "the original session's host is offline, reconnect it from your
terminal" fallback (``resume-dir-cli-fallback``). That is a dead end: they
cannot reconnect a machine they don't own, and the dialog offers no path to run
the clone on their own machine.

Same-user / same-machine forks bind fine (why Claude Code appeared to work);
the discriminator is the cross-user + workspace binding, not the harness. The
source here is the real Polly (claude-sdk orchestrator) bundle, matching the
ticket.

Expected fix: when the fork source's host is not one the forking user can reach
(a different owner / not in their host list), the resume dialog should guide
them to their OWN machine — here, with no host of their own online, the
"start one with ``omnigent host``" guidance (``resume-dir-no-hosts``) — instead
of the reconnect-the-original-host CLI fallback. Without the fix this test stops
at the ``resume-dir-no-hosts`` assertion (the CLI fallback is shown instead).
"""

from __future__ import annotations

import io
import json as _json
import os
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    MultiUserServer,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import _REPO_ROOT

ALICE_EMAIL = "alice@ui.test"
BOB_EMAIL = "bob@ui.test"
_ALICE_HEADERS = {"X-Forwarded-Email": ALICE_EMAIL}
_LEVEL_READ = 1
# The state a bound Polly runner leaves: a workspace on alice's host. 32-hex
# so it parses as the server's Uuid16 host id. bob owns no host, so alice's is
# invisible (and offline) from his side.
_ALICE_HOST_ID = uuid.uuid4().hex
_ALICE_WORKSPACE = "/home/alice/projects/app"
_FORK_SOURCE_LABEL = "omnigent.fork.source_id"


def _build_polly_bundle() -> bytes:
    """Tar ``examples/polly`` — the real claude-sdk coding orchestrator."""
    src = _REPO_ROOT / "examples" / "polly"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            tar.add(path, arcname=str(path.relative_to(src)))
    return buf.getvalue()


@pytest.fixture(scope="module")
def polly_source(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[MultiUserServer, str]]:
    """Multi-user server + alice's shared, workspace-bound Polly session.

    alice creates a Polly session, it is seeded as a coding session (workspace
    + host on alice's machine — the state a bound runner leaves) and shared
    read to bob. Yields ``(server, source_id)``.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    server_tmp = tmp_path_factory.mktemp("e2e_ui_fork_cross_user")
    db_path: Path = server_tmp / "test.db"
    gen = spawn_multi_user_server(mock_llm_server_url, server_tmp)
    server = next(gen)
    try:
        create = httpx.post(
            f"{server.base_url}/v1/sessions",
            data={"metadata": _json.dumps({})},
            files={"bundle": ("polly.tar.gz", _build_polly_bundle(), "application/gzip")},
            headers=_ALICE_HEADERS,
            timeout=120.0,
        )
        create.raise_for_status()
        source_id = create.json()["session_id"]

        # Seed the workspace + host with the same store method the server's
        # runner-bind uses: a loopback runner registers as the reserved
        # "local" user, so it cannot bind alice's session here. Retry past the
        # transient sqlite lock while the server holds the db.
        store = SqlAlchemyConversationStore(f"sqlite:///{db_path}")
        for _ in range(10):
            try:
                store.set_host_id(source_id, _ALICE_HOST_ID, workspace=_ALICE_WORKSPACE)
                break
            except Exception:  # noqa: BLE001 — sqlite "database is locked" retry
                time.sleep(0.5)
        else:
            raise RuntimeError("could not seed the source workspace/host")

        httpx.put(
            f"{server.base_url}/v1/sessions/{source_id}/permissions",
            json={"user_id": BOB_EMAIL, "level": _LEVEL_READ},
            headers=_ALICE_HEADERS,
            timeout=10.0,
        ).raise_for_status()

        yield server, source_id
    finally:
        gen.close()


def test_cross_user_fork_offers_own_machine(
    polly_source: tuple[MultiUserServer, str],
    browser: Browser,
) -> None:
    server, source_id = polly_source

    # This test drives the sync API, which the conftest's OMNIGENT_E2E_RECORD_DIR
    # hook (async-only) does not instrument; wire the video dir explicitly so the
    # recorder films the journey. No-op in ordinary runs (env unset).
    context_kwargs: dict = {"extra_http_headers": {"X-Forwarded-Email": BOB_EMAIL}}
    if record_dir := os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        context_kwargs["record_video_dir"] = record_dir
    bob = browser.new_context(**context_kwargs)
    try:
        # bob owns no host, and the list is owner-scoped, so alice's machine is
        # invisible to him — he can neither pick it nor launch on it.
        hosts = bob.request.get(f"{server.base_url}/v1/hosts")
        assert hosts.ok, hosts.status
        assert hosts.json()["hosts"] == []

        # bob forks the shared coding session. The empty-body fork is the exact
        # call the SPA's forkSession() issues; the server returns an idle,
        # *unbound* clone carrying the fork-source label.
        fork = bob.request.post(
            f"{server.base_url}/v1/sessions/{source_id}/fork",
            data={},
        )
        assert fork.ok, f"{fork.status} {fork.text()}"
        fork_body = fork.json()
        fork_id = fork_body["id"]
        assert fork_body.get("workspace") in (None, ""), fork_body.get("workspace")
        assert fork_body.get("host_id") in (None, ""), fork_body.get("host_id")
        assert fork_body.get("labels", {}).get(_FORK_SOURCE_LABEL) == source_id

        # bob opens the clone and tries to continue → the offline banner routes
        # to the resume picker. Loopback base URL (not the public alias) so the
        # sandbox egress proxy is bypassed; the resume flow under test never
        # reads isCurrentServerLocal(), so the host does not change behavior.
        page = bob.new_page()
        page.goto(f"{server.base_url}/c/{fork_id}")

        disconnected = page.get_by_test_id("disconnected-indicator")
        expect(disconnected).to_be_visible(timeout=60_000)
        disconnected.click()

        dialog = page.get_by_test_id("resume-dir-dialog")
        expect(dialog).to_be_visible()
        # Let the dialog resolve the source + host lists before asserting which
        # branch it settled on.
        expect(page.get_by_test_id("resume-dir-loading")).to_have_count(0)

        # Correct behavior: with no machine of his own online, bob is guided to
        # start one — the resume flow points at HIS compute, not alice's.
        expect(page.get_by_test_id("resume-dir-no-hosts")).to_be_visible()

        # The bug: instead, bob is dead-ended on the CLI fallback that tells him
        # to reconnect the ORIGINAL (alice's) host — a machine he neither owns
        # nor can reach.
        expect(page.get_by_test_id("resume-dir-cli-fallback")).to_have_count(0)
        expect(
            page.get_by_text("The original session's host is offline")
        ).to_have_count(0)
    finally:
        bob.close()
