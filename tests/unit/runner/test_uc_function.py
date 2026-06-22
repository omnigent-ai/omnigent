"""Unit tests for :mod:`omnigent.runner.uc_function`.

Tests cover:

- SQL statement construction with parameterized queries
  (``_build_select_statement``).
- End-to-end ``execute_uc_function`` with a mocked
  ``WorkspaceClient`` — verifies the correct SQL is sent, the
  warehouse ID is forwarded, and the result is extracted from the
  SDK response.
- Error handling: missing warehouse ID, failed execution, no result
  data.
- Dispatch integration: ``_is_uc_function_tool`` and
  ``_execute_uc_function_tool`` in ``tool_dispatch.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.runner.uc_function import (
    _build_select_statement,
)
from omnigent.spec.types import LocalToolInfo, ToolRuntime

# ── Helpers ─────────────────────────────────────────────────────


@dataclass
class _FakeStatementStatus:
    """Stub for ``databricks.sdk.service.sql.StatementStatus``.

    :param state: The execution state, e.g.
        ``_FakeStatementState.SUCCEEDED``.
    :param error: Optional error payload for failed statements.
    """

    state: Any
    error: Any = None


@dataclass
class _FakeStatementError:
    """Stub for a statement execution error.

    :param message: Human-readable error message.
    """

    message: str


@dataclass
class _FakeResultData:
    """Stub for ``databricks.sdk.service.sql.ResultData``.

    :param data_array: The result rows, each a list of string
        column values.
    """

    data_array: list[list[str | None]] | None = None


@dataclass
class _FakeStatementResponse:
    """Stub for ``databricks.sdk.service.sql.StatementResponse``.

    :param status: Execution status.
    :param result: Result data payload.
    """

    status: _FakeStatementStatus | None = None
    result: _FakeResultData | None = None


class _FakeStatementState:
    """Enum stand-in for ``databricks.sdk.service.sql.StatementState``.

    Uses string ``value`` attributes matching the real enum so
    comparisons in production code work correctly.
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

    def __init__(self, value: str) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.value == other
        if hasattr(other, "value"):
            return self.value == other.value
        return NotImplemented


@dataclass
class _FakeAgentSpec:
    """Minimal agent spec stub for dispatch tests.

    :param local_tools: List of tool info objects the spec declares.
    :param executor: Executor config stub.
    """

    local_tools: list[LocalToolInfo] = field(default_factory=list)
    executor: Any = None


@dataclass
class _FakeExecutorSpec:
    """Minimal executor spec stub.

    :param auth: Auth config, e.g. a ``_FakeDatabricksAuth``.
    :param profile: Deprecated direct profile field.
    :param config: Compat config dict.
    """

    auth: Any = None
    profile: str | None = None
    config: dict[str, Any] | None = None


@dataclass
class _FakeDatabricksAuth:
    """Minimal Databricks auth stub.

    :param profile: Databricks config profile name, e.g.
        ``"oss"``.
    """

    profile: str


# ── _build_select_statement tests ───────────────────────────────


def test_build_select_no_args() -> None:
    """Empty args produce a bare ``SELECT func()`` with no parameters."""
    sql, params = _build_select_statement("cat.schema.func", {})
    assert sql == "SELECT `cat.schema.func`()"
    assert params == []


def test_build_select_bare_function_no_backticks() -> None:
    """Bare function names (no dots) are emitted unquoted.

    Built-in SQL functions like ``ai_query`` don't live in a
    catalog namespace and must not be backtick-quoted.
    """
    sql, params = _build_select_statement("ai_query", {"endpoint": "my-ep", "prompt": "hi"})
    # No backticks around ai_query.
    assert sql == "SELECT ai_query(:endpoint, :prompt)"
    assert len(params) == 2


def test_build_select_rejects_sql_injection_in_catalog_path() -> None:
    """catalog_path with SQL metacharacters is rejected.

    Prevents SQL injection via crafted tool specs — backticks,
    semicolons, parens, or spaces in catalog_path would alter the
    generated SQL structure.
    """
    with pytest.raises(ValueError, match="Invalid catalog_path"):
        _build_select_statement("func(); DROP TABLE x; --", {})


