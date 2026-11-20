"""Runner-side thingy tools: registration, the server gate, and calls against the real routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import jsonschema
import pytest
import pytest_asyncio

from omnigent.automations_availability import THINGIES_HEADER, without_thingy_tools
from omnigent.runner._entry import _AgentSpecCache, _resolve_agent_spec_from_server
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.thingy_tools import THINGY_TOOL_NAMES, execute_thingy_tool
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
from omnigent.stores.thingy_store.local import LocalThingyStore
from omnigent.tools import ToolManager
from omnigent.tools.builtins.thingies import THINGY_TOOLS
from tests.server.helpers import build_agent_bundle

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
_CARDS = {"cards": {"type": "object", "properties": {"title": {"type": "string"}}}}


def _registered(spec: AgentSpec) -> set[str]:
    return {schema["function"]["name"] for schema in ToolManager(spec).get_tool_schemas()}


def test_agents_get_the_thingy_tools_unless_their_spec_or_server_turns_them_off() -> None:
    assert len(THINGY_TOOL_NAMES) == 13
    assert all(name.startswith("sys_thingy_") for name in THINGY_TOOL_NAMES)
    # Native harnesses see only the relay's names, so the tools must be listed there.
    assert (
        THINGY_TOOL_NAMES <= _ALL_LOCAL_TOOLS and THINGY_TOOL_NAMES <= _NATIVE_RELAY_BUILTIN_TOOLS
    )

    spec = AgentSpec(spec_version=1)
    assert _registered(spec) >= THINGY_TOOL_NAMES
    relayed = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}
    assert relayed >= THINGY_TOOL_NAMES
    off = AgentSpec(
        spec_version=1, tool_groups=parse_tool_groups({"thingies": False}, spawn=False)
    )
    assert not THINGY_TOOL_NAMES & _registered(off)
    child = AgentSpec(spec_version=1, name="child")
    stripped = without_thingy_tools(AgentSpec(spec_version=1, sub_agents=[child]))
    assert not THINGY_TOOL_NAMES & _registered(stripped)
    assert not THINGY_TOOL_NAMES & _registered(stripped.sub_agents[0])
    assert not THINGY_TOOL_NAMES & {s["name"] for s in build_native_relay_tool_schemas(stripped)}


@pytest.mark.parametrize("tool", THINGY_TOOLS)
def test_tool_schemas_are_valid_json_schema(tool: type) -> None:
    schema = tool().get_schema()
    assert schema["function"]["name"] == tool.name()
    jsonschema.Draft202012Validator.check_schema(schema["function"]["parameters"])
    assert "mcp" not in schema["function"]["description"].lower()


@pytest.mark.asyncio
async def test_the_runner_keeps_the_tools_only_when_the_server_turns_thingies_on(
    tmp_path: Path,
) -> None:
    bundle = build_agent_bundle("thingy-agent")
    # No header is a server without thingies, so the tools would only fail.
    gate_headers = [{THINGIES_HEADER: "0"}, {THINGIES_HEADER: "1"}, {}]

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
                client, _AgentSpecCache(tmp_path), "ag_thingy", session_id="session"
            )
            for _ in range(3)
        ]
    assert [_registered(result.spec) >= THINGY_TOOL_NAMES for result in results] == [
        False,
        True,
        False,
    ]


@pytest_asyncio.fixture()
async def server(db_uri: str, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The real app in single-user mode, with thingies on."""
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversations = SqlAlchemyConversationStore(db_uri)
    # The calling session, which a new thingy is shown beside.
    conversations.create_conversation(conversation_id=SESSION)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "thingies"}),
        thingy_store=LocalThingyStore(str(tmp_path / "thingies")),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _call(client: httpx.AsyncClient, tool: str, call_id: str | None = None, **args) -> dict:
    output = await execute_thingy_tool(
        tool, json.dumps(args), server_client=client, conversation_id=SESSION, call_id=call_id
    )
    return json.loads(output)


