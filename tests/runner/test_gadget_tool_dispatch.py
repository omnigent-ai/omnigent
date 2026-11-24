"""Runner-side gadget tools: registration, the server gate, and calls against the real routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import jsonschema
import pytest
import pytest_asyncio

from omnigent.automations_availability import GADGETS_HEADER, without_gadget_tools
from omnigent.runner._entry import _AgentSpecCache, _resolve_agent_spec_from_server
from omnigent.runner.gadget_tools import GADGET_TOOL_NAMES, execute_gadget_tool
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    build_native_relay_tool_schemas,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.spec.parser import parse_tool_groups
from omnigent.spec.types import AgentSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.gadget_store.local import LocalGadgetStore
from omnigent.tools import ToolManager
from omnigent.tools.builtins.gadgets import GADGET_TOOLS
from tests.server.helpers import build_agent_bundle

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
OTHER_SESSION = "7d1c2e0f4b5a49c3a8e6f0b1c2d3e4f5"
_CARDS = {"cards": {"type": "object", "properties": {"title": {"type": "string"}}}}


def _registered(spec: AgentSpec) -> set[str]:
    return {schema["function"]["name"] for schema in ToolManager(spec).get_tool_schemas()}


def test_agents_get_the_gadget_tools_unless_their_spec_or_server_turns_them_off() -> None:
    assert len(GADGET_TOOL_NAMES) == 13
    assert all(name.startswith("sys_gadget_") for name in GADGET_TOOL_NAMES)
    # Native harnesses see only the relay's names, so the tools must be listed there.
    assert (
        GADGET_TOOL_NAMES <= _ALL_LOCAL_TOOLS and GADGET_TOOL_NAMES <= _NATIVE_RELAY_BUILTIN_TOOLS
    )

    spec = AgentSpec(spec_version=1)
    assert _registered(spec) >= GADGET_TOOL_NAMES
    relayed = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}
    assert relayed >= GADGET_TOOL_NAMES
    off = AgentSpec(spec_version=1, tool_groups=parse_tool_groups({"gadgets": False}, spawn=False))
    assert not GADGET_TOOL_NAMES & _registered(off)
    child = AgentSpec(spec_version=1, name="child")
    stripped = without_gadget_tools(AgentSpec(spec_version=1, sub_agents=[child]))
    assert not GADGET_TOOL_NAMES & _registered(stripped)
    assert not GADGET_TOOL_NAMES & _registered(stripped.sub_agents[0])
    assert not GADGET_TOOL_NAMES & {s["name"] for s in build_native_relay_tool_schemas(stripped)}


@pytest.mark.parametrize("tool", GADGET_TOOLS)
def test_tool_schemas_are_valid_json_schema(tool: type) -> None:
    schema = tool().get_schema()
    assert schema["function"]["name"] == tool.name()
    jsonschema.Draft202012Validator.check_schema(schema["function"]["parameters"])
    assert "mcp" not in schema["function"]["description"].lower()


@pytest.mark.asyncio
async def test_the_runner_keeps_the_tools_only_when_the_server_turns_gadgets_on(
    tmp_path: Path,
) -> None:
    bundle = build_agent_bundle("gadget-agent")
    # No header is a server without gadgets, so the tools would only fail.
    gate_headers = [{GADGETS_HEADER: "0"}, {GADGETS_HEADER: "1"}, {}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=bundle,
            headers={
                "X-Agent-Version": "1",
                "X-Agent-Session-Scoped": "true",
                **gate_headers.pop(0),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server.test"
    ) as client:
        results = [
            await _resolve_agent_spec_from_server(
                client, _AgentSpecCache(tmp_path), "ag_gadget", session_id="session"
            )
            for _ in range(3)
        ]
    assert [_registered(result.spec) >= GADGET_TOOL_NAMES for result in results] == [
        False,
        True,
        False,
    ]


@pytest_asyncio.fixture()
async def server(db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The real app in single-user mode, with gadgets on."""
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversations = SqlAlchemyConversationStore(db_uri)
    # The calling session, which a new gadget is shown beside.
    conversations.create_conversation(conversation_id=SESSION)
    conversations.create_conversation(conversation_id=OTHER_SESSION)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "gadgets"}),
        gadget_store=LocalGadgetStore(str(tmp_path / "gadgets")),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _call(client: httpx.AsyncClient, tool: str, call_id: str | None = None, **args) -> dict:
    output = await execute_gadget_tool(
        tool, json.dumps(args), server_client=client, conversation_id=SESSION, call_id=call_id
    )
    return json.loads(output)


