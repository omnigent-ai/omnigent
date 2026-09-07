"""E2E: engaging Codex Plan mode must actually plan.

The reporter engaged Plan mode in the web UI and the agent "went straight to
making changes instead of planning". Mechanism (probed live against the real
Codex app-server, codex-cli 0.139.0): Codex's ``collaborationMode.settings.
developer_instructions`` field is *replacing*, not additive — a non-null
value substitutes for the collaboration mode's built-in prompt. The runner's
plan-mode handler (``_handle_codex_native_plan_mode_change`` in
``omnigent/runner/app.py``) always fills that field with the wrapper spec's
authored instructions (the ``omnigent codex`` blurb), so entering Plan mode
delivers the blurb *instead of* Codex's Plan Mode instructions: the model is
never told to plan, and edits away.

Journey (the reporter's, driven through the real SPA): open a codex-native
session, send a first prompt (plan mode is only reachable after that —
facet 1), click the composer's Plan toggle, then send a coding request. The
turn's LLM request — served by the mock LLM backend so it is capturable —
must carry Codex's Plan Mode instructions in its ``<collaboration_mode>``
block. Today it carries only the wrapper blurb, so this test is red.

Same live stack as ``messages/test_native_codex_render_parity.py``: a real
``codex`` CLI in the session terminal, the native bridge forwarding composer
messages into the app-server thread, and the mock LLM serving ``/v1/responses``
(the fixture writes the mock provider config when ``LLM_API_KEY`` is absent).
"""

from __future__ import annotations

import os
import re
import shutil
import uuid
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

# Must match the model in the mock openai provider config written by the
# native_codex_mock_session fixture (conftest._CODEX_MOCK_MODEL).
_CODEX_MOCK_MODEL = "gpt-4o"

# Mock LLM responds instantly; budget covers native CLI boot + attach.
_MOCK_TURN_TIMEOUT_MS = 60_000

# Codex's built-in Plan Mode prompt (delivered inside the request's
# ``<collaboration_mode>`` developer block when plan mode is active) —
# e.g. "# Plan Mode (Conversational)". The codex-native wrapper blurb the
# runner sends today contains no such text, which is the discriminator.
_PLAN_MODE_MARKER = re.compile(r"plan\s*mode", re.IGNORECASE)

pytestmark = [
    pytest.mark.skipif(
        shutil.which("codex") is None,
        reason="codex CLI is required for the native Codex plan-mode e2e",
    ),
    pytest.mark.skipif(
        bool(os.environ.get("LLM_API_KEY")),
        reason=(
            "asserting on the model request requires the mock LLM backend "
            "(native_codex_mock_session only writes the mock provider "
            "config when LLM_API_KEY is absent)"
        ),
    ),
]


def _collaboration_blocks(request_body: dict) -> list[str]:
    """Extract every ``<collaboration_mode>`` block from a Responses request.

    Codex injects the active collaboration mode as a developer message whose
    text wraps the mode's instructions in ``<collaboration_mode>...\
</collaboration_mode>``.

    :param request_body: A captured ``POST /v1/responses`` JSON body.
    :returns: The inner text of each collaboration block, in order.
    """
    blocks: list[str] = []
    for item in request_body.get("input") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if not isinstance(text, str):
                continue
            blocks.extend(
                re.findall(
                    r"<collaboration_mode>(.*?)</collaboration_mode>",
                    text,
                    re.DOTALL,
                )
            )
    return blocks


@pytest.mark.timeout(300)
def test_codex_plan_mode_engaged_from_web_ui_is_respected(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """After the Plan toggle, the next turn must carry plan-mode instructions.

    Red on the unfixed tree: the toggle's ``thread/settings/update`` sends the
    wrapper spec's blurb as ``collaborationMode.settings.developer_
    instructions``, which *replaces* Codex's Plan Mode prompt — so the model
    request after engaging Plan mode carries no planning instructions at all
    and the agent goes straight to making changes.
    """
    base_url, session_id = native_codex_mock_session

    nonce = uuid.uuid4().hex[:8]
    bootstrap_marker = f"usr-boot-{nonce}"
    plan_marker = f"usr-plan-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"ack-boot-{nonce}"}],
        key=bootstrap_marker,
        match=bootstrap_marker,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"ack-plan-{nonce}"}],
        key=plan_marker,
        match=plan_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    # Turn 1 — the first prompt. Plan mode is only reachable after this
    # (facet 1), and it settles the session's live model so the runner's
    # plan-mode handler has a confirmed model to re-assert.
    _send(page, f"Bootstrap turn. Context marker {bootstrap_marker}.")
    expect(page.locator(_ASSISTANT, has_text=f"ack-boot-{nonce}").first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    # Engage Plan mode through the real UI path: the composer toggle PATCHes
    # ``collaboration_mode: plan`` and the server requires a confirmed 2xx
    # runner forward into the live Codex thread before persisting.
    plan_toggle = page.get_by_test_id("codex-plan-mode-toggle")
    expect(plan_toggle).to_be_visible(timeout=30_000)
    expect(plan_toggle).to_have_attribute("aria-label", "Enter Plan mode")

    def _is_plan_patch(response) -> bool:
        parsed = urlparse(response.url)
        return (
            response.request.method == "PATCH"
            and parsed.path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )

    with page.expect_response(_is_plan_patch, timeout=60_000):
        plan_toggle.click()
    expect(plan_toggle).to_have_attribute("data-active", "true", timeout=30_000)
    # The composer status line mirrors the engaged mode.
    expect(page.get_by_test_id("composer-plan-mode")).to_contain_text("Plan mode", timeout=30_000)

    # Turn 2 — the coding request sent *in Plan mode*. This is the turn the
    # reporter watched edit files instead of planning.
    _send(
        page,
        f"Add a hello.txt file with a greeting to the workspace. Context marker {plan_marker}.",
    )
    expect(page.locator(_ASSISTANT, has_text=f"ack-plan-{nonce}").first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    # The plan-mode turn's model request must carry Codex's Plan Mode
    # instructions. Fetch what the mock LLM actually received for turn 2.
    captured = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    captured.raise_for_status()
    plan_turn_requests = [
        body
        for body in captured.json()["requests"]
        if isinstance(body, dict) and plan_marker in str(body.get("input"))
    ]
    assert plan_turn_requests, (
        f"mock LLM captured no request containing the plan-turn marker "
        f"{plan_marker!r} — the plan-mode turn never reached the model"
    )

    blocks = _collaboration_blocks(plan_turn_requests[-1])
    assert blocks, (
        "the plan-mode turn's model request carries no "
        "<collaboration_mode> block at all — the Plan toggle changed "
        "nothing about what the model is told."
    )
    assert any(_PLAN_MODE_MARKER.search(block) for block in blocks), (
        "Plan mode was engaged in the web UI but the model "
        "request's <collaboration_mode> block carries no Plan Mode "
        "instructions — it contains only the wrapper's developer "
        "instructions, which REPLACED Codex's plan-mode prompt "
        f"(blocks: {[b[:120] for b in blocks]!r}). The model is never told "
        "to plan, so it goes straight to making changes."
    )
