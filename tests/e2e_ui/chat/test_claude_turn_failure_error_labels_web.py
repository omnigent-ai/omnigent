"""E2E-UI regression + recording driver: the SPA failure card on a
claude-native session must show the turn-failure detail and must not attribute
a Claude failure to Codex.

Both facets are user-visible on the chat failure card (``error-pill``), which
renders the persisted turn-failure detail:

* Facet B (Codex misattribution): a claude-native session's failed turn must not
  render a failure card headed "Codex ran into an error during this turn.".
  Buggy behavior: it does -- a Claude session's failure attributed to Codex.
* Facet A (category lost): when Claude's StopFailure hook carried an error
  category, the failure card must surface a cause. Buggy behavior: the bridge
  drops the category and the forwarder posts a bare ``failed`` with no output,
  so no failure card appears at all.

These assert the DESIRED behavior, so they FAIL on the buggy build (and film the
buggy state on the way) and PASS once the fix lands. The tightest deterministic
guard on the persisted ``last_task_error`` lives in
``tests/e2e/test_claude_turn_failure_error_labels_e2e.py``; these drive
the same two symptoms on the web surface the user sees.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tarfile
import tempfile
import threading
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

_STOP_FAILURE_CATEGORY = "authentication_failed"
_CLAUDE_FAILURE_OUTPUT = "Claude Code hit an unrecoverable error while completing this turn."
_CODEX_MISLABEL_HEADLINE = "Codex ran into an error"


def _create_claude_native_session(base_url: str) -> str:
    """Create an unbound claude-native wrapper session (no runner launch).

    Stamps the same wrapper / terminal-first labels ``omnigent claude`` writes so
    the session is a real claude-native conversation; it is left unbound so no
    real Claude TUI launches and competes with the failure this test drives.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
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


def _publish_native_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str,
    output: str | None = None,
) -> None:
    """Post the external_session_status a native forwarder POSTs to the server."""
    data: dict[str, object] = {"status": status, "response_id": response_id}
    if output is not None:
        data["output"] = output
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=10.0,
    )
    resp.raise_for_status()


def _open_chat_view(page: Page, base_url: str, session_id: str) -> None:
    """Navigate to the session and ensure the chat (not terminal) view is shown."""
    page.goto(f"{base_url}/c/{session_id}")
    chat_toggle = page.get_by_role("button", name="Chat view")
    if chat_toggle.count() > 0:
        chat_toggle.first.click()


def _seed_stop_failure_hook(bridge_dir: Path) -> None:
    """Seed a native Claude bridge with a StopFailure hook carrying a category.

    Records a ``SessionStart`` then a ``StopFailure`` hook whose ``error`` field
    carries the Claude error category, exactly as the live CLI writes on an
    errored turn, with an empty transcript (no persisted explanation).
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session-stopfailure",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "claude-session-stopfailure",
            "error": _STOP_FAILURE_CATEGORY,
            "transcript_path": str(transcript_path),
        },
    )


async def _drive_forwarder_through_stop_failure(
    base_url: str, session_id: str, bridge_dir: Path
) -> None:
    """Run the real forwarder loop over the seeded StopFailure hook."""
    import omnigent.harnesses.claude_native.forwarder as fwd

    task = asyncio.create_task(
        fwd.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id=session_id,
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.02,
        )
    )
    try:
        await asyncio.sleep(5.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _drive_forwarder_in_thread(base_url: str, session_id: str, bridge_dir: Path) -> None:
    """Run the async forwarder drive on its own loop/thread.

    The Playwright sync fixtures hold a running event loop in this thread, so
    the forwarder coroutine runs on a dedicated thread with a fresh loop.
    """
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                _drive_forwarder_through_stop_failure(base_url, session_id, bridge_dir)
            )
        except BaseException as exc:  # surfaced on the main thread below
            error["exc"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if "exc" in error:
        raise error["exc"]


def test_claude_native_failure_card_is_not_labeled_as_codex(
    page: Page,
    live_server: str,
) -> None:
    """Facet B: a Claude session's failure card must not read as a Codex error."""
    base_url = live_server
    session_id = _create_claude_native_session(base_url)

    _open_chat_view(page, base_url, session_id)

    _publish_native_status(base_url, session_id, "running", response_id="claude_turn_fail")
    _publish_native_status(
        base_url,
        session_id,
        "failed",
        response_id="claude_turn_fail",
        output=_CLAUDE_FAILURE_OUTPUT,
    )

    pill = page.get_by_test_id("error-pill")
    expect(pill).to_have_count(1, timeout=20_000)
    expect(pill).not_to_contain_text(_CODEX_MISLABEL_HEADLINE, timeout=5_000)


def test_claude_native_stop_failure_surfaces_its_category(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Facet A: a StopFailure category must reach a failure card on the web view.

    Drives the real forwarder over a StopFailure hook carrying an
    ``authentication_failed`` category, then opens the chat. Buggy behavior: the
    forwarder posts a bare ``failed`` with no output, so no failure card appears
    -- the detail is lost.
    """
    from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

    base_url = live_server
    session_id = _create_claude_native_session(base_url)

    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    _seed_stop_failure_hook(bridge_dir)
    _drive_forwarder_in_thread(base_url, session_id, bridge_dir)

    _open_chat_view(page, base_url, session_id)

    expect(page.get_by_test_id("error-pill")).to_have_count(1, timeout=10_000)
