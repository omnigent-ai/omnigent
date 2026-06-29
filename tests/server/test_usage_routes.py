"""Tests for the /v1/usage route (single-user mode)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.routes.usage import create_usage_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def ctx(db_uri: str) -> tuple[TestClient, SqlAlchemyConversationStore]:
    """A TestClient over a minimal app + the backing conversation store."""
    store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    app.include_router(create_usage_router(store), prefix="/v1")
    return TestClient(app), store


def test_empty_usage(ctx: tuple[TestClient, SqlAlchemyConversationStore]) -> None:
    client, _ = ctx
    body = client.get("/v1/usage").json()
    assert body["object"] == "usage"
    assert body["conversations"] == 0
    assert body["totals"]["input_tokens"] == 0
    assert body["by_model"] == {}


def test_aggregates_across_conversations(
    ctx: tuple[TestClient, SqlAlchemyConversationStore],
) -> None:
    client, store = ctx
    c1 = store.create_conversation(title="a").id
    c2 = store.create_conversation(title="b").id
    store.set_session_usage(
        c1,
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "total_cost_usd": 0.5,
            "by_model": {"claude-sonnet-4-6": {"input_tokens": 100, "total_cost_usd": 0.5}},
        },
    )
    store.set_session_usage(
        c2,
        {
            "input_tokens": 200,
            "output_tokens": 20,
            "total_cost_usd": 1.0,
            "by_model": {"claude-sonnet-4-6": {"input_tokens": 200, "total_cost_usd": 1.0}},
        },
    )

    body = client.get("/v1/usage").json()
    assert body["conversations"] == 2
    assert body["totals"]["input_tokens"] == 300
    assert body["totals"]["total_cost_usd"] == 1.5
    assert body["by_model"]["claude-sonnet-4-6"]["input_tokens"] == 300