def test_build_select_rejects_sql_injection_in_param_name() -> None:
    """Parameter names with SQL metacharacters are rejected.

    Parameter names from LLM tool-call arguments are interpolated
    as ``:name`` markers. SQL metacharacters in keys would alter
    the query structure.
    """
    with pytest.raises(ValueError, match="Invalid parameter name"):
        _build_select_statement("cat.schema.func", {"x; DROP TABLE": "val"})


def test_build_select_single_arg() -> None:
    """Single string arg produces one named parameter marker."""
    sql, params = _build_select_statement(
        "cat.schema.classify",
        {"text": "hello"},
    )
    assert sql == "SELECT `cat.schema.classify`(:text)"
    # String values are passed through directly (not JSON-encoded).
    assert len(params) == 1
    assert params[0] == {"name": "text", "value": "hello"}


def test_build_select_multiple_args() -> None:
    """Multiple args produce comma-separated parameter markers."""
    sql, params = _build_select_statement(
        "cat.schema.func",
        {"a": "x", "b": 42, "c": True},
    )
    assert ":a" in sql
    assert ":b" in sql
    assert ":c" in sql
    # Non-string values are JSON-encoded.
    b_param = next(p for p in params if p["name"] == "b")
    assert b_param["value"] == "42"
    c_param = next(p for p in params if p["name"] == "c")
    assert c_param["value"] == "true"


def test_build_select_non_string_value_json_encoded() -> None:
    """Non-string values (int, bool, list, dict) are JSON-encoded.

    The SQL Statement Execution API accepts all parameter values
    as strings; JSON encoding preserves type information for the
    server-side parser.
    """
    _, params = _build_select_statement(
        "cat.schema.func",
        {"items": [1, 2, 3]},
    )
    assert params[0]["value"] == json.dumps([1, 2, 3])


# ── execute_uc_function tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_execute_uc_function_missing_warehouse_id() -> None:
    """Calling without warehouse_id raises ValueError immediately.

    This prevents a confusing SDK error downstream — the user
    gets a clear message about the missing config.
    """
    from omnigent.runner.uc_function import execute_uc_function

    with pytest.raises(ValueError, match="requires a warehouse_id"):
        await execute_uc_function(
            catalog_path="cat.schema.func",
            args={"x": "1"},
            warehouse_id=None,
        )


