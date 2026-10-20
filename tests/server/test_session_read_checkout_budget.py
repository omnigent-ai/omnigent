"""Pool-checkout budget of the reads the web app issues when a session is opened.

Opening a session page fires the session snapshot and the sidebar list. Each
handler runs several store calls back to back; every call that checks out its
own pooled connection costs a checkout and, on Postgres, a ``pool_pre_ping``
round trip. One request should hold one connection per engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from sqlalchemy import Engine, event

from omnigent.db.utils import get_or_create_engine
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests._helpers.session import post_session_bundle
from tests.server.helpers import build_agent_bundle

OWNER = {"X-Forwarded-Email": "owner@example.test"}
ADMIN = {"X-Forwarded-Email": "admin@example.test"}
# Query strings the web chat surface sends on session open (captured from the SPA).
SNAPSHOT_QUERY = (
    "include_items=false&include_liveness=false&include_usage=false&refresh_state=true"
)
SIDEBAR_QUERY = "order=desc&sort_by=updated_at&limit=30&visibility=mine"


class CheckoutMeter:
    """Pool checkouts and dialect pre-pings observed on one engine."""

    def __init__(self) -> None:
        self.checkouts = 0
        self.pings = 0


@contextmanager
def meter_engine(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[CheckoutMeter]:
    meter = CheckoutMeter()

    def _on_checkout(_dbapi: object, _record: object, _proxy: object) -> None:
        meter.checkouts += 1

    original_ping = engine.dialect.do_ping

    def _do_ping(dbapi_connection: object) -> bool:
        meter.pings += 1
        return original_ping(dbapi_connection)

    event.listen(engine, "checkout", _on_checkout)
    monkeypatch.setattr(engine.dialect, "do_ping", _do_ping)
    try:
        yield meter
    finally:
        event.remove(engine, "checkout", _on_checkout)


async def _create_session(client: httpx.AsyncClient, title: str | None = None) -> str:
    response = await post_session_bundle(
        client.post,
        "/v1/sessions",
        build_agent_bundle(name="hello_world"),
        metadata={"title": title} if title else None,
        headers=OWNER,
    )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _assert_one_checkout(meter: CheckoutMeter, request: str) -> None:
    assert meter.checkouts >= 1, (
        f"{request}: meter saw no checkouts; attached to the wrong engine?"
    )
    assert meter.checkouts == 1, (
        f"{request} must reuse one pooled connection, took {meter.checkouts} checkouts "
        f"({meter.pings} pre-ping round trips)"
    )
    assert meter.pings <= 1, f"{request} paid {meter.pings} pre-ping round trips for one request"


@pytest.mark.asyncio
async def test_session_snapshot_read_costs_one_checkout(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _create_session(auth_client)
    request = f"/v1/sessions/{session_id}?{SNAPSHOT_QUERY}"

    with meter_engine(get_or_create_engine(db_uri), monkeypatch) as meter:
        response = await auth_client.get(request, headers=OWNER)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == session_id
    _assert_one_checkout(meter, f"GET {request}")


@pytest.mark.asyncio
async def test_session_snapshot_read_costs_one_checkout_for_admin(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admin's access check skips the conversation read, so the snapshot reads it."""
    session_id = await _create_session(auth_client)
    SqlAlchemyPermissionStore(db_uri).ensure_user(ADMIN["X-Forwarded-Email"], is_admin=True)
    request = f"/v1/sessions/{session_id}?{SNAPSHOT_QUERY}"

    with meter_engine(get_or_create_engine(db_uri), monkeypatch) as meter:
        response = await auth_client.get(request, headers=ADMIN)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == session_id
    _assert_one_checkout(meter, f"GET {request}")


@pytest.mark.asyncio
async def test_session_sidebar_list_costs_one_checkout(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _create_session(auth_client)
    request = f"/v1/sessions?{SIDEBAR_QUERY}"

    with meter_engine(get_or_create_engine(db_uri), monkeypatch) as meter:
        response = await auth_client.get(request, headers=OWNER)

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["data"]] == [session_id]
    _assert_one_checkout(meter, f"GET {request}")


@pytest.mark.asyncio
async def test_session_sidebar_search_costs_one_checkout(
    auth_client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sidebar search shares the checkout too, including the Postgres statement timeout."""
    session_id = await _create_session(auth_client, title="budget search target")
    await _create_session(auth_client, title="unrelated")
    request = f"/v1/sessions?{SIDEBAR_QUERY}&search_query=budget"

    with meter_engine(get_or_create_engine(db_uri), monkeypatch) as meter:
        response = await auth_client.get(request, headers=OWNER)

    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["data"]] == [session_id]
    _assert_one_checkout(meter, f"GET {request}")
