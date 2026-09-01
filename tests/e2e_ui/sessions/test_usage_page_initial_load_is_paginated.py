"""Regression coverage: the Usage page's initial load must be paginated.

The web Usage page loads its whole report through a single unbounded
``GET /v1/usage``: the server walks *every* conversation the caller can
access and serializes them all into one ``sessions`` array (no ``limit`` /
cursor parameters exist on the route), and the SPA fetches that monolith on
page load. With hundreds of accumulated sessions the initial fetch's latency
and payload grow linearly with session count, stalling the page.

This test reproduces the user journey: accumulate hundreds of priced
sessions, then open the Usage page. It fails while the page's initial
``/v1/usage`` fetch still returns every session in one response, and passes
once the initial load is bounded (any pagination / windowing of the session
detail satisfies it).
"""

from __future__ import annotations

import json
import uuid

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui import conftest as e2e_conftest

# Enough sessions that an unbounded report is observably monolithic, while
# keeping seeding + the buggy full-table fetch fast enough for CI.
SEEDED_SESSIONS = 300


def _stub_server_info(page: Page) -> None:
    """Advertise the ``usage_page`` release feature so the SPA mounts /usage.

    The spawned e2e server runs without ``OMNIGENT_FEATURES``, so the SPA's
    navigation gate would 404 the Usage route. Only ``/v1/info`` is stubbed;
    the ``/v1/usage`` report itself is served by the real server.
    """
    body = json.dumps(
        {
            "accounts_enabled": False,
            "single_user": True,
            "login_url": None,
            "needs_setup": False,
            "features": {
                "usage_page": True,
                "harness_install": False,
            },
            "harness_install_enabled": False,
            "installable_harnesses": [],
        }
    )

    def handle_info(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/v1/info", handle_info)


def _seed_priced_sessions(count: int) -> None:
    """Write *count* priced top-level sessions straight into the store.

    Stages the "user accumulated hundreds of sessions" precondition without
    driving hundreds of real agent turns. Rows go through the same store the
    server reads with, so the report path (ACL filter, ``has_agent_id``,
    ``session_usage`` rollup) runs for real. Each session gets an owner grant
    for the reserved local user, matching what the create-session route
    stamps in single-user mode.

    :param count: Number of sessions to seed.
    :raises RuntimeError: If the server under test isn't one we spawned
        (``--ui-base-url``), so its database isn't reachable from here.
    """
    from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from omnigent.stores.permission_store.sqlalchemy_store import (
        SqlAlchemyPermissionStore,
    )

    database_uri = e2e_conftest._server_state.get("database_uri")
    if not database_uri:
        raise RuntimeError(
            "seeding needs the spawned server's database; it is "
            "unavailable when running against --ui-base-url."
        )
    store = SqlAlchemyConversationStore(str(database_uri))
    perms = SqlAlchemyPermissionStore(str(database_uri))
    for i in range(count):
        conv = store.create_conversation(
            kind="default",
            title=f"usage seed {i}",
            agent_id=uuid.uuid4().hex,
        )
        perms.grant(RESERVED_USER_LOCAL, conv.id, LEVEL_OWNER)
        store.set_session_usage(
            conv.id,
            {
                "input_tokens": 1_000 + i,
                "output_tokens": 500,
                "total_tokens": 1_500 + i,
                "total_cost_usd": 0.25,
                "by_model": {"gpt-4o-mini": {"total_cost_usd": 0.25}},
            },
        )


def test_usage_page_initial_load_does_not_fetch_every_session(
    page: Page,
    live_server: str,
) -> None:
    """The Usage page's first ``/v1/usage`` fetch must not carry all sessions.

    With SEEDED_SESSIONS sessions on the server, opening ``/usage`` must not
    materialize every one of them in the initial report response — that is
    the unbounded-monolith behavior this bug is about. Any bounded first
    page (server-side ``limit`` default, cursor pagination, a windowed
    detail endpoint) passes.
    """
    _stub_server_info(page)
    _seed_priced_sessions(SEEDED_SESSIONS)

    with page.expect_response(
        lambda r: "/v1/usage" in r.url and r.request.method == "GET",
        timeout=120_000,
    ) as response_info:
        page.goto(f"{live_server}/usage")

    response = response_info.value
    assert response.status == 200, f"usage fetch failed: HTTP {response.status}"

    # The page itself must render — this guards against "passing" because
    # the route 404'd or the report request never fired.
    expect(page.get_by_role("heading", name="Usage", exact=True)).to_be_visible(timeout=30_000)

    body = response.json()
    sessions = body.get("sessions") or []
    assert len(sessions) < SEEDED_SESSIONS, (
        f"initial GET /v1/usage returned all {len(sessions)} of "
        f"{SEEDED_SESSIONS} seeded sessions in one monolithic response — "
        "the Usage page's first load is unpaginated, so its latency and "
        "payload grow linearly with total session count."
    )
