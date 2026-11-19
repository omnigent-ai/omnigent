"""Native ``/clear`` replacement sessions keep the cleared session's title and project."""

from __future__ import annotations

import httpx
import pytest

from omnigent.entities.conversation import USER_SESSION_TITLE_MAX_CHARS
from omnigent.native._native_supersession import (
    clear_replacement_create_body,
    unfiled_clear_replacement_body,
)


@pytest.mark.parametrize(
    ("title", "expected_title"),
    [
        ("x" * (USER_SESSION_TITLE_MAX_CHARS + 20), "x" * USER_SESSION_TITLE_MAX_CHARS),
        ("", None),
        (None, None),
    ],
)
def test_clear_replacement_title_fits_the_create_limit(
    title: str | None, expected_title: str | None
) -> None:
    body = clear_replacement_create_body({"title": title}, agent_id="ag", labels={})

    assert body.get("title") == expected_title
    assert "project_id" not in body
    assert "workspace" not in body


@pytest.mark.parametrize(
    ("status_code", "old_session", "retries"),
    [
        (404, {"project_id": "proj_alpha"}, True),
        (403, {"project_id": "proj_alpha"}, False),
        (500, {"project_id": "proj_alpha"}, False),
        (404, {}, False),
    ],
)
def test_clear_replacement_retries_unfiled_only_after_project_not_found(
    status_code: int, old_session: dict[str, object], retries: bool
) -> None:
    create_body = clear_replacement_create_body(
        {**old_session, "title": "Fix login flow"}, agent_id="ag", labels={"k": "v"}
    )

    retry_body = unfiled_clear_replacement_body(create_body, httpx.Response(status_code))

    if retries:
        assert retry_body == {"agent_id": "ag", "labels": {"k": "v"}, "title": "Fix login flow"}
    else:
        assert retry_body is None