@pytest.mark.asyncio
async def test_an_agent_builds_a_thingy_and_works_with_its_records_and_actions(
    server: httpx.AsyncClient,
) -> None:
    created = await _call(server, "sys_thingy_create", name="Board")
    thingy_id = created["id"]
    assert created["browse_url"] == f"/thingy/{thingy_id}"
    assert created["shown_in_session"] == SESSION
    shown = (await server.get(f"/v1/thingies/sessions/{SESSION}")).json()["items"]
    assert [item["id"] for item in shown] == [thingy_id]

    revision = await _call(
        server,
        "sys_thingy_revision_create",
        thingy_id=thingy_id,
        html="<main>Board</main>",
        sdk_version=1,
        capabilities=["data.read", "data.write"],
        collections=_CARDS,
        guide="# Cards",
        actions=[{"name": "run_card", "description": "Work on a card."}],
    )
    unpublished = await _call(
        server, "sys_thingy_records", thingy_id=thingy_id, op="list", collection="cards"
    )
    assert "not been published" in unpublished["error"]
    published = await _call(
        server,
        "sys_thingy_publish",
        thingy_id=thingy_id,
        revision_id=revision["id"],
        if_version=None,
    )
    assert published["published_revision"] == revision["id"]
    described = await _call(server, "sys_thingy_describe", thingy_id=thingy_id)
    assert described["guide"] == "# Cards" and described["collections"] == _CARDS
    source = await _call(server, "sys_thingy_revision_get", thingy_id=thingy_id)
    assert source["html"] == "<main>Board</main>"

    # A retried call with the same call ID is applied once.
    first = await _call(
        server,
        "sys_thingy_records",
        "call-1",
        thingy_id=thingy_id,
        op="create",
        collection="cards",
        value={"title": "Ship"},
    )
    again = await _call(
        server,
        "sys_thingy_records",
        "call-1",
        thingy_id=thingy_id,
        op="create",
        collection="cards",
        value={"title": "Ship"},
    )
    assert again == first
    replaced = await _call(
        server,
        "sys_thingy_records",
        thingy_id=thingy_id,
        op="replace",
        collection="cards",
        id=first["id"],
        value={"title": "Shipped"},
        if_version=first["version"],
        operation_id="rename-card",
    )
    stale = await _call(
        server,
        "sys_thingy_records",
        thingy_id=thingy_id,
        op="replace",
        collection="cards",
        id=first["id"],
        value={"title": "Stale"},
        if_version=first["version"],
        operation_id="stale-card",
    )
    assert stale["error"]["code"] == "CONFLICT"
    listed = await _call(
        server, "sys_thingy_records", thingy_id=thingy_id, op="list", collection="cards"
    )
    assert listed["items"] == [replaced]
    fetched = await _call(
        server,
        "sys_thingy_records",
        thingy_id=thingy_id,
        op="get",
        collection="cards",
        id=first["id"],
    )
    assert fetched == {"record": replaced}
    missing = await _call(
        server, "sys_thingy_records", thingy_id=thingy_id, op="get", collection="cards", id="nope"
    )
    assert missing == {"record": None}
    bad_op = await _call(
        server, "sys_thingy_records", thingy_id=thingy_id, op="upsert", collection="cards"
    )
    assert "op must be one of" in bad_op["error"]
    deleted = await _call(
        server,
        "sys_thingy_records",
        thingy_id=thingy_id,
        op="delete",
        collection="cards",
        id=first["id"],
        if_version=replaced["version"],
    )
    assert deleted["deleted"] is True

    request = await server.post(
        f"/v1/thingies/{thingy_id}/actions",
        json={"action": "run_card", "params": {}, "operation_id": "r1"},
    )
    action = request.json()
    ledger = await _call(server, "sys_thingy_action_list", thingy_id=thingy_id)
    assert [item["id"] for item in ledger["items"]] == [action["id"]]
    done = await _call(
        server,
        "sys_thingy_action_update",
        thingy_id=thingy_id,
        id=action["id"],
        if_version=action["version"],
        state="done",
        summary="Shipped **it**",
    )
    assert done["value"]["state"] == "done" and done["value"]["summary"] == "Shipped **it**"

    comment = await server.post(
        f"/v1/thingies/{thingy_id}/comments",
        json={"body": "Rename it", "anchor": {"text": "Board"}, "operation_id": "k1"},
    )
    drafts = await _call(server, "sys_thingy_comment_list", thingy_id=thingy_id, status="draft")
    assert [item["id"] for item in drafts["items"]] == [comment.json()["id"]]
    await _call(server, "sys_thingy_rename", thingy_id=thingy_id, name="Launch board")
    addressed = await _call(
        server,
        "sys_thingy_comment_update",
        thingy_id=thingy_id,
        id=comment.json()["id"],
        status="addressed",
    )
    assert addressed["status"] == "addressed"
    listed_thingies = await _call(server, "sys_thingy_list")
    assert [item["name"] for item in listed_thingies["items"]] == ["Launch board"]
    got = await _call(server, "sys_thingy_get", thingy_id=thingy_id)
    assert got["permissions"]["manage"] is True