@pytest.mark.asyncio
async def test_an_agent_builds_a_gadget_and_works_with_its_records_and_actions(
    server: httpx.AsyncClient,
) -> None:
    created = await _call(server, "sys_gadget_create", name="Board")
    gadget_id = created["id"]
    assert created["browse_url"] == f"/gadget/{gadget_id}"
    assert created["shown_in_session"] == SESSION
    shown = (await server.get(f"/v1/gadgets/sessions/{SESSION}")).json()["items"]
    assert [item["id"] for item in shown] == [gadget_id]

    revision = await _call(
        server,
        "sys_gadget_revision_create",
        gadget_id=gadget_id,
        html="<main>Board</main>",
        sdk_version=1,
        capabilities=["data.read", "data.write"],
        collections=_CARDS,
        guide="# Cards",
        actions=[{"name": "run_card", "description": "Work on a card."}],
    )
    unpublished = await _call(
        server, "sys_gadget_records", gadget_id=gadget_id, op="list", collection="cards"
    )
    assert "not been published" in unpublished["error"]
    published = await _call(
        server,
        "sys_gadget_publish",
        gadget_id=gadget_id,
        revision_id=revision["id"],
        if_version=None,
    )
    assert published["published_revision"] == revision["id"]
    described = await _call(server, "sys_gadget_describe", gadget_id=gadget_id)
    assert described["guide"] == "# Cards" and described["collections"] == _CARDS
    source = await _call(server, "sys_gadget_revision_get", gadget_id=gadget_id)
    assert source["html"] == "<main>Board</main>"

    # A retried call with the same call ID is applied once.
    first = await _call(
        server,
        "sys_gadget_records",
        "call-1",
        gadget_id=gadget_id,
        op="create",
        collection="cards",
        value={"title": "Ship"},
    )
    again = await _call(
        server,
        "sys_gadget_records",
        "call-1",
        gadget_id=gadget_id,
        op="create",
        collection="cards",
        value={"title": "Ship"},
    )
    assert again == first
    replaced = await _call(
        server,
        "sys_gadget_records",
        gadget_id=gadget_id,
        op="replace",
        collection="cards",
        id=first["id"],
        value={"title": "Shipped"},
        if_version=first["version"],
        operation_id="rename-card",
    )
    stale = await _call(
        server,
        "sys_gadget_records",
        gadget_id=gadget_id,
        op="replace",
        collection="cards",
        id=first["id"],
        value={"title": "Stale"},
        if_version=first["version"],
        operation_id="stale-card",
    )
    assert stale["error"]["code"] == "CONFLICT"
    listed = await _call(
        server, "sys_gadget_records", gadget_id=gadget_id, op="list", collection="cards"
    )
    assert listed["items"] == [replaced]
    fetched = await _call(
        server,
        "sys_gadget_records",
        gadget_id=gadget_id,
        op="get",
        collection="cards",
        id=first["id"],
    )
    assert fetched == {"record": replaced}
    missing = await _call(
        server, "sys_gadget_records", gadget_id=gadget_id, op="get", collection="cards", id="nope"
    )
    assert missing == {"record": None}
    bad_op = await _call(
        server, "sys_gadget_records", gadget_id=gadget_id, op="upsert", collection="cards"
    )
    assert "op must be one of" in bad_op["error"]
    deleted = await _call(
        server,
        "sys_gadget_records",
        gadget_id=gadget_id,
        op="delete",
        collection="cards",
        id=first["id"],
        if_version=replaced["version"],
    )
    assert deleted["deleted"] is True

    request = await server.post(
        f"/v1/gadgets/{gadget_id}/actions",
        json={"action": "run_card", "params": {}, "operation_id": "r1"},
    )
    action = request.json()
    ledger = await _call(server, "sys_gadget_action_list", gadget_id=gadget_id)
    assert [item["id"] for item in ledger["items"]] == [action["id"]]
    done = await _call(
        server,
        "sys_gadget_action_update",
        gadget_id=gadget_id,
        id=action["id"],
        if_version=action["version"],
        state="done",
        summary="Shipped **it**",
    )
    assert done["value"]["state"] == "done" and done["value"]["summary"] == "Shipped **it**"

    comment = await server.post(
        f"/v1/gadgets/{gadget_id}/comments",
        json={"body": "Rename it", "anchor": {"text": "Board"}, "operation_id": "k1"},
    )
    drafts = await _call(server, "sys_gadget_comment_list", gadget_id=gadget_id, status="draft")
    assert [item["id"] for item in drafts["items"]] == [comment.json()["id"]]
    await _call(server, "sys_gadget_rename", gadget_id=gadget_id, name="Launch board")
    addressed = await _call(
        server,
        "sys_gadget_comment_update",
        gadget_id=gadget_id,
        id=comment.json()["id"],
        status="addressed",
    )
    assert addressed["status"] == "addressed"
    listed_gadgets = await _call(server, "sys_gadget_list")
    assert [item["name"] for item in listed_gadgets["items"]] == ["Launch board"]
    got = await _call(server, "sys_gadget_get", gadget_id=gadget_id)
    assert got["permissions"]["manage"] is True


