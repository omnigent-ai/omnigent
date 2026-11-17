"""The background elicitation verdict POST keeps its ids inside the session path.

``_post_elicitation_result`` runs as a fire-and-forget task behind the
responses stream hooks, so a malformed id must neither reach the server as
a different route nor escape as an exception that would kill the task.
"""

from __future__ import annotations

import json

import httpx
import pytest
from omnigent_client._responses import _post_elicitation_result


@pytest.mark.asyncio
async def test_verdict_posts_to_the_elicitation_resolve_url() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"queued": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _post_elicitation_result(http, "http://srv", "conv_abc", "elicit_xyz", True)

    assert captured == {
        "url": "http://srv/v1/sessions/conv_abc/elicitations/elicit_xyz/resolve",
        "body": {"action": "accept"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "elicitation_id"),
    [
        pytest.param("conv_abc/permissions/alice", "elicit_xyz", id="session-id-with-slash"),
        pytest.param("conv_abc", "../../projects/proj_1", id="elicitation-id-traversal"),
    ],
)
async def test_unsafe_ids_send_nothing_and_do_not_raise(
    session_id: str, elicitation_id: str
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(202, json={"queued": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await _post_elicitation_result(http, "http://srv", session_id, elicitation_id, False)

    assert calls == []
