"""End-to-end coverage for the Archive Library's rolling date filter."""

from __future__ import annotations

import time
import uuid

import httpx

from tests.e2e.conftest import create_runner_bound_session, register_inline_agent


def test_recent_archive_filter_includes_a_newly_archived_session(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """A real archived session appears inside the rolling 30-day window."""
    suffix = uuid.uuid4().hex[:6]
    agent_name = register_inline_agent(
        http_client,
        name=f"archive-filter-{suffix}",
        harness="openai-agents",
        model=f"mock-archive-filter-{suffix}",
        profile="",
        prompt="Archive date filter fixture.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    try:
        archived = http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"title": f"archive-filter-{suffix}", "archived": True},
        )
        archived.raise_for_status()

        now = int(time.time())
        recent = http_client.get(
            "/v1/sessions",
            params={
                "visibility": "archived",
                "archived_only": "true",
                "sort_by": "archived_at",
                "order": "desc",
                "archived_after": now - 30 * 86_400,
            },
        )
        recent.raise_for_status()
        assert session_id in {row["id"] for row in recent.json()["data"]}

        future = http_client.get(
            "/v1/sessions",
            params={
                "visibility": "archived",
                "archived_only": "true",
                "sort_by": "archived_at",
                "order": "desc",
                "archived_after": now + 60,
            },
        )
        future.raise_for_status()
        assert session_id not in {row["id"] for row in future.json()["data"]}
    finally:
        http_client.delete(f"/v1/sessions/{session_id}")
