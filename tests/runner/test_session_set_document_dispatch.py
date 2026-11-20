"""Session-bound document writes and tool advertisement."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.server_capabilities import (
    invalidate_server_info,
    server_session_set_document_enabled,
)
from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    execute_tool,
    filter_session_document_tool_schema,
)
from omnigent.runtime.prompt import (
    SESSION_DOCUMENT_INSTRUCTION,
    build_instructions,
    build_native_startup_instructions,
    session_document_tool_exposed,
)
from omnigent.session_documents import (
    GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX,
    GOOGLE_DOCS_SELECTION_LABEL,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager

_TOOL = "sys_session_set_document"
_SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
_OTHER = "a" * 32
_DOCS = "https://docs.google.com/document"
_URL = f"{_DOCS}/d/attached/edit?tab=t.notes"
_SELECTION = GOOGLE_DOCS_SELECTION_LABEL
_ENTRY = f"{GOOGLE_DOCS_DOCUMENT_LABEL_PREFIX}attached"


async def _set_document(
    client: httpx.AsyncClient, args: dict[str, object], session: str | None = _SESSION
) -> dict[str, str]:
    return json.loads(
        await execute_tool(
            tool_name=_TOOL,
            arguments=json.dumps(args),
            server_client=client,
            conversation_id=session,
            agent_spec=AgentSpec(spec_version=1),
        )
    )


@pytest.mark.parametrize(
    "info",
    [
        {"features": {"google_docs": False}, "session_set_document": False},
        {"features": {"google_docs": True}},
        {"session_set_document": "true"},
        {"session_set_document": True},
    ],
    ids=["disabled", "older-server", "invalid-capability", "enabled"],
)
async def test_document_tool_and_prompt_follow_connected_server(info: dict[str, object]) -> None:
    current_info = info
    unavailable = False
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if unavailable:
            return httpx.Response(503)
        return httpx.Response(200, json=current_info)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://server.example"
    ) as client:
        enabled = await server_session_set_document_enabled(client)
        assert enabled is (info.get("session_set_document") is True)
        spec = AgentSpec(spec_version=1, instructions="Authored instructions")
        raw_schemas = ToolManager(spec, session_set_document_enabled=True).get_tool_schemas()
        visible = filter_session_document_tool_schema(raw_schemas, enabled=enabled)
        assert session_document_tool_exposed(visible) is enabled
        assert not session_document_tool_exposed(ToolManager(spec).get_tool_schemas())
        assert (SESSION_DOCUMENT_INSTRUCTION in build_instructions(spec, None, visible)) is enabled
        for relay_spec in (spec, None):
            relay = build_native_relay_tool_schemas(
                relay_spec, session_set_document_enabled=enabled
            )
            exposed = session_document_tool_exposed(relay)
            assert exposed is enabled
            prompt = build_native_startup_instructions(
                relay_spec, session_document_tool_exposed=exposed
            )
            assert (SESSION_DOCUMENT_INSTRUCTION in prompt) is enabled
            if enabled:
                schema = next(tool for tool in relay if tool["name"] == _TOOL)
                assert schema["parameters"] == {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                }
        if not enabled:
            output = await _set_document(client, {"url": _URL})
            assert set(output) == {"error"}
        assert requests == ["/v1/info"]
        current_info = {"session_set_document": not enabled}
        invalidate_server_info(client)
        unavailable = True
        assert not await server_session_set_document_enabled(client)
        unavailable = False
        assert await server_session_set_document_enabled(client) is not enabled
        assert requests == ["/v1/info"] * 3


@pytest.mark.parametrize(
    "args",
    [
        {"url": "https://example.test/document/d/attached/edit"},
        {"url": f"{_URL}#heading=h.section"},
        {"url": f"{_URL}&resourcekey=access"},
        {"url": f"{_DOCS}/d/{'a' * 110}/edit"},
        {"url": f"{_URL}{'a' * 230}"},
        {"url": None},
        {"url": _URL, "session_id": _OTHER},
    ],
    ids=["foreign", "deep-link", "access-key", "long-key", "long-url", "missing-url", "target"],
)
async def test_document_tool_rejects_invalid_input_before_http(args: dict[str, object]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Invalid input made a request to {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://server.example"
    ) as client:
        assert set(await _set_document(client, args)) == {"error"}
        assert set(await _set_document(client, {"url": _URL}, session=None)) == {"error"}
        assert set(await _set_document(client, {"url": _URL}, session="../other")) == {"error"}


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "unrelated-read",
        "denied-read",
        "denied-write",
        "missing-entry",
        "unrelated-write",
        "lost-write",
        "invalid-json",
    ],
)
async def test_document_tool_requires_confirmation_from_calling_session(
    failure: str | None,
) -> None:
    requests = []
    patch = {_SELECTION: _URL, _ENTRY: _URL}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"session_set_document": True})
        if request.method == "GET":
            if failure == "denied-read":
                return httpx.Response(403)
            return httpx.Response(
                200,
                json={
                    "id": _OTHER if failure == "unrelated-read" else _SESSION,
                    "labels": {_SELECTION: f"{_DOCS}/d/old/edit"},
                },
            )
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == _SESSION
        assert json.loads(request.content) == {"labels": patch}
        if failure == "lost-write":
            raise httpx.ReadTimeout("disconnected", request=request)
        if failure == "denied-write":
            return httpx.Response(403)
        if failure == "invalid-json":
            return httpx.Response(200, text="bad response")
        return httpx.Response(
            200,
            json={
                "id": _OTHER if failure == "unrelated-write" else _SESSION,
                "labels": {_SELECTION: _URL} if failure == "missing-entry" else patch,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://server.example"
    ) as client:
        output = await _set_document(client, {"url": f"{_DOCS}/u/0/d/attached/edit#tab=t.notes"})
    assert requests[:2] == [("GET", "/v1/info"), ("GET", f"/v1/sessions/{_SESSION}/labels")]
    assert requests[2:] == (
        []
        if failure in {"unrelated-read", "denied-read"}
        else [("PATCH", f"/v1/sessions/{_SESSION}")]
    )
    if failure is None:
        assert output == {"url": _URL}
    else:
        assert set(output) == {"error"}
