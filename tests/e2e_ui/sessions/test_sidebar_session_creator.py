"""A worker an agent started names that session on its sidebar card, through archive and delete.

A manager session runs the openai-agents harness on a real host. The mock LLM scripts its
reply, but the runner executes ``sys_session_create(placement="same_host")`` for real, so
the server stamps the worker's creator label. Each time the card opens it looks the creator
up: by title while the creator is open or archived, as "Another session" once deleted.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_session_message_labels import (
    _agent_bundle,
    _create_session,
    _created_session,
    _hosted_stack,
)

_TURN_TIMEOUT_MS = 120_000
_MANAGER_REPLY = "The docs worker has its task."


def _script_manager(mock_url: str, nonce: str, agent_id: str) -> str:
    """Script a manager turn that starts a same-host docs worker; return its prompt."""
    tools = ["sys_session_create"]
    set_fallback_mock_llm(mock_url, "_policy_llm_", '{"action":"allow","reason":""}')
    task = f"Refresh the setup guide (docs-{nonce})."
    configure_mock_llm(
        mock_url, [{"text": "Setup guide refreshed."}], match=f"docs-{nonce}", required_tools=tools
    )
    create = {
        "agent_id": agent_id,
        "placement": "same_host",
        "title": "Docs worker",
        "message": task,
    }
    call = {"call_id": "create", "name": "sys_session_create", "arguments": json.dumps(create)}
    configure_mock_llm(
        mock_url,
        [{"tool_calls": [call]}, {"text": _MANAGER_REPLY}],
        match=f"start-{nonce}",
        required_tools=tools,
    )
    return f"Start a docs worker (start-{nonce})."


def _card_mismatch(page: Page, session_id: str, details: str) -> str | None:
    """Reopen *session_id*'s sidebar card; describe how it differs from *details*, if it does."""
    page.reload()
    page.locator(f'aside.conversations-sidebar a[href="/c/{session_id}"]').hover()
    card = page.get_by_test_id("session-tooltip-content")
    try:
        expect(card).to_contain_text(details)
        # "Created by" already names the session this one would read as nested in.
        expect(card).not_to_contain_text("Nested in")
    except AssertionError as error:
        return str(error)
    return None


@pytest.mark.timeout(300)
def test_sidebar_card_names_the_creating_session(
    page: Page,
    built_spa: None,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """The worker's card names its creator while open and archived, and hides it once deleted."""
    if os.environ.get("OMNIGENT_REPRO_SERVER_URL"):
        pytest.fail("This test owns its host and runners; run it outside verify-env run.")
    mock_url = isolated_mock_llm_server_url
    with _hosted_stack(tmp_path) as hosted:
        manager = _create_session(hosted, _agent_bundle(mock_url), "Manager")
        agent_id = hosted.client.get(f"/v1/sessions/{manager}").json()["agent_id"]
        prompt = _script_manager(mock_url, uuid.uuid4().hex[:8], agent_id)
        page.goto(f"{hosted.stack.base_url}/c/{manager}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_enabled(timeout=_TURN_TIMEOUT_MS)
        composer.fill(prompt)
        page.get_by_role("button", name="Send", exact=True).click()
        reply = page.get_by_text(_MANAGER_REPLY, exact=True)
        expect(reply).to_be_visible(timeout=_TURN_TIMEOUT_MS)
        worker = _created_session(hosted.client, "Docs worker", manager)
        page.goto(f"{hosted.stack.base_url}/c/{worker}")

        # Each state is checked on its own, so one mismatch cannot hide the next.
        mismatches = {"open": _card_mismatch(page, worker, "Created byManagerOwnerYou")}
        hosted.client.patch(f"/v1/sessions/{manager}", json={"archived": True}).raise_for_status()
        mismatches["archived"] = _card_mismatch(
            page, worker, "Created byManager · archivedOwnerYou"
        )
        hosted.client.delete(f"/v1/sessions/{manager}").raise_for_status()
        mismatches["deleted"] = _card_mismatch(page, worker, "Created byAnother sessionOwnerYou")
        assert mismatches == {"open": None, "archived": None, "deleted": None}
