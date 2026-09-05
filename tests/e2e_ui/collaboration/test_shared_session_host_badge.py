"""E2E: a shared session's host badge must show the host NAME.

Journey: the owner connects a named host and binds a session to it, shares
the session with a collaborator, and the collaborator opens it. The composer
status line's host badge (``web/src/components/HostBadge.tsx``) should answer
"which machine is this session on" with the host's friendly name — but for
the shared viewer it shows the raw ``host_id`` hex instead.

Mechanism (root-cause lead, not what this test drives): the badge resolves
the name via ``GET /v1/hosts``, which lists only hosts OWNED BY THE CALLER
(``host_store.list_hosts(user_id)`` in ``omnigent/server/routes/hosts.py``).
A shared session is bound to the *owner's* host, which is never in the
viewer's list, so ``resolveHostBadge`` falls back to the raw id — a fallback
``HostBadge.tsx`` explicitly documents for "shared session" and the unit
tests pin. The session snapshot carries ``host_id`` but no host name, so the
viewer has no way to resolve it.

Test shape
----------
Real user path end to end — no route interception on the surface under test:

- A dedicated multi-user server (sharing needs non-single-user auth), from
  ``_multi_user_server.spawn_multi_user_server``.
- A REAL host on the genuine WS tunnel (``/v1/hosts/{id}/tunnel``): the test
  connects as the admin identity, sends ``host.hello`` with a friendly name,
  and answers ``host.stat`` / ``host.launch_runner`` frames — the same
  lightweight-real-host pattern as
  ``tests/server/integration/test_host_session_binding.py``, but over a real
  socket against the spawned server.
- The admin-owned session is bound to that host through the real launch
  route (``POST /v1/hosts/{id}/runners``), so ``host_id`` reaches the
  session row via the production path.
- The session is shared with Bob (edit); Bob's browser context opens it.

The final assertions pin the CORRECT behavior — the badge names the host —
so this test FAILS on the bug (badge shows the raw hex id) and passes once
shared-session host-name resolution is fixed.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Browser, expect
from websockets.sync.client import connect as ws_connect

from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.identity import token_bound_runner_id
from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)

# Friendly name the owner's host announces in its hello frame — what the
# badge must display. Distinctive so raw-id fallback can't accidentally match.
_HOST_NAME = "alices-macbook"
_BOB_EMAIL = "bob-shared-host@ui.test"
# Permission levels mirrored from omnigent/server/auth.py.
_LEVEL_EDIT = 2
# Workspace the launch binds; the fake host stats it as an existing directory.
_WORKSPACE = "/work/shared-host-badge"


@pytest.fixture
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A dedicated NON-single-user server (sharing enabled) + admin session."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_shared_host_badge")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


class _FakeHost:
    """A minimal REAL host on the genuine ``/v1/hosts/{id}/tunnel`` WebSocket.

    Registers under the admin identity with a friendly name, then answers the
    two frames the runner-launch path sends: ``host.stat`` (workspace
    validation — always "exists, directory") and ``host.launch_runner``
    (always "launched", echoing the token-derived runner id). Runner-tunnel
    keepalive frames (ping) are ignored; the test finishes well inside the
    server's 90s liveness window.
    """

    def __init__(self, base_url: str, name: str) -> None:
        self.host_id = uuid.uuid4().hex
        ws_url = base_url.replace("http://", "ws://", 1)
        self._ws = ws_connect(
            f"{ws_url}/v1/hosts/{self.host_id}/tunnel",
            additional_headers={"X-Forwarded-Email": ADMIN_EMAIL},
        )
        self._ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version="0.1.0-e2e",
                    frame_protocol_version=1,
                    name=name,
                )
            )
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="shared-host-badge-fake-host", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self._ws.recv(timeout=1.0)
            except TimeoutError:
                continue
            except Exception:  # socket closed on teardown
                return
            if not isinstance(raw, str):
                continue
            try:
                frame = decode_host_frame(raw)
            except ValueError:
                # Runner-tunnel keepalive (ping) shares the socket; skip it.
                continue
            if isinstance(frame, HostStatFrame):
                self._ws.send(
                    encode_host_frame(
                        HostStatResultFrame(
                            request_id=frame.request_id,
                            status="ok",
                            exists=True,
                            type="directory",
                            canonical_path=frame.path,
                        )
                    )
                )
            elif isinstance(frame, HostLaunchRunnerFrame):
                self._ws.send(
                    encode_host_frame(
                        HostLaunchRunnerResultFrame(
                            request_id=frame.request_id,
                            status="launched",
                            runner_id=token_bound_runner_id(frame.binding_token),
                        )
                    )
                )

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):  # already closed
            self._ws.close()
        self._thread.join(timeout=5)


def _wait_for_named_host(base_url: str, host_id: str, timeout_s: float = 10.0) -> dict:
    """Poll ``GET /v1/hosts`` (as admin) until *host_id* appears; return its row.

    The hello frame's ``upsert_on_connect`` runs just after the WS connect
    returns, so an immediate list can race it.
    """
    deadline = time.monotonic() + timeout_s
    last: list[dict] = []
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{base_url}/v1/hosts",
            headers={"X-Forwarded-Email": ADMIN_EMAIL},
            timeout=10.0,
        )
        resp.raise_for_status()
        last = resp.json()["hosts"]
        for row in last:
            if row["host_id"] == host_id:
                return row
        time.sleep(0.25)
    raise AssertionError(f"host {host_id} never appeared in /v1/hosts: {last}")


def test_shared_session_host_badge_shows_host_name(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """The collaborator's host badge names the host, not its raw id.

    Fails on the bug: the badge for a shared session renders the raw
    ``host_id`` hex because the viewer's ``/v1/hosts`` list (owner-scoped)
    can't resolve the owner's host record to a name.

    :param browser: pytest-playwright browser (has the public-loopback
        host-resolver mapping from the shared launch args).
    :param multi_user_server: Spawned multi-user server + admin session.
    """
    base_url = multi_user_server.base_url
    session_id = multi_user_server.session_id
    admin_headers = {"X-Forwarded-Email": ADMIN_EMAIL}

    host = _FakeHost(base_url, _HOST_NAME)
    bob_ctx = None
    try:
        # The server knows the host's friendly name (owner's view resolves) —
        # so the failure below is specifically the shared viewer's resolution.
        row = _wait_for_named_host(base_url, host.host_id)
        assert row["name"] == _HOST_NAME, row

        # Bind the admin session to the host through the REAL launch route,
        # so host_id reaches the session row via the production path.
        launch = httpx.post(
            f"{base_url}/v1/hosts/{host.host_id}/runners",
            json={"session_id": session_id, "workspace": _WORKSPACE},
            headers=admin_headers,
            timeout=30.0,
        )
        assert launch.status_code == 200, f"launch failed: {launch.status_code} {launch.text}"

        snapshot = httpx.get(
            f"{base_url}/v1/sessions/{session_id}",
            headers=admin_headers,
            timeout=10.0,
        )
        snapshot.raise_for_status()
        assert snapshot.json().get("host_id") == host.host_id, snapshot.json()

        # Share with Bob at edit level (the ordinary collaborator shape).
        httpx.put(
            f"{base_url}/v1/sessions/{session_id}/permissions",
            json={"user_id": _BOB_EMAIL, "level": _LEVEL_EDIT},
            headers=admin_headers,
            timeout=10.0,
        ).raise_for_status()

        # Bob opens the shared session. Explicit record_video_dir so the
        # journey is filmed even though this test opens its own sync context
        # (the conftest injector only patches the async API).
        record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
        ctx_kwargs: dict = {"extra_http_headers": {"X-Forwarded-Email": _BOB_EMAIL}}
        if record_dir:
            ctx_kwargs["record_video_dir"] = record_dir
        bob_ctx = browser.new_context(**ctx_kwargs)
        page = bob_ctx.new_page()
        page.goto(f"{multi_user_server.public_url}/c/{session_id}")

        badge = page.get_by_test_id("host-badge")
        expect(badge).to_be_visible(timeout=15_000)
        # THE BUG: for the shared viewer the badge shows the raw host_id hex
        # instead of the host's name. These two assertions pin the fix.
        expect(badge).to_contain_text(_HOST_NAME, timeout=15_000)
        expect(badge).not_to_contain_text(host.host_id)
    finally:
        # Close the context even on failure so the video finalizes.
        if bob_ctx is not None:
            bob_ctx.close()
        host.close()