@pytest.mark.asyncio
async def test_execute_uc_function_missing_warehouse_id_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ValueError when warehouse_id is None AND env var is unset.

    Ensures the env var fallback path is exercised: when neither
    the parameter nor ``DATABRICKS_WAREHOUSE_ID`` is available,
    the error message names both sources.
    """
    from omnigent.runner.uc_function import execute_uc_function

    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    with pytest.raises(ValueError, match="requires a warehouse_id"):
        await execute_uc_function(
            catalog_path="cat.schema.func",
            args={},
            warehouse_id=None,
        )


@pytest.mark.asyncio
async def test_execute_uc_function_warehouse_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """warehouse_id falls back to DATABRICKS_WAREHOUSE_ID env var.

    When the caller passes ``warehouse_id=None``, the env var
    is used instead. Verifies the env value reaches the SDK call.
    """
    fake_response = _FakeStatementResponse(
        status=_FakeStatementStatus(state=_FakeStatementState("SUCCEEDED")),
        result=_FakeResultData(data_array=[["ok"]]),
    )

    captured_calls: list[dict[str, object]] = []

    def _fake_execute_statement(**kwargs: object) -> _FakeStatementResponse:
        captured_calls.append(kwargs)
        return fake_response

    @dataclass
    class _FakeStatementExecution:
        execute_statement: object = None

    @dataclass
    class _FakeClient:
        statement_execution: _FakeStatementExecution = field(
            default_factory=lambda: _FakeStatementExecution(
                execute_statement=_fake_execute_statement,
            ),
        )

    monkeypatch.setattr(
        "omnigent.runner.uc_function._get_workspace_client",
        lambda profile: _FakeClient(),
    )

    @dataclass
    class _FakeParam:
        name: str
        value: str | None = None

    monkeypatch.setattr("databricks.sdk.service.sql.StatementParameterListItem", _FakeParam)
    monkeypatch.setattr("databricks.sdk.service.sql.StatementState", _FakeStatementState)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "env-wh-456")

    from omnigent.runner.uc_function import execute_uc_function

    result = await execute_uc_function(
        catalog_path="cat.schema.func",
        args={},
        warehouse_id=None,
    )

    # Env var value reached the SDK call.
    assert result == "ok"
    assert len(captured_calls) == 1
    assert captured_calls[0]["warehouse_id"] == "env-wh-456"


@pytest.mark.asyncio
async def test_execute_uc_function_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful UC function execution returns the scalar result.

    Verifies that:
    - The correct SQL and warehouse_id are forwarded to the SDK.
    - The scalar result from ``data_array[0][0]`` is returned.
    """
    # Build a fake response with a scalar result.
    fake_response = _FakeStatementResponse(
        status=_FakeStatementStatus(
            state=_FakeStatementState("SUCCEEDED"),
        ),
        result=_FakeResultData(data_array=[["positive"]]),
    )

    captured_calls: list[dict[str, Any]] = []

    def _fake_execute_statement(
        *,
        statement: str,
        warehouse_id: str,
        parameters: Any = None,
    ) -> _FakeStatementResponse:
        captured_calls.append(
            {
                "statement": statement,
                "warehouse_id": warehouse_id,
                "parameters": parameters,
            }
        )
        return fake_response

    # Stub the workspace client.
    @dataclass
    class _FakeStatementExecution:
        execute_statement: Any = None

    @dataclass
    class _FakeClient:
        statement_execution: _FakeStatementExecution = field(
            default_factory=lambda: _FakeStatementExecution(
                execute_statement=_fake_execute_statement,
            ),
        )

    fake_client = _FakeClient()

    monkeypatch.setattr(
        "omnigent.runner.uc_function._get_workspace_client",
        lambda profile: fake_client,
    )

    # Also patch the SDK imports used inside execute_uc_function.
    # StatementParameterListItem is used to wrap parameters.
    @dataclass
    class _FakeParam:
        name: str
        value: str | None = None

    monkeypatch.setattr(
        "databricks.sdk.service.sql.StatementParameterListItem",
        _FakeParam,
    )
    monkeypatch.setattr(
        "databricks.sdk.service.sql.StatementState",
        _FakeStatementState,
    )

    from omnigent.runner.uc_function import execute_uc_function

    result = await execute_uc_function(
        catalog_path="cat.schema.classify",
        args={"text": "hello"},
        profile="oss",
        warehouse_id="wh-123",
    )

    # Scalar result extracted from data_array[0][0].
    assert result == "positive"
    # Verify the SDK was called with correct warehouse.
    assert len(captured_calls) == 1
    assert captured_calls[0]["warehouse_id"] == "wh-123"
    assert "cat.schema.classify" in captured_calls[0]["statement"]


