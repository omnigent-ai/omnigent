"""Tests for the cross-harness ``sys_ask_user_question`` tool surface.

Covers the runner-side half of the feature:

- ``_execute_ask_user_question_tool``: the blocking ``server_client.post``
  to the server ``/ask_user_question`` route — correct URL / body
  passthrough, verbatim JSON response, and clean error JSON on failure.
- Registration in ``omnigent.tools.builtins``: spec-gated (unlike
  ``browser_*``, NOT auto-registered).
- Native-relay exposure: ``build_native_relay_tool_schemas`` surfaces the
  tool's schema when the spec declares it — native harnesses (claude-native,
  codex-native, opencode-native, cursor-native, hermes-native,
  antigravity-native) ignore ``request.tools`` entirely, so a miss here
  means the tool is invisible to them even though it's "registered".
- ``execute_tool()`` actually dispatches the call (not just advertises it).
"""

from __future__ import annotations

import json

import httpx
import pytest

import omnigent.tools.builtins as builtins_mod
from omnigent.runner.tool_dispatch import (
    _ASK_USER_QUESTION_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_ask_user_question_tool,
    build_native_relay_tool_schemas,
    execute_tool,
)
from omnigent.spec.types import AgentSpec, BuiltinToolConfig, ToolsConfig

_QUESTIONS_ARGS = {
    "questions": [
        {
            "question": "Which framework?",
            "header": "Framework",
            "options": [
                {"label": "React", "description": "Component-based UI library."},
                {"label": "Vue", "description": "Progressive framework.", "recommended": True},
            ],
            "multiSelect": False,
        }
    ]
}


# ── Helpers ──────────────────────────────────────────────────────


class _RecordingResponse:
    """Minimal httpx response stub with a scripted body."""

    def __init__(self, *, status_code: int = 200, body: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    @property
    def text(self) -> str:
        """Return the JSON body as text (what the tool returns verbatim)."""
        return json.dumps(self._body)


class _RecordingClient:
    """httpx.AsyncClient stub that records the POST and returns a script."""

    def __init__(self, response: _RecordingResponse | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object], object]] = []
        self._response = response or _RecordingResponse(body={"questions": [], "answers": {}})

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object] | None = None,
        timeout: object = None,
    ) -> _RecordingResponse:
        """Record the call and return the scripted response."""
        self.calls.append((url, json or {}, timeout))
        return self._response


class _ErrorClient:
    """httpx.AsyncClient stub whose POST raises a generic HTTPError."""

    async def post(self, url: str, **_: object) -> _RecordingResponse:
        """Raise a connect error the tool must surface as an error string."""
        raise httpx.ConnectError("connection refused")


# ── _execute_ask_user_question_tool ────────────────────────────────


@pytest.mark.asyncio
async def test_posts_questions_to_ask_user_question_route() -> None:
    """The tool POSTs the raw arguments verbatim to the session route."""
    scripted = {
        "questions": _QUESTIONS_ARGS["questions"],
        "answers": {"Which framework?": "Vue"},
    }
    client = _RecordingClient(_RecordingResponse(body=scripted))
    out = await _execute_ask_user_question_tool(
        _QUESTIONS_ARGS,
        server_client=client,
        conversation_id="conv_abc",
    )

    assert len(client.calls) == 1
    url, body, timeout = client.calls[0]
    assert url == "/v1/sessions/conv_abc/ask_user_question"
    assert body == _QUESTIONS_ARGS
    # Read budget must be long enough to wait for a human — a day, like the
    # platform's default ask_timeout.
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 86400.0
    assert json.loads(out) == scripted


@pytest.mark.asyncio
async def test_http_error_returns_error_json() -> None:
    """A generic HTTP error is surfaced as an error JSON, not raised."""
    out = await _execute_ask_user_question_tool(
        _QUESTIONS_ARGS,
        server_client=_ErrorClient(),
        conversation_id="conv_abc",
    )
    parsed = json.loads(out)
    assert "sys_ask_user_question failed" in parsed["error"]


