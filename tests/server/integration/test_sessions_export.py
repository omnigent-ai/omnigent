"""``GET /v1/sessions/{id}/export?format=jsonl`` and the export's output control."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.server.data_controls import OUTPUT_OMITTED, DataControls, IncludeOutputs
from omnigent.server.routes.sessions import routes_items
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

_ITEMS = [
    {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": "Count the rows"}]},
    },
    {
        "type": "function_call",
        "data": {
            "agent": "analyst",
            "name": "sys_os_shell",
            "arguments": json.dumps({"command": "wc -l sales.csv"}),
            "call_id": "call_1",
        },
    },
    {
        "type": "function_call_output",
        "data": {"call_id": "call_1", "output": "1501 sales.csv"},
    },
    {
        "type": "message",
        "data": {
            "role": "assistant",
            "agent": "analyst",
            "content": [{"type": "output_text", "text": "1,500 rows plus a header."}],
        },
    },
]


async def _session(client: httpx.AsyncClient) -> str:
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "initial_items": _ITEMS}
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _export(
    client: httpx.AsyncClient, session_id: str, **params: str
) -> tuple[httpx.Response, list[dict[str, Any]]]:
    response = await client.get(
        f"/v1/sessions/{session_id}/export", params={"format": "jsonl", **params}
    )
    assert response.status_code == 200, response.text
    return response, [json.loads(line) for line in response.text.splitlines()]


async def test_exports_the_transcript_the_session_api_serves(client: httpx.AsyncClient) -> None:
    """The export is the session snapshot plus every item, as the old client-built file was."""
    session_id = await _session(client)

    response, lines = await _export(client, session_id)

    assert (await client.get("/v1/info")).json()["session_export"] is True
    assert response.headers["content-type"].startswith("application/jsonl")
    assert response.headers["content-disposition"].startswith(
        f'attachment; filename="{session_id}.jsonl"'
    )
    meta, *records = lines
    assert meta["record_type"] == "session_meta"
    assert meta["id"] == session_id
    assert meta["outputs_omitted"] is False
    items = (await client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})).json()
    assert records == [{"record_type": "item", **item} for item in items["data"]]
    assert records[2]["output"] == "1501 sales.csv"


@pytest.mark.parametrize(
    ("include_outputs", "requested", "omitted"),
    [
        (IncludeOutputs.REQUEST, "false", True),
        (IncludeOutputs.NEVER, "true", True),
        (IncludeOutputs.NEVER, "false", True),
    ],
    ids=["request-asks-to-omit", "never-overrides-request", "never-and-request-agree"],
)
async def test_leaves_tool_outputs_out(
    app: FastAPI,
    client: httpx.AsyncClient,
    include_outputs: IncludeOutputs,
    requested: str,
    omitted: bool,
) -> None:
    """Outputs are replaced whenever the request or the deployment says so."""
    app.state.data_controls = DataControls(include_outputs=include_outputs)
    session_id = await _session(client)

    _response, (meta, *records) = await _export(client, session_id, outputs=requested)

    assert meta["outputs_omitted"] is omitted
    assert [record["type"] for record in records] == [item["type"] for item in _ITEMS]
    assert records[2]["call_id"] == "call_1"
    assert records[2]["output"] == OUTPUT_OMITTED
    # What the agent ran and said stays; only what the tool returned goes.
    assert json.loads(records[1]["arguments"]) == {"command": "wc -l sales.csv"}
    assert records[3]["content"][0]["text"] == "1,500 rows plus a header."
    assert "1501 sales.csv" not in "\n".join(json.dumps(record) for record in records)


async def test_info_reports_the_data_controls(app: FastAPI, client: httpx.AsyncClient) -> None:
    """Clients read the controls from ``/v1/info`` to label exports and hide downloads."""
    assert (await client.get("/v1/info")).json()["data_controls"] == {
        "include_outputs": "request",
        "allow_downloads": True,
        "content_security_policy": "off",
    }
    app.state.data_controls = DataControls(
        include_outputs=IncludeOutputs.NEVER, allow_downloads=False
    )
    assert (await client.get("/v1/info")).json()["data_controls"] == {
        "include_outputs": "never",
        "allow_downloads": False,
        "content_security_policy": "off",
    }


async def test_rejects_an_unknown_session_or_format(client: httpx.AsyncClient) -> None:
    missing = await client.get("/v1/sessions/conv_missing/export", params={"format": "jsonl"})
    assert missing.status_code == 404
    session_id = await _session(client)
    for params in ({"format": "csv"}, {}):
        response = await client.get(f"/v1/sessions/{session_id}/export", params=params)
        assert response.status_code == 422, params


async def test_reads_every_store_page_in_order(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session longer than one store page exports whole and in order."""
    monkeypatch.setattr(routes_items, "_EXPORT_PAGE_SIZE", 3)
    session_id = await _session(client)

    _response, (_meta, *records) = await _export(client, session_id)

    assert [record["type"] for record in records] == [item["type"] for item in _ITEMS]
