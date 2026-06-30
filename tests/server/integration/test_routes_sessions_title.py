"""Integration tests for session-created conversation titles.

The two event-driven title tests (``test_session_event_seeds_title_*``)
were dropped with the DBOS / ``/v1/responses`` removal: they posted
``message`` events to ``POST /v1/sessions/{id}/events``, which now
requires a runner-bound session to forward the event. Previously the
test fixture mounted the legacy responses router which drove
execution in-process; with that path gone, the route returns 503
``runner_unavailable`` before reaching the title-seed helper. Coverage
for first-message title seeding lives at the e2e level (the REPL and
web flows exercise it against a real runner).
"""

from __future__ import annotations

import httpx
import pytest

from tests.server.helpers import create_test_session

pytestmark = pytest.mark.asyncio


async def test_session_create_metadata_sets_initial_title(
    client: httpx.AsyncClient,
) -> None:
    """POST /sessions metadata writes the initial conversation title."""
    session = await create_test_session(
        client,
        name="metadata-title-agent",
        title="initial prompt title",
    )

    conversation = await client.get(f"/v1/sessions/{session['id']}")
    assert conversation.status_code == 200
    assert conversation.json()["title"] == "initial prompt title"