@pytest.mark.asyncio
async def test_4xx_returns_error_json() -> None:
    """A >=400 response body is reported as an error string, not raised."""
    client = _RecordingClient(_RecordingResponse(status_code=400, body={"detail": "bad input"}))
    out = await _execute_ask_user_question_tool(
        _QUESTIONS_ARGS,
        server_client=client,
        conversation_id="conv_abc",
    )
    parsed = json.loads(out)
    assert "sys_ask_user_question returned 400" in parsed["error"]


@pytest.mark.asyncio
async def test_requires_server_and_session() -> None:
    """Missing server_client or conversation_id fails loud with JSON."""
    out_no_client = await _execute_ask_user_question_tool(
        _QUESTIONS_ARGS, server_client=None, conversation_id="conv"
    )
    assert "requires server access" in json.loads(out_no_client)["error"]

    out_no_conv = await _execute_ask_user_question_tool(
        _QUESTIONS_ARGS, server_client=_RecordingClient(), conversation_id=None
    )
    assert "requires a session id" in json.loads(out_no_conv)["error"]


# ── execute_tool() actually dispatches (not just advertises) ─────


@pytest.mark.asyncio
async def test_execute_tool_dispatches_to_ask_user_question_branch() -> None:
    """``execute_tool()`` routes ``sys_ask_user_question`` to the handler
    above — being visible in the schema without this branch means it
    silently fails for every caller."""
    scripted = {"questions": [], "answers": {"Which framework?": "Vue"}}
    client = _RecordingClient(_RecordingResponse(body=scripted))
    out = await execute_tool(
        tool_name="sys_ask_user_question",
        arguments=json.dumps(_QUESTIONS_ARGS),
        server_client=client,
        terminal_registry=None,
        resource_registry=None,
        agent_spec=None,
        conversation_id="conv_abc",
        task_id="task_abc",
        agent_id="agent_abc",
        agent_name=None,
        runner_workspace=None,
        mcp_manager=None,
        session_inbox=None,
        session_async_tasks=None,
        harness_client=None,
        publish_event=None,
        filesystem_registry=None,
    )
    assert json.loads(out) == scripted
    assert len(client.calls) == 1
    assert client.calls[0][0] == "/v1/sessions/conv_abc/ask_user_question"


# ── Registration: spec-gated, not framework-owned ─────────────────


def test_reserved_but_not_framework_owned() -> None:
    """
    Unlike ``browser_*`` (framework-owned, always-on), ``sys_ask_user_question``
    IS instantiable via the registry — an agent spec must declare it in
    ``tools.builtins`` to get it, following the ``web_search`` /
    ``upload_file`` pattern.
    """
    assert "sys_ask_user_question" in builtins_mod.BUILTIN_NAMES
    assert "sys_ask_user_question" in builtins_mod.INSTANTIABLE_BUILTINS
    tool = builtins_mod.get_builtin_tool("sys_ask_user_question")
    assert tool is not None
    assert tool.name() == "sys_ask_user_question"


def test_toolmanager_does_not_auto_register_without_opt_in() -> None:
    """A bare spec (no ``tools.builtins``) does NOT register the tool."""
    from omnigent.tools.manager import ToolManager

    mgr = ToolManager(AgentSpec(spec_version=1))
    assert mgr.get_tool("sys_ask_user_question") is None


# ── Native-relay exposure ────────────────────────────────────────


def test_ask_user_question_in_native_relay_union() -> None:
    """The relay builtin union must include the tool name."""
    assert _ASK_USER_QUESTION_TOOLS <= _NATIVE_RELAY_BUILTIN_TOOLS


def test_native_relay_surfaces_schema_when_spec_declares_it() -> None:
    """
    A spec that declares ``sys_ask_user_question`` in ``tools.builtins``
    surfaces its schema on the native relay — the ONLY tool surface every
    native harness (claude-native, codex-native, opencode-native,
    cursor-native, hermes-native, antigravity-native) sees.
    """
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name="sys_ask_user_question")]),
    )
    schemas = build_native_relay_tool_schemas(spec)
    matching = [s for s in schemas if s["name"] == "sys_ask_user_question"]
    assert len(matching) == 1
    assert matching[0]["description"]
    assert matching[0]["parameters"]["type"] == "object"


def test_native_relay_omits_schema_for_bare_spec() -> None:
    """Without the spec opt-in, the relay does not advertise the tool."""
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1))
    names = {s["name"] for s in schemas}
    assert "sys_ask_user_question" not in names
