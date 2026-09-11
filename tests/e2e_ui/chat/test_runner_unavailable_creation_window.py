"""E2E: the first message in a session whose runner never became available.

Reproduction of the "runner unavailable during the session creation window"
failure mode. A native-terminal (Claude Code wrapper) session is
created the same way ``omnigent claude`` creates one, but its runner never
connects — in production the host launched a runner that failed to start; here
the session simply never gets a runner bound, which is the same
creation-window state the server's dispatch path sees. The user then sends
their first message from the web composer.

The journey must settle in the reported failure, handled loudly and durably:

1. the send is accepted — the user's message is consumed, not dropped/5xx'd;
2. the chat renders a live error pill without a reload whose collapsed
   headline names the failure ("The session's runner failed to start on the
   host.", not the generic "Something went wrong"), and the expanded pill
   carries the actionable reason — the exact ``runner_failed_to_start``
   message ("The runner for this session is not available — it may have
   failed to start. See the host logs."), the same text the server logs as
   ``session turn failed for <id>: ...``;
3. the turn settles ``failed`` server-side with a durable
   ``runner_failed_to_start`` error item;
4. both the message and the error survive a reload (durable history, not a
   transient toast).

While the creation-window failure handling is intact this test passes; any
regression that drops the message, hides the failure behind a generic
headline, or loses the structured ``runner_failed_to_start`` reason fails it.
"""

from __future__ import annotations

import io
import json
import tarfile
import tempfile
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

# The exact user-facing text the server persists (and logs as
# ``session turn failed for <id>: ...``) when a native session's runner is
# unreachable and the host daemon reported no more specific exit cause. Keep
# in sync with the offline-error branch in
# omnigent/server/routes/sessions/routes_events.py.
_RUNNER_UNAVAILABLE_MESSAGE = (
    "The runner for this session is not available — "
    "it may have failed to start. See the host logs."
)

# The collapsed error-pill headline for the ``runner_failed_to_start`` code.
# Keep in sync with FAILURE_CODE_DESCRIPTIONS (web StatusBlocks.tsx and
# omnigent/runner/launch_failure.py).
_RUNNER_UNAVAILABLE_HEADLINE = "The session's runner failed to start on the host."

_MESSAGE = "hello from the creation window"


def _create_native_claude_session_without_runner(base_url: str) -> str:
    """Create the ``claude-native`` wrapper session with no runner bound.

    Mirrors the conftest ``_create_native_claude_session`` factory (the exact
    terminal-first spec + wrapper labels ``omnigent claude`` ships) but skips
    the runner bind: the session is left in the creation-window state where
    its runner never connected — how a session looks when the host-launched
    runner failed to start.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_claude_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the conftest native session factories.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _ensure_chat_view(page: Page) -> None:
    """Switch a terminal-first (native) session to its chat bubble view.

    The toggle only exists for terminal-capable sessions; when it is absent
    the session is already on the chat surface, so this is a no-op.

    :param page: The Playwright page, on the session's surface.
    """
    if page.get_by_test_id("view-mode-toggle").count() == 0:
        return
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def test_first_message_fails_loudly_when_runner_never_becomes_available(
    page: Page,
    live_server: str,
) -> None:
    """A creation-window message settles as a visible, durable failed turn.

    :param page: Playwright page fixture.
    :param live_server: Spawned server base URL; its runner is deliberately
        NOT bound to the session under test.
    :returns: None.
    """
    session_id = _create_native_claude_session_without_runner(live_server)
    try:
        page.goto(f"{live_server}/c/{session_id}")
        _ensure_chat_view(page)

        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        expect(composer).to_be_enabled(timeout=30_000)
        composer.fill(_MESSAGE)
        page.get_by_role("button", name="Send", exact=True).click()

        # The user's message is consumed into the transcript, not dropped.
        expect(page.get_by_text(_MESSAGE).first).to_be_visible(timeout=15_000)

        # The failure surfaces live (no reload): the error pill renders from
        # the session stream, its collapsed headline names the failure (not
        # the generic fallback), and expanding it shows the actionable reason.
        pill = page.get_by_test_id("error-pill").first
        expect(pill).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-headline").first).to_contain_text(
            _RUNNER_UNAVAILABLE_HEADLINE
        )
        pill.click()
        expect(page.get_by_text(_RUNNER_UNAVAILABLE_MESSAGE).first).to_be_visible(timeout=10_000)

        # Server-side the turn settled failed with the structured reason.
        snap = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        snap.raise_for_status()
        assert snap.json()["status"] == "failed", snap.text
        items_resp = httpx.get(
            f"{live_server}/v1/sessions/{session_id}/items",
            params={"limit": 100},
            timeout=10.0,
        )
        items_resp.raise_for_status()
        items = items_resp.json()["data"]
        error_items = [i for i in items if i["type"] == "error"]
        assert [i.get("code") for i in error_items] == ["runner_failed_to_start"], items
        assert _RUNNER_UNAVAILABLE_MESSAGE in (error_items[0].get("message") or ""), error_items

        # Durability: both the message and the failure survive a reload.
        page.reload()
        expect(page.get_by_text(_MESSAGE).first).to_be_visible(timeout=30_000)
        expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=30_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