@pytest.mark.asyncio
async def test_record_calls_follow_a_publish_made_meanwhile(server: httpx.AsyncClient) -> None:
    gadget_id = (await _call(server, "sys_gadget_create", name="Board", session_id=None))["id"]
    revisions = []
    for html in ("<main>1</main>", "<main>2</main>"):
        revisions.append(
            await _call(
                server,
                "sys_gadget_revision_create",
                gadget_id=gadget_id,
                html=html,
                sdk_version=1,
                capabilities=["data.read"],
                collections=_CARDS,
            )
        )
    first = await _call(
        server,
        "sys_gadget_publish",
        gadget_id=gadget_id,
        revision_id=revisions[0]["id"],
        if_version=None,
    )
    original_request = server.request
    published_second = False

    async def publish_after_reading(method: str, url: str, **kwargs):
        # Another publish lands between the tool's metadata read and its record call.
        nonlocal published_second
        response = await original_request(method, url, **kwargs)
        if method == "GET" and url == f"/v1/gadgets/{gadget_id}" and not published_second:
            published_second = True
            await original_request(
                "POST",
                f"/v1/gadgets/{gadget_id}/publish",
                json={"revision_id": revisions[1]["id"], "if_version": first["version"]},
            )
        return response

    server.request = publish_after_reading  # type: ignore[method-assign]
    page = await _call(
        server, "sys_gadget_records", gadget_id=gadget_id, op="list", collection="cards"
    )
    assert published_second
    assert page == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"detail": "Not Found"}),
        (404, {"error": {"code": "NOT_FOUND", "message": "Not found", "request_id": None}}),
        (405, {"detail": "Method Not Allowed"}),
    ],
)
@pytest.mark.asyncio
async def test_a_server_without_gadgets_says_so(status: int, body: dict) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://s"
    ) as client:
        result = await _call(client, "sys_gadget_list")
    assert result == {"error": "this Omnigent server has no gadgets"}
    assert seen[0].headers[ORCHESTRATION_SOURCE_HEADER] == SESSION


@pytest.mark.asyncio
async def test_invalid_arguments_never_reach_the_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://s"
    ) as client:
        for tool, args in (
            ("sys_gadget_get", {"gadget_id": "../sessions"}),
            ("sys_gadget_describe", {}),
            ("sys_gadget_comment_update", {"gadget_id": "a" * 32, "status": "addressed"}),
            ("sys_gadget_records", {"gadget_id": "a" * 32, "op": ["list"]}),
            ("sys_gadget_records", {"gadget_id": "a" * 32, "op": {"list": 1}}),
        ):
            assert "invalid_input" in (await _call(client, tool, **args))["error"]
        malformed = await execute_gadget_tool(
            "sys_gadget_list", "{", server_client=client, conversation_id=SESSION
        )
        assert "malformed" in json.loads(malformed)["error"]
    no_server = await execute_gadget_tool(
        "sys_gadget_list", "{}", server_client=None, conversation_id=SESSION
    )
    assert "requires server access" in json.loads(no_server)["error"]


@pytest.mark.asyncio
async def test_an_agent_changes_one_region_of_a_page_without_sending_it_again(
    server: httpx.AsyncClient,
) -> None:
    gadget_id = (await _call(server, "sys_gadget_create", name="Radar"))["id"]
    page = "<main><!-- view:start -->plain<!-- view:end --><footer>kit</footer></main>"
    base = await _call(
        server,
        "sys_gadget_revision_create",
        gadget_id=gadget_id,
        html=page,
        sdk_version=1,
        capabilities=["data.read"],
        collections=_CARDS,
        guide="# Radar",
    )

    derived = await _call(
        server,
        "sys_gadget_revision_create",
        gadget_id=gadget_id,
        base_revision_id=base["id"],
        edits=[{"start": "<!-- view:start -->", "end": "<!-- view:end -->", "new": "<svg/>"}],
    )
    missed = await _call(
        server,
        "sys_gadget_revision_create",
        gadget_id=gadget_id,
        base_revision_id=base["id"],
        edits=[{"old": "<aside>", "new": "x"}],
    )

    source = await _call(
        server, "sys_gadget_revision_get", gadget_id=gadget_id, revision_id=derived["id"]
    )
    assert (
        source["html"]
        == "<main><!-- view:start --><svg/><!-- view:end --><footer>kit</footer></main>"
    )
    assert source["guide"] == "# Radar" and source["collections"] == _CARDS
    assert missed["error"]["details"] == {"edit": 1, "reason": "not_found"}


