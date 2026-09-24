"""Verify native failures surface a reason and never reuse a successful reply as that reason."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_native_codex_session,
    _ensure_runner_online,
    _server_state,
)

_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_MESSAGE = '[data-testid="error-message-content"]'

# Match the pill produced by the native failure under test.
_NATIVE_FAILURE_HEADLINE = "The agent ran into an error during this turn."

_SUCCESS_PROSE = "All conflicts resolved. Continue the sync:"


def _publish_native_status(
    base_url: str,
    session_id: str,
    status: str,
    *,
    response_id: str,
    output: str | None = None,
) -> None:
    """Post the status payload emitted by a codex-native forwarder."""
    data: dict[str, str] = {"status": status, "response_id": response_id}
    if output is not None:
        data["output"] = output
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_session_status", "data": data},
        timeout=15.0,
    )
    resp.raise_for_status()


def _publish_assistant_message(
    base_url: str, session_id: str, text: str, *, response_id: str
) -> None:
    """Persist a normal, successful assistant reply for the in-flight turn."""
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": "assistant", "text": text, "response_id": response_id},
        },
        timeout=15.0,
    )
    resp.raise_for_status()


def test_detail_less_native_failure_surfaces_readable_error(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Shape 1: a native turn that fails with no detail must not fail silently."""
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_codex_session(live_server, runner_id)
    try:
        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

        working = page.locator(_WORKING)
        pills = page.locator(_ERROR_PILL)

        _publish_native_status(live_server, session_id, "running", response_id="turn_no_detail")
        expect(working).to_be_visible(timeout=15_000)

        # Reproduce the forwarder's bare failed edge.
        _publish_native_status(live_server, session_id, "failed", response_id="turn_no_detail")
        expect(working).to_have_count(0, timeout=15_000)

        # Require the fallback pill for this detail-less failure.
        native_failure_pill = pills.filter(has_text=_NATIVE_FAILURE_HEADLINE)
        expect(native_failure_pill.first).to_be_visible(timeout=15_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def test_native_failure_does_not_show_assistant_reply_as_the_error(
    page: Page,
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Shape 3: a successful reply must never be published as the failure reason."""
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_codex_session(live_server, runner_id)
    try:
        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

        working = page.locator(_WORKING)
        pills = page.locator(_ERROR_PILL)

        _publish_native_status(live_server, session_id, "running", response_id="turn_prose")
        expect(working).to_be_visible(timeout=15_000)

        # The turn produces a normal, successful assistant reply...
        _publish_assistant_message(
            live_server, session_id, _SUCCESS_PROSE, response_id="turn_prose"
        )
        # ...and is then labelled failed with no reason of its own.
        _publish_native_status(live_server, session_id, "failed", response_id="turn_prose")
        expect(working).to_have_count(0, timeout=15_000)

        # Inspect the reason derived after the assistant reply.
        error_pill = pills.filter(has_text=_NATIVE_FAILURE_HEADLINE).first
        expect(error_pill).to_be_visible(timeout=15_000)
        error_pill.click()
        message = error_pill.locator(_ERROR_MESSAGE)
        expect(message).to_be_visible(timeout=10_000)

        # Persisted assistant prose must be labeled instead of reused verbatim.
        assert message.inner_text().strip() != _SUCCESS_PROSE, (
            "the failed turn's surfaced reason is the assistant's own successful "
            "message verbatim; a successful reply must not be shown as the error"
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
