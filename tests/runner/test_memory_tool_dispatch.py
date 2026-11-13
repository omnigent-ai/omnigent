"""Tests for the ``memory_*`` tool surface (runner side).

* ``_execute_memory_tool`` sends each call to the right ``/v1/memory`` verb and
  URL with the calling session in the orchestration-source header, never sends
  an owner, and turns server errors and an unavailable server into clean errors.
* Registration: the five names are registered by ``ToolManager`` unless
  ``tool_groups.memory`` is off, and are members of the dispatch and relay sets.
* The names are the runner's only for an agent with memory on. Otherwise they
  behave as before memory existed: an agent's own tool by one of these names
  is advertised, routed and run as that tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _MEMORY_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_memory_tool,
    build_native_relay_tool_schemas,
    execute_tool,
    should_dispatch_locally,
)
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, ToolGroupsConfig
from omnigent.tools.manager import ToolManager

_NAMES = {"memory_search", "memory_get", "memory_list", "memory_write", "memory_delete"}
_SESSION = "0123456789abcdef0123456789abcdef"


class _Resp:
    def __init__(self, *, status_code: int = 200, body: object | None = None) -> None:
        self.status_code = status_code
        self._body = {} if body is None else body

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> object:
        return self._body


class _RecordingClient:
    """Records each call; ``/v1/info`` advertises memory unless told otherwise."""

    def __init__(self, response: _Resp | None = None, *, memory: object = True) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None, dict[str, str] | None]] = []
        self.info_calls = 0
        self._response = response or _Resp(body={"ok": True})
        self._info = _Resp(body={"memory": memory} if memory is not None else {})

    async def get(self, url: str, *, params=None, headers=None, timeout=None) -> _Resp:
        if url == "/v1/info":
            self.info_calls += 1
            return self._info
        self.calls.append(("GET", url, params, headers))
        return self._response

    async def post(self, url: str, *, json=None, headers=None, timeout=None) -> _Resp:
        self.calls.append(("POST", url, json, headers))
        return self._response

    async def put(self, url: str, *, json=None, headers=None, timeout=None) -> _Resp:
        self.calls.append(("PUT", url, json, headers))
        return self._response

    async def delete(self, url: str, *, params=None, headers=None, timeout=None) -> _Resp:
        self.calls.append(("DELETE", url, params, headers))
        return self._response


def _spec(groups: ToolGroupsConfig | None = None, *, search: bool = True) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        skills_filter="none",
        tool_groups=groups or ToolGroupsConfig(),
        memory_search_available=search,
    )


async def _call(client: _RecordingClient, name: str, args: dict[str, object]) -> dict[str, object]:
    return json.loads(
        await _execute_memory_tool(
            name, json.dumps(args), server_client=client, conversation_id=_SESSION
        )
    )


@pytest.mark.parametrize(
    ("name", "args", "expected"),
    [
        (
            "memory_search",
            {"query": "retries", "path_prefix": "/projects", "limit": 3},
            (
                "POST",
                "/v1/memory/search",
                {"query": "retries", "path_prefix": "/projects", "limit": 3},
            ),
        ),
        ("memory_get", {"path": "/a"}, ("GET", "/v1/memory/entry", {"path": "/a"})),
        ("memory_list", {"path_prefix": "/p"}, ("GET", "/v1/memory/entries", {"prefix": "/p"})),
        (
            "memory_list",
            {"path_prefix": "/p", "page_token": "/p/a"},
            ("GET", "/v1/memory/entries", {"prefix": "/p", "page_token": "/p/a"}),
        ),
        ("memory_list", {}, ("GET", "/v1/memory/entries", None)),
        (
            "memory_write",
            {"path": "/a", "content": "x", "description": "d"},
            ("PUT", "/v1/memory/entry", {"path": "/a", "content": "x", "description": "d"}),
        ),
        ("memory_delete", {"path": "/a"}, ("DELETE", "/v1/memory/entry", {"path": "/a"})),
    ],
)
async def test_each_tool_calls_its_route_with_the_session_header(name, args, expected) -> None:
    client = _RecordingClient()

    result = await _call(client, name, args)

    assert result == {"ok": True}
    assert client.calls == [(*expected, {ORCHESTRATION_SOURCE_HEADER: _SESSION})]


async def test_an_owner_supplied_by_the_model_is_never_forwarded() -> None:
    client = _RecordingClient()

    await _call(
        client,
        "memory_write",
        {"path": "/a", "content": "x", "actor_id": "user:victim", "user_id": "victim"},
    )
    await _call(client, "memory_search", {"query": "q", "actor_id": "user:victim"})

    assert all("actor_id" not in (call[2] or {}) for call in client.calls)
    assert all("user_id" not in (call[2] or {}) for call in client.calls)


async def test_capability_is_probed_once_per_connection() -> None:
    client = _RecordingClient()

    await _call(client, "memory_list", {})
    await _call(client, "memory_list", {})

    assert client.info_calls == 1


@pytest.mark.parametrize("memory", [False, None])
async def test_a_server_without_memory_gets_a_clear_error_and_no_call(memory) -> None:
    client = _RecordingClient(memory=memory)

    result = await _call(client, "memory_write", {"path": "/a", "content": "x"})

    assert "not available" in result["error"]
    assert client.calls == []


@pytest.mark.parametrize("name", ["memory_get", "memory_write", "memory_delete"])
async def test_path_is_required(name: str) -> None:
    client = _RecordingClient()

    result = await _call(client, name, {"content": "x"})

    assert "requires a string 'path'" in result["error"]
    assert client.calls == []


async def test_server_errors_are_reported_with_status_and_details() -> None:
    client = _RecordingClient(_Resp(status_code=403, body={"detail": "no"}))

    result = await _call(client, "memory_write", {"path": "/a", "content": "x"})

    assert result["error"] == "server returned 403"
    assert "no" in result["details"]


async def test_missing_client_and_bad_arguments_return_errors() -> None:
    assert (
        "requires server access"
        in json.loads(await _execute_memory_tool("memory_list", "{}", server_client=None))["error"]
    )
    assert (
        "malformed JSON"
        in json.loads(
            await _execute_memory_tool("memory_list", "{", server_client=_RecordingClient())
        )["error"]
    )
    assert (
        "must be an object"
        in json.loads(
            await _execute_memory_tool("memory_list", "[]", server_client=_RecordingClient())
        )["error"]
    )


async def test_execute_tool_routes_memory_names_to_the_memory_handler() -> None:
    client = _RecordingClient()

    result = await execute_tool(
        tool_name="memory_get",
        arguments=json.dumps({"path": "/a"}),
        server_client=client,
        agent_spec=_spec(),
        conversation_id=_SESSION,
    )

    assert json.loads(result) == {"ok": True}
    assert client.calls[0][1] == "/v1/memory/entry"


def test_memory_tools_are_in_the_dispatch_and_relay_sets() -> None:
    assert _MEMORY_TOOLS == _NAMES
    assert _NAMES <= _ALL_LOCAL_TOOLS
    assert _NAMES <= _NATIVE_RELAY_BUILTIN_TOOLS


def test_memory_names_are_the_runners_only_for_an_agent_with_memory() -> None:
    other_builtin = sorted(_ALL_LOCAL_TOOLS - _NAMES)[0]

    for name in _NAMES:
        assert should_dispatch_locally(name, _spec())
        assert not should_dispatch_locally(name, _spec(ToolGroupsConfig(memory=False)))
        assert not should_dispatch_locally(name)
    assert should_dispatch_locally(other_builtin)


def test_memory_names_are_not_reserved() -> None:
    from omnigent.tools.builtins import BUILTIN_NAMES

    assert not _NAMES & BUILTIN_NAMES


async def test_with_memory_off_a_memory_call_never_reaches_the_memory_server() -> None:
    client = _RecordingClient()

    result = await execute_tool(
        tool_name="memory_get",
        arguments=json.dumps({"path": "/a"}),
        server_client=client,
        agent_spec=_spec(ToolGroupsConfig(memory=False)),
        conversation_id=_SESSION,
    )

    assert "not enabled for this agent" in result
    assert client.calls == []
    assert client.info_calls == 0


async def test_an_agents_own_tool_named_like_a_memory_tool_runs_as_itself(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.yaml").write_text(
        "spec_version: 1\nname: own-memory\nexecutor:\n  config:\n    harness: claude-sdk\n"
    )
    (tmp_path / "tools" / "python").mkdir(parents=True)
    (tmp_path / "tools" / "python" / "memory_search.py").write_text(
        "from omnigent_client.tools import tool\n\n\n"
        "@tool\n"
        "def memory_search(query: str) -> str:\n"
        "    return f'own: {query}'\n"
    )
    spec = parse(tmp_path)
    client = _RecordingClient()

    result = await execute_tool(
        tool_name="memory_search",
        arguments=json.dumps({"query": "tabs"}),
        server_client=client,
        agent_spec=spec,
        conversation_id=_SESSION,
        local_tool_workdir=tmp_path,
    )

    assert spec.tool_groups.memory is False
    assert "own: tabs" in result
    assert client.calls == []
    assert not should_dispatch_locally("memory_search", spec)
    assert "memory_search" not in {s["name"] for s in build_native_relay_tool_schemas(spec)}


async def test_a_memory_name_defined_in_another_tool_file_keeps_its_own_schema(
    tmp_path: Path,
) -> None:
    """The loader registers @tool functions by name, so the file name doesn't matter."""
    (tmp_path / "config.yaml").write_text(
        "spec_version: 1\nname: own-memory\nexecutor:\n  config:\n    harness: claude-sdk\n"
    )
    (tmp_path / "tools" / "python").mkdir(parents=True)
    (tmp_path / "tools" / "python" / "helpers.py").write_text(
        "from omnigent_client.tools import tool\n\n\n"
        "@tool\ndef lookup(query: str) -> str:\n    return 'other'\n\n\n"
        "@tool\ndef memory_get(path: str) -> str:\n"
        '    """Own lookup."""\n'
        "    return 'own'\n"
    )
    spec = parse(tmp_path)
    client = _RecordingClient()

    await execute_tool(
        tool_name="memory_get",
        arguments=json.dumps({"path": "/a"}),
        server_client=client,
        agent_spec=spec,
        conversation_id=_SESSION,
        local_tool_workdir=tmp_path,
    )

    schemas = {
        s["function"]["name"]: s["function"]
        for s in ToolManager(spec, workdir=tmp_path).get_tool_schemas()
    }
    assert spec.tool_groups.memory is False
    assert schemas["memory_get"]["description"] == "Own lookup."
    assert not (_NAMES - {"memory_get"}) & set(schemas)
    assert client.calls == []
    assert not should_dispatch_locally("memory_get", spec)


