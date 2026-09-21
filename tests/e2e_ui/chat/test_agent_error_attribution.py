"""Agent error headlines through the real session API and browser renderer.

Native failures enter through the forwarder's status endpoint. Non-native
history uses persisted response items. Neither case needs a provider failure
or a running vendor CLI to exercise attribution, reload, and disclosure.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.entities import ErrorData, MessageData, NewConversationItem
from tests.e2e_ui.chat.test_failure_error_card import _publish_native_status
from tests.e2e_ui.conftest import seed_committed_items


@contextmanager
def _named_session(base_url: str, name: str, harness: str) -> Iterator[str]:
    """Register an isolated agent without launching its harness."""
    config = (
        f"spec_version: 1\nname: {name}\nprompt: Help with the requested task.\n"
        f"executor:\n  config:\n    harness: {harness}\n"
    ).encode()
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        entry = tarfile.TarInfo("config.yaml")
        entry.size = len(config)
        archive.addfile(entry, io.BytesIO(config))
    response = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]
    try:
        agent = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
        agent.raise_for_status()
        assert agent.json()["name"] == name
        yield session_id
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).raise_for_status()


def _expect_named_error(page: Page, display_name: str, message: str) -> None:
    """Check the single named banner and expand its original diagnostics."""
    headline = f"{display_name} ran into an error during this turn."
    pill = page.get_by_test_id("error-pill")
    expect(pill).to_have_count(1, timeout=15_000)
    expect(pill.get_by_test_id("error-headline")).to_have_text(headline)
    pill.get_by_role("button", name=headline, exact=True).click()
    expect(pill.get_by_test_id("error-message-content")).to_have_text(message)


@pytest.mark.parametrize(
    ("agent_name", "harness", "display_name"),
    [
        ("claude-native-ui", "claude-native", "Claude Code"),
        ("codex-native-ui", "codex-native", "Codex"),
    ],
    ids=["claude-code", "codex"],
)
def test_native_failure_names_the_fetched_agent_live_and_after_reload(
    page: Page,
    live_server: str,
    agent_name: str,
    harness: str,
    display_name: str,
) -> None:
    """A native status failure uses the API name and survives a fresh page."""
    message = "API Error: 502 The upstream server returned an invalid response."
    response_id = "native_turn_named_error"
    with _named_session(live_server, agent_name, harness) as session_id:
        seed_committed_items(
            session_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=response_id,
                    data=MessageData(
                        role="assistant",
                        agent=agent_name,
                        content=[{"type": "output_text", "text": message}],
                    ),
                ),
            ],
        )
        page.goto(f"{live_server}/c/{session_id}")
        expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=15_000)
        _publish_native_status(live_server, session_id, "running", response_id=response_id)
        _publish_native_status(live_server, session_id, "failed", response_id=response_id)
        _expect_named_error(page, display_name, message)

        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        snapshot.raise_for_status()
        assert snapshot.json()["agent_name"] == agent_name
        assert snapshot.json()["last_task_error"]["code"] == "native_turn_error"
        page.reload()
        _expect_named_error(page, display_name, message)


def test_non_native_failure_names_its_response_agent_after_reload(
    page: Page,
    live_server: str,
) -> None:
    """A custom name from saved response items is never replaced by a default."""
    suffix = uuid.uuid4().hex[:8]
    agent_name = f"release-reviewer-{suffix}"
    display_name = f"Release-reviewer-{suffix}"
    message = "The harness stopped while reviewing the release."
    response_id = "resp_named_error"
    with _named_session(live_server, agent_name, "openai-agents") as session_id:
        seed_committed_items(
            session_id,
            [
                NewConversationItem(
                    type="message",
                    response_id=response_id,
                    data=MessageData(
                        role="assistant",
                        agent=agent_name,
                        content=[{"type": "output_text", "text": "Reviewing the release."}],
                    ),
                ),
                NewConversationItem(
                    type="error",
                    response_id=response_id,
                    data=ErrorData(source="execution", code="RuntimeError", message=message),
                ),
            ],
        )
        page.goto(f"{live_server}/c/{session_id}")
        _expect_named_error(page, display_name, message)

        page.reload()
        _expect_named_error(page, display_name, message)
