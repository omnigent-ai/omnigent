"""``GET /v1/sessions/{id}/export?format=ipynb`` downloads a session as a notebook."""

from __future__ import annotations

import json

import httpx
import pytest

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
        "data": {
            "call_id": "call_1",
            "output": json.dumps({"stdout": "1501 sales.csv\n", "stderr": "", "exit_code": 0}),
        },
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


async def test_exports_the_session_as_a_notebook_attachment(client: httpx.AsyncClient) -> None:
    session_id = await _session(client)
    link = f"https://omnigent.example/c/{session_id}?o=7"

    response = await client.get(
        f"/v1/sessions/{session_id}/export", params={"format": "ipynb", "link": link}
    )

    assert response.status_code == 200, response.text
    # The web shows the menu item only on servers that advertise the export.
    assert (await client.get("/v1/info")).json()["session_notebook_export"] is True
    assert response.headers["content-type"].startswith("application/x-ipynb+json")
    assert response.headers["content-disposition"] == f'attachment; filename="{session_id}.ipynb"'
    notebook = response.json()
    assert (notebook["nbformat"], notebook["nbformat_minor"]) == (4, 5)
    header, *cells = notebook["cells"]
    # The header names the agent and model the session view shows.
    session = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert session["llm_model"]
    assert {
        f"- **Session:** `{session_id}`",
        "- **Agent:** test-agent",
        f"- **Model:** {session['llm_model']}",
        f"- **Link:** {link}",
    } <= set(header["source"].splitlines())
    assert [(cell["cell_type"], cell["source"]) for cell in cells[:3]] == [
        ("markdown", "**User**\n\nCount the rows"),
        ("code", "%%bash\nwc -l sales.csv"),
        ("markdown", "**Assistant**\n\n1,500 rows plus a header."),
    ]
    assert cells[1]["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": "1501 sales.csv\n"}
    ]


async def test_leaves_outputs_out_on_request(client: httpx.AsyncClient) -> None:
    session_id = await _session(client)

    response = await client.get(
        f"/v1/sessions/{session_id}/export", params={"format": "ipynb", "outputs": "false"}
    )

    code = [cell for cell in response.json()["cells"] if cell["cell_type"] == "code"]
    assert [cell["outputs"] for cell in code] == [[]]


async def test_says_when_the_session_is_longer_than_the_export(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _session(client)
    monkeypatch.setattr(routes_items, "_MAX_EXPORT_ITEMS", 2)

    response = await client.get(f"/v1/sessions/{session_id}/export", params={"format": "ipynb"})

    header, *cells = response.json()["cells"]
    assert "the session continues past this notebook's last cell" in header["source"]
    assert [cell["source"] for cell in cells][:2] == [
        "**User**\n\nCount the rows",
        "%%bash\nwc -l sales.csv",
    ]
    assert not any("1,500 rows" in cell["source"] for cell in cells)


async def test_pairs_a_call_with_its_output_across_store_pages(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _session(client)
    # Two items a page: the shell call ends the first page, its output starts the second.
    monkeypatch.setattr(routes_items, "_EXPORT_PAGE_SIZE", 2)

    response = await client.get(f"/v1/sessions/{session_id}/export", params={"format": "ipynb"})

    header, *cells = response.json()["cells"]
    assert "continues past" not in header["source"]
    assert [cell["source"] for cell in cells[:3]] == [
        "**User**\n\nCount the rows",
        "%%bash\nwc -l sales.csv",
        "**Assistant**\n\n1,500 rows plus a header.",
    ]
    assert cells[1]["outputs"] == [
        {"output_type": "stream", "name": "stdout", "text": "1501 sales.csv\n"}
    ]


@pytest.mark.parametrize(
    ("path", "params", "status"),
    [
        ("/v1/sessions/conv_missing/export", {"format": "ipynb"}, 404),
        (None, {"format": "jsonl"}, 422),
        (None, {}, 422),
        (None, {"format": "ipynb", "link": "javascript:alert(1)"}, 422),
    ],
    ids=["unknown-session", "unsupported-format", "no-format", "non-web-link"],
)
async def test_rejects_what_it_cannot_export(
    client: httpx.AsyncClient, path: str | None, params: dict[str, str], status: int
) -> None:
    session_id = await _session(client)
    response = await client.get(path or f"/v1/sessions/{session_id}/export", params=params)
    assert response.status_code == status, response.text