def test_toolmanager_registers_memory_unless_the_group_is_off() -> None:
    def registered(spec: AgentSpec) -> set[str]:
        return {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}

    assert registered(_spec()) >= _NAMES
    assert not (_NAMES & registered(_spec(ToolGroupsConfig(memory=False))))


def test_native_relay_advertises_memory_and_honours_the_group() -> None:
    def relayed(spec: AgentSpec) -> set[str]:
        return {schema["name"] for schema in build_native_relay_tool_schemas(spec)}

    assert relayed(_spec()) >= _NAMES
    assert not (_NAMES & relayed(_spec(ToolGroupsConfig(memory=False))))


async def test_without_backend_search_memory_search_is_neither_offered_nor_proxied() -> None:
    spec = _spec(search=False)
    client = _RecordingClient()

    result = await execute_tool(
        tool_name="memory_search",
        arguments=json.dumps({"query": "tabs"}),
        server_client=client,
        agent_spec=spec,
        conversation_id=_SESSION,
    )

    registered = {s["function"]["name"] for s in ToolManager(spec).get_tool_schemas()}
    relayed = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}
    assert registered & _NAMES == relayed & _NAMES == _NAMES - {"memory_search"}
    assert not should_dispatch_locally("memory_search", spec)
    assert should_dispatch_locally("memory_list", spec)
    assert "not enabled for this agent" in result
    assert client.calls == []
    assert client.info_calls == 0


def test_schemas_are_valid_and_never_ask_for_an_owner() -> None:
    for schema in ToolManager(_spec()).get_tool_schemas():
        function = schema["function"]
        if function["name"] not in _NAMES:
            continue
        jsonschema.Draft202012Validator.check_schema(function["parameters"])
        assert function["parameters"]["additionalProperties"] is False
        assert not {"actor_id", "user_id", "owner"} & set(function["parameters"]["properties"])