async def _create_card(
    client: httpx.AsyncClient,
    gadget_id: str,
    title: str,
    *,
    session: str = SESSION,
    call_id: str | None,
    invocation_id: str | None = None,
) -> dict:
    output = await execute_gadget_tool(
        "sys_gadget_records",
        json.dumps(
            {
                "gadget_id": gadget_id,
                "op": "create",
                "collection": "cards",
                "value": {"title": title},
            }
        ),
        server_client=client,
        conversation_id=session,
        call_id=call_id,
        invocation_id=invocation_id,
    )
    return json.loads(output)


@pytest.mark.asyncio
async def test_independent_calls_sharing_a_call_id_each_create_a_record_and_retries_do_not(
    server: httpx.AsyncClient,
) -> None:
    gadget_id = (await _call(server, "sys_gadget_create", name="Board"))["id"]
    revision = await _call(
        server,
        "sys_gadget_revision_create",
        gadget_id=gadget_id,
        html="<main>Board</main>",
        sdk_version=1,
        capabilities=["data.read", "data.write"],
        collections=_CARDS,
    )
    await _call(
        server,
        "sys_gadget_publish",
        gadget_id=gadget_id,
        revision_id=revision["id"],
        if_version=None,
    )
    # A harness can derive call IDs from a call's content, so independent identical calls in
    # one session repeat a call ID; each is its own output item.
    first = await _create_card(
        server, gadget_id, "Ship", call_id="call_same", invocation_id="fc_1"
    )
    second = await _create_card(
        server, gadget_id, "Ship", call_id="call_same", invocation_id="fc_2"
    )
    other = await _create_card(
        server, gadget_id, "Plan", call_id="call_same", invocation_id="fc_3"
    )
    assert "error" not in other
    assert len({first["id"], second["id"], other["id"]}) == 3
    # A retry of each invocation is the record that invocation made.
    assert (
        await _create_card(server, gadget_id, "Ship", call_id="call_same", invocation_id="fc_1")
        == first
    )
    assert (
        await _create_card(server, gadget_id, "Ship", call_id="call_same", invocation_id="fc_2")
        == second
    )
    # With only a call ID, the same one in another session is another invocation.
    here = await _create_card(server, gadget_id, "Ship", call_id="call_x")
    there = await _create_card(server, gadget_id, "Ship", session=OTHER_SESSION, call_id="call_x")
    assert here["id"] != there["id"]
    assert await _create_card(server, gadget_id, "Ship", call_id="call_x") == here
    listed = await _call(
        server, "sys_gadget_records", gadget_id=gadget_id, op="list", collection="cards"
    )
    assert len(listed["items"]) == 5


@pytest.mark.asyncio
async def test_the_runner_passes_each_calls_output_item_id_to_the_gadget_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.runner.tool_dispatch as tool_dispatch

    seen: list[tuple[str | None, str | None]] = []

    async def fake_gadget_tool(*_args, call_id=None, invocation_id=None, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append((call_id, invocation_id))
        return "{}"

    monkeypatch.setattr(tool_dispatch, "execute_gadget_tool", fake_gadget_tool)
    event = {
        "type": "response.output_item.done",
        "item": {
            "id": "fc_0a1b2c",
            "type": "function_call",
            "status": "action_required",
            "name": "sys_gadget_list",
            "arguments": "{}",
            "call_id": "call_same",
        },
    }
    assert tool_dispatch.get_item_id(event) == "fc_0a1b2c"
    assert tool_dispatch.get_item_id({"item": {"call_id": "call_same"}}) == ""
    await tool_dispatch.execute_tool(
        tool_name="sys_gadget_list",
        arguments="{}",
        conversation_id=SESSION,
        call_id=tool_dispatch.get_call_id(event),
        invocation_id=tool_dispatch.get_item_id(event),
    )
    assert seen == [("call_same", "fc_0a1b2c")]
