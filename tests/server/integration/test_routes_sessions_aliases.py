"""Integration tests for BE-2 additions on the session URL surface.

This PR adds two things on top of main's existing ``/v1/sessions/*``
routes:

1. ``search_query`` query param on ``GET /v1/sessions``. Powers the
   sidebar's session search; case-insensitive substring filter on
   ``SqlConversation.title`` via a SQL ``LIKE``. Sessions with a
   ``NULL`` title are excluded when the filter is active.

2. ``DELETE /v1/sessions/{conversation_id}`` alias on the
   conversations router. Main has no DELETE on its sessions
   router, but the UI needs one — the conversations DELETE
   handler already runs the full teardown (tasks, runner-side
   resource cleanup, session files), so the alias just delegates.

Main owns ``GET /v1/sessions``, ``PATCH /v1/sessions/{id}``,
``GET /v1/sessions/{id}/items``, and the ``/v1/sessions/{id}/
comments[/*]`` aliases. Their tests live elsewhere
(``test_routes_sessions_title.py`` for create/patch/title, e2e for
comments). This file pins only the new BE-2 surface.
"""

from __future__ import annotations

import httpx
import pytest

from tests.server.helpers import (
    create_test_session,
)

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient,
    *,
    name: str,
    title: str | None = None,
) -> str:
    """Create a session via ``POST /v1/sessions`` and return its id.

    ``GET /v1/sessions`` filters to conversations with ``agent_id IS
    NOT NULL`` (i.e. real sessions). Tests that need a session in the
    listing must go through ``POST /v1/sessions``.

    :param client: HTTP client wired to the test app.
    :param name: Agent name to write into the uploaded bundle, e.g.
        ``"search-agent"``.
    :param title: Optional session title.
    :returns: The new session/conversation id.
    """
    session = await create_test_session(client, name=name, title=title)
    return session["id"]


# ── GET /v1/sessions?search_query=... ─────────────────────────


async def test_list_sessions_search_query_filters_by_title(client: httpx.AsyncClient) -> None:
    """``search_query`` performs a case-insensitive substring match on title.

    Two seeded sessions get distinct titles; the search must return
    only the one whose title matches. Search target uses mixed case
    ("Alpha…") with a lowercase query ("alpha") to prove the LIKE
    is case-insensitive on both sides.

    :param client: HTTP client wired to the test app.
    """
    conv_a = await _create_session(
        client,
        name="search-alpha-agent",
        title="Alpha Roadmap Review",
    )
    await _create_session(
        client,
        name="search-beta-agent",
        title="Beta Bug Triage",
    )

    resp = await client.get("/v1/sessions", params={"search_query": "alpha"})
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()["data"]]
    # The case-insensitive match must hit conv_a only. If conv_b
    # appears the LIKE pattern wasn't applied. If conv_a is missing
    # the LIKE is case-sensitive (e.g. forgot func.lower()).
    assert ids == [conv_a], f"Expected only {conv_a!r} matching 'alpha', got {ids!r}"


async def test_list_sessions_search_query_empty_is_noop(client: httpx.AsyncClient) -> None:
    """An empty ``search_query=`` returns the unfiltered list.

    The UI sends ``?search_query=`` when the search box is cleared;
    the route must treat that identically to the param being
    absent so the sidebar doesn't go blank on every Backspace.

    :param client: HTTP client wired to the test app.
    """
    conv_id = await _create_session(client, name="empty-search-agent")

    resp = await client.get("/v1/sessions", params={"search_query": ""})
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()["data"]]
    # The seeded session must still appear under the empty
    # search. Failure here means empty-string normalization was
    # broken — the route would pass ``""`` to the store, which
    # then filters by ``LIKE '%%'`` (matches any non-null title)
    # AND drops untitled rows, so a freshly-created untitled
    # session would vanish from the list.
    assert conv_id in ids


async def test_list_sessions_search_query_excludes_null_titles(
    client: httpx.AsyncClient,
) -> None:
    """A session with a NULL title is excluded from search results.

    The LIKE filter on NULL evaluates to NULL (falsy), so untitled
    sessions are correctly omitted. Pin this so a future refactor
    that adds an ``OR title IS NULL`` doesn't accidentally surface
    untitled rows in unrelated searches.

    :param client: HTTP client wired to the test app.
    """
    conv_a = await _create_session(
        client,
        name="has-title-agent",
        title="Has Title",
    )
    await _create_session(client, name="null-title-agent")

    resp = await client.get("/v1/sessions", params={"search_query": "has"})
    ids = [c["id"] for c in resp.json()["data"]]
    # Only the titled match returns. If conv_b appears, the NULL
    # filter is misbehaving.
    assert ids == [conv_a]


# ── DELETE /v1/sessions/{conversation_id} ─────────────────────


async def test_delete_session(client: httpx.AsyncClient) -> None:
    """``DELETE /v1/sessions/{id}`` removes the conversation row.

    Routes via the conversations router's DELETE handler — that
    one runs the full teardown (tasks, runner-side resources,
    session files). Cross-check via the conversations GET that
    the row is gone, so the alias isn't silently acknowledging
    the delete without performing it.

    The session is created via ``POST /v1/sessions`` (the only
    create path after the DBOS/responses removal); the delete
    alias is agnostic of how the row was created — it just needs
    one to remove.

    :param client: HTTP client wired to the test app.
    """
    snapshot = await create_test_session(client)
    conv_id = snapshot["id"]

    del_resp = await client.delete(f"/v1/sessions/{conv_id}")
    assert del_resp.status_code == 200
    body = del_resp.json()
    assert body["id"] == conv_id
    assert body["deleted"] is True

    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404


async def test_delete_session_not_found(client: httpx.AsyncClient) -> None:
    """``DELETE /v1/sessions/{missing}`` returns 404."""
    resp = await client.delete("/v1/sessions/conv_does_not_exist")
    assert resp.status_code == 404


async def test_delete_session_when_runner_offline(client: httpx.AsyncClient) -> None:
    """An offline bound runner must not block ``DELETE /v1/sessions/{id}``.

    Reproduces the production failure where ``RunnerRouter.
    client_for_session_resources`` raises ``RUNNER_UNAVAILABLE``
    because the bound runner has disconnected. The handler must
    catch that, skip runner-side cleanup, and still tear down
    server-owned state (tasks, files, the conversation row) so the
    chat actually disappears from the UI.
    """
    from omnigent.errors import ErrorCode, OmnigentError
    from omnigent.runtime import _globals, set_runner_router

    snapshot = await create_test_session(client)
    conv_id = snapshot["id"]

    class _OfflineRunnerRouter:
        def client_for_session_resources(self, session_id: str) -> object:
            del session_id
            raise OmnigentError(
                "runner 'runner_token_offline' is offline",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )

    prior = _globals._runner_router
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    try:
        del_resp = await client.delete(f"/v1/sessions/{conv_id}")
    finally:
        set_runner_router(prior)

    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404