@pytest.mark.asyncio
async def test_execute_uc_function_failed_statement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failed statement execution raises RuntimeError with the error message."""
    fake_response = _FakeStatementResponse(
        status=_FakeStatementStatus(
            state=_FakeStatementState("FAILED"),
            error=_FakeStatementError(message="Function not found"),
        ),
    )

    @dataclass
    class _FakeStatementExecution:
        execute_statement: Any = None

    @dataclass
    class _FakeClient:
        statement_execution: _FakeStatementExecution = field(
            default_factory=lambda: _FakeStatementExecution(
                execute_statement=lambda **kw: fake_response,
            ),
        )

    monkeypatch.setattr(
        "omnigent.runner.uc_function._get_workspace_client",
        lambda profile: _FakeClient(),
    )

    @dataclass
    class _FakeParam:
        name: str
        value: str | None = None

    monkeypatch.setattr(
        "databricks.sdk.service.sql.StatementParameterListItem",
        _FakeParam,
    )
    monkeypatch.setattr(
        "databricks.sdk.service.sql.StatementState",
        _FakeStatementState,
    )

    from omnigent.runner.uc_function import execute_uc_function

    with pytest.raises(RuntimeError, match="Function not found"):
        await execute_uc_function(
            catalog_path="cat.schema.missing",
            args={},
            warehouse_id="wh-123",
        )


@pytest.mark.asyncio
async def test_execute_uc_function_null_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """NULL result from the function returns JSON null."""
    fake_response = _FakeStatementResponse(
        status=_FakeStatementStatus(
            state=_FakeStatementState("SUCCEEDED"),
        ),
        result=None,
    )

    @dataclass
    class _FakeStatementExecution:
        execute_statement: Any = None

    @dataclass
    class _FakeClient:
        statement_execution: _FakeStatementExecution = field(
            default_factory=lambda: _FakeStatementExecution(
                execute_statement=lambda **kw: fake_response,
            ),
        )

    monkeypatch.setattr(
        "omnigent.runner.uc_function._get_workspace_client",
        lambda profile: _FakeClient(),
    )

    @dataclass
    class _FakeParam:
        name: str
        value: str | None = None

    monkeypatch.setattr(
        "databricks.sdk.service.sql.StatementParameterListItem",
        _FakeParam,
    )
    monkeypatch.setattr(
        "databricks.sdk.service.sql.StatementState",
        _FakeStatementState,
    )

    from omnigent.runner.uc_function import execute_uc_function

    result = await execute_uc_function(
        catalog_path="cat.schema.func",
        args={},
        warehouse_id="wh-123",
    )

    assert result == json.dumps(None)


# ── Dispatch integration tests ──────────────────────────────────


def test_is_uc_function_tool_true() -> None:
    """``_is_uc_function_tool`` returns True for UC_FUNCTION tools."""
    from omnigent.runner.tool_dispatch import _is_uc_function_tool

    spec = _FakeAgentSpec(
        local_tools=[
            LocalToolInfo(
                name="classify",
                path=None,
                language="omnigent-python-callable",
                runtime=ToolRuntime.UC_FUNCTION,
                catalog_path="cat.schema.classify",
                warehouse_id="wh-123",
            ),
        ],
    )
    # UC function tool is detected.
    assert _is_uc_function_tool("classify", spec) is True
    # Non-existent tool returns False.
    assert _is_uc_function_tool("nonexistent", spec) is False


def test_is_uc_function_tool_false_for_server_tool() -> None:
    """``_is_uc_function_tool`` returns False for SERVER-runtime tools."""
    from omnigent.runner.tool_dispatch import _is_uc_function_tool

    spec = _FakeAgentSpec(
        local_tools=[
            LocalToolInfo(
                name="calc",
                path="tests.tool_functions.calculate",
                language="python",
                runtime=ToolRuntime.SERVER,
            ),
        ],
    )
    assert _is_uc_function_tool("calc", spec) is False


def test_is_uc_function_tool_no_spec() -> None:
    """``_is_uc_function_tool`` returns False when agent_spec is None."""
    from omnigent.runner.tool_dispatch import _is_uc_function_tool

    assert _is_uc_function_tool("classify", None) is False


def test_resolve_uc_profile_from_auth() -> None:
    """Profile is extracted from ``executor.auth.profile``."""
    from omnigent.runner.tool_dispatch import _resolve_uc_profile

    spec = _FakeAgentSpec(
        executor=_FakeExecutorSpec(
            auth=_FakeDatabricksAuth(profile="oss"),
        ),
    )
    assert _resolve_uc_profile(spec) == "oss"


def test_resolve_uc_profile_from_deprecated_field() -> None:
    """Profile falls back to ``executor.profile`` when auth is absent."""
    from omnigent.runner.tool_dispatch import _resolve_uc_profile

    spec = _FakeAgentSpec(
        executor=_FakeExecutorSpec(profile="legacy-profile"),
    )
    assert _resolve_uc_profile(spec) == "legacy-profile"


def test_resolve_uc_profile_from_config() -> None:
    """Profile falls back to ``executor.config["profile"]``."""
    from omnigent.runner.tool_dispatch import _resolve_uc_profile

    spec = _FakeAgentSpec(
        executor=_FakeExecutorSpec(config={"profile": "compat-profile"}),
    )
    assert _resolve_uc_profile(spec) == "compat-profile"


def test_resolve_uc_profile_none() -> None:
    """Returns None when no profile is configured anywhere."""
    from omnigent.runner.tool_dispatch import _resolve_uc_profile

    spec = _FakeAgentSpec(executor=_FakeExecutorSpec())
    assert _resolve_uc_profile(spec) is None
