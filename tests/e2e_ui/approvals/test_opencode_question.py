"""E2E: an OpenCode question event round-trips through the web form."""

from __future__ import annotations

import asyncio
import contextlib
import threading

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.opencode_native_client import OpenCodeEvent
from omnigent.opencode_native_forwarder import OpenCodeNativeForwarder

_FORM = '[data-testid="ask-user-question-form"]'
_SUBMIT = '[data-testid="ask-user-question-submit"]'


class _RecordingOpenCodeClient:
    """Record the answer returned by the live web elicitation flow."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, list[list[str]]]] = []

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        self.replies.append((request_id, answers))
        return True

    async def reject_question(self, request_id: str) -> bool:
        return True


@pytest.mark.timeout(90)
def test_opencode_question_round_trips_through_web(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """question.asked -> web checkboxes -> reply_question selected labels."""
    base_url, session_id = seeded_session
    opencode = _RecordingOpenCodeClient()
    result: dict[str, object] = {}

    async def _forward_question() -> None:
        async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as server:
            forwarder = OpenCodeNativeForwarder(
                session_id=session_id,
                opencode_session_id="ses_e2e",
                opencode_client=opencode,  # type: ignore[arg-type]
                server_client=server,
            )
            await forwarder.handle_event(
                OpenCodeEvent(
                    id=None,
                    type="question.asked",
                    properties={
                        "sessionID": "ses_e2e",
                        "id": "que_e2e",
                        "questions": [
                            {
                                "header": "Choose tools",
                                "question": "Which tools should run?",
                                "multiple": True,
                                "options": [
                                    {"label": "Tests"},
                                    {"label": "Lint"},
                                    {"label": "Build"},
                                ],
                            }
                        ],
                    },
                    raw={},
                )
            )
            task = forwarder._question_tasks["que_e2e"]
            await task

    def _run_forwarder() -> None:
        try:
            asyncio.run(_forward_question())
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_run_forwarder, daemon=True)
    thread.start()
    try:
        page.goto(f"{base_url}/c/{session_id}")

        form = page.locator(_FORM)
        expect(form).to_be_visible(timeout=15_000)
        form.get_by_role("checkbox", name="Tests").check()
        form.get_by_role("checkbox", name="Lint").check()
        form.locator(_SUBMIT).click()
    finally:
        thread.join(timeout=5)
        if thread.is_alive():
            with contextlib.suppress(httpx.HTTPError):
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/events",
                    json={
                        "type": "approval",
                        "data": {"elicitation_id": "que_e2e", "action": "decline"},
                    },
                    timeout=10.0,
                ).raise_for_status()
            thread.join(timeout=30)

    assert not thread.is_alive(), "OpenCode question hook did not receive the web verdict"
    if "error" in result:
        raise AssertionError(f"forwarder failed: {result['error']}") from result["error"]
    assert opencode.replies == [("que_e2e", [["Tests", "Lint"]])]