@pytest.mark.asyncio
async def test_record_calls_follow_a_publish_made_meanwhile(server: httpx.AsyncClient) -> None:
    thingy_id = (await _call(server, "sys_thingy_create", name="Board", session_id=None))["id"]
    revisions = []
    for html in ("<main>1</main>", "<main>2</main>"):
        revisions.append(
            await _call(
                server,
                "sys_thingy_revision_create",
                thingy_id=thingy_id,
                html=html,
                sdk_version=1,
                capabilities=["data.read"],
                collections=_CARDS,
            )
        )
    first = await _call(
        server,
        "sys_thingy_publish",
        thingy_id=thingy_id,
        revision_id=revisions[0]["id"],
        if_version=None,
    )
    original_request = server.request
    published_second = False

    async def publish_after_reading(method: str, url: str, **kwargs):
        # Another publish lands between the tool's metadata read and its record call.
        nonlocal published_second
        response = await original_request(method, url, **kwargs)
        if method == "GET" and url == f"/v1/thingies/{thingy_id}" and not published_second:
            published_second = True
            await original_request(
                "POST",
                f"/v1/thingies/{thingy_id}/publish",
                json={"revision_id": revisions[1]["id"], "if_version": first["version"]},
            )
        return response

    server.request = publish_after_reading  # type: ignore[method-assign]
    page = await _call(
        server, "sys_thingy_records", thingy_id=thingy_id, op="list", collection="cards"
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
async def test_a_server_without_thingies_says_so(status: int, body: dict) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://s"
    ) as client:
        result = await _call(client, "sys_thingy_list")
    assert result == {"error": "this Omnigent server has no thingies"}
    assert seen[0].headers[ORCHESTRATION_SOURCE_HEADER] == SESSION


@pytest.mark.asyncio
async def test_invalid_arguments_never_reach_the_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://s"
    ) as client:
        for tool, args in (
            ("sys_thingy_get", {"thingy_id": "../sessions"}),
            ("sys_thingy_describe", {}),
            ("sys_thingy_comment_update", {"thingy_id": "a" * 32, "status": "addressed"}),
        ):
            assert "invalid_input" in (await _call(client, tool, **args))["error"]
        malformed = await execute_thingy_tool(
            "sys_thingy_list", "{", server_client=client, conversation_id=SESSION
        )
        assert "malformed" in json.loads(malformed)["error"]
    no_server = await execute_thingy_tool(
        "sys_thingy_list", "{}", server_client=None, conversation_id=SESSION
    )
    assert "requires server access" in json.loads(no_server)["error"]


@pytest.mark.asyncio
async def test_an_agent_changes_one_region_of_a_page_without_sending_it_again(
    server: httpx.AsyncClient,
) -> None:
    thingy_id = (await _call(server, "sys_thingy_create", name="Radar"))["id"]
    page = "<main><!-- view:start -->plain<!-- view:end --><footer>kit</footer></main>"
    base = await _call(
        server,
        "sys_thingy_revision_create",
        thingy_id=thingy_id,
        html=page,
        sdk_version=1,
        capabilities=["data.read"],
        collections=_CARDS,
        guide="# Radar",
    )

    derived = await _call(
        server,
        "sys_thingy_revision_create",
        thingy_id=thingy_id,
        base_revision_id=base["id"],
        edits=[{"start": "<!-- view:start -->", "end": "<!-- view:end -->", "new": "<svg/>"}],
    )
    missed = await _call(
        server,
        "sys_thingy_revision_create",
        thingy_id=thingy_id,
        base_revision_id=base["id"],
        edits=[{"old": "<aside>", "new": "x"}],
    )

    source = await _call(
        server, "sys_thingy_revision_get", thingy_id=thingy_id, revision_id=derived["id"]
    )
    assert (
        source["html"]
        == "<main><!-- view:start --><svg/><!-- view:end --><footer>kit</footer></main>"
    )
    assert source["guide"] == "# Radar" and source["collections"] == _CARDS
    assert missed["error"]["details"] == {"edit": 1, "reason": "not_found"}
