"""``GET /v1/sessions/{id}/data_queries`` through the real route and stores."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.entities import FunctionCallData, MessageData, NewConversationItem
from omnigent.entities.conversation import FunctionCallOutputData
from omnigent.server.routes.sessions.routes_data_queries import collect_data_queries
from omnigent.spec.data_queries import parse_data_queries
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import build_agent_bundle, create_test_session

pytestmark = pytest.mark.asyncio

_WAREHOUSE_RULES = {
    "rules": [
        {
            "name": "warehouse_sql",
            "mcp": {"server": "warehouse", "tool": "execute_sql"},
            "sql": {"argument": "statement"},
            "source": {"argument": "warehouse_id"},
            "row_count": {"result": "manifest.total_row_count"},
        }
    ]
}


def _call(
    response_id: str, call_id: str, name: str, arguments: dict[str, object]
) -> NewConversationItem:
    return NewConversationItem(
        type="function_call",
        response_id=response_id,
        data=FunctionCallData(
            agent="analyst", name=name, arguments=json.dumps(arguments), call_id=call_id
        ),
    )


def _output(response_id: str, call_id: str, output: str) -> NewConversationItem:
    return NewConversationItem(
        type="function_call_output",
        response_id=response_id,
        data=FunctionCallOutputData(call_id=call_id, output=output),
    )


def _turn_items() -> list[NewConversationItem]:
    return [
        NewConversationItem(
            type="message",
            response_id="resp_1",
            data=MessageData(role="user", content=[{"type": "input_text", "text": "revenue?"}]),
        ),
        _call("resp_1", "call_ls", "Bash", {"command": "ls"}),
        _output("resp_1", "call_ls", "q.sql"),
        _call("resp_1", "call_psql", "Bash", {"command": "psql -d shop -c 'SELECT 1'"}),
        _call(
            "resp_1",
            "call_mcp",
            "mcp__warehouse__execute_sql",
            {"statement": "SELECT sum(total) FROM orders", "warehouse_id": "wh1"},
        ),
        _output("resp_1", "call_psql", " ?column?\n----------\n 1\n(1 row)\n"),
        _output("resp_1", "call_mcp", json.dumps({"manifest": {"total_row_count": 4}})),
        _call("resp_2", "call_running", "Bash", {"command": "duckdb -c 'select 2'"}),
    ]


async def test_session_data_queries_classify_spec_and_builtin_calls(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Spec rules and built-in CLI patterns classify the session's calls, outputs paired."""
    bundle = build_agent_bundle(name="analyst", data_queries=_WAREHOUSE_RULES)
    session = await create_test_session(client, name="analyst", bundle=bundle)
    store = SqlAlchemyConversationStore(db_uri)
    stored = store.append(session["id"], _turn_items())
    call_ids = {
        item.data.call_id: item.id for item in stored if isinstance(item.data, FunctionCallData)
    }

    resp = await client.get(f"/v1/sessions/{session['id']}/data_queries")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [entry["call_id"] for entry in body["data"]] == [
        "call_psql",
        "call_mcp",
        "call_running",
    ]
    psql, mcp, running = body["data"]
    assert psql["id"] == call_ids["call_psql"]
    assert psql["response_id"] == "resp_1"
    assert psql["tool_name"] == "Bash"
    assert (psql["rule"], psql["origin"], psql["matcher"]) == ("psql", "builtin", "shell")
    assert (psql["sql"], psql["source"], psql["row_count"]) == ("SELECT 1", "shop", 1)
    assert mcp["output_item_id"] is not None
    assert (mcp["rule"], mcp["origin"], mcp["kind"]) == ("warehouse_sql", "spec", "query")
    assert (mcp["sql"], mcp["source"], mcp["row_count"]) == (
        "SELECT sum(total) FROM orders",
        "wh1",
        4,
    )
    assert running["output_item_id"] is None and running["row_count"] is None
    assert body["has_more"] is False and body["next_after"] is None

    # Pages resume from ``next_after`` without losing or repeating a query.
    first = (await client.get(f"/v1/sessions/{session['id']}/data_queries?limit=1")).json()
    assert [entry["call_id"] for entry in first["data"]] == ["call_psql"]
    assert first["data"][0]["row_count"] == 1
    assert first["has_more"] is True
    rest = (
        await client.get(
            f"/v1/sessions/{session['id']}/data_queries",
            params={"after": first["next_after"]},
        )
    ).json()
    assert [entry["call_id"] for entry in rest["data"]] == ["call_mcp", "call_running"]


async def test_session_data_queries_respect_a_builtins_opt_out(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """With ``builtins: false`` a ``psql`` shell call is no longer a query."""
    bundle = build_agent_bundle(
        name="analyst", data_queries={**_WAREHOUSE_RULES, "builtins": False}
    )
    session = await create_test_session(client, name="analyst", bundle=bundle)
    SqlAlchemyConversationStore(db_uri).append(session["id"], _turn_items())

    body = (await client.get(f"/v1/sessions/{session['id']}/data_queries")).json()

    assert [entry["call_id"] for entry in body["data"]] == ["call_mcp"]


async def test_session_data_queries_unknown_session_is_404(client: httpx.AsyncClient) -> None:
    """A session that doesn't exist reads as not found."""
    resp = await client.get("/v1/sessions/conv_missing/data_queries")
    assert resp.status_code == 404


@pytest.mark.parametrize(("page_size", "max_scanned_items"), [(2, 3), (1, 1), (3, 2)])
async def test_scan_bound_keeps_outputs_paired_across_pages(
    client: httpx.AsyncClient, db_uri: str, page_size: int, max_scanned_items: int
) -> None:
    """Scans cut short by their item bound resume without losing a query or its output."""
    session = await create_test_session(client, name="analyst")
    store = SqlAlchemyConversationStore(db_uri)
    store.append(session["id"], _turn_items())
    spec = parse_data_queries(_WAREHOUSE_RULES)

    seen: list[tuple[str, int | None]] = []
    after: str | None = None
    for _ in range(20):
        page = collect_data_queries(
            store,
            session["id"],
            spec=spec,
            after=after,
            limit=10,
            max_scanned_items=max_scanned_items,
            page_size=page_size,
        )
        seen.extend((entry.call_id, entry.row_count) for entry in page.data)
        if not page.has_more:
            break
        assert page.next_after is not None and page.next_after != after
        after = page.next_after
    else:
        pytest.fail("the scan never reached the session's end")
    assert seen == [("call_psql", 1), ("call_mcp", 4), ("call_running", None)]
