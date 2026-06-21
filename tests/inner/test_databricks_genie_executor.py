"""Tests for :class:`DatabricksGenieExecutor` with a fake Genie client.

The executor talks to a Databricks Genie space via ``WorkspaceClient.genie``. A
small set of fakes mirror the ``databricks-sdk`` shapes the executor reads
(``GenieMessage`` / ``GenieAttachment`` / ``StatementResponse``), so every branch
is exercised without the SDK or a live workspace.
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.inner.databricks_genie_executor import (
    DatabricksGenieError,
    DatabricksGenieExecutor,
    _extract_text,
    _latest_user_text,
    _render_statement,
)
from omnigent.inner.executor import (
    ExecutorError,
    TextChunk,
    TurnComplete,
)


def _run(coro: Any) -> Any:
    """Drive a coroutine to completion on a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


# ---------------------------------------------------------------------------
# Fakes mirroring the databricks-sdk Genie shapes the executor reads
# ---------------------------------------------------------------------------


@dataclass
class FakeTextAttachment:
    content: str | None = None


@dataclass
class FakeQueryAttachment:
    title: str | None = None
    description: str | None = None
    query: str | None = None
    statement_id: str | None = "stmt-1"


@dataclass
class FakeAttachment:
    text: FakeTextAttachment | None = None
    query: FakeQueryAttachment | None = None


@dataclass
class FakeMessage:
    """Stand-in for ``GenieMessage``."""

    conversation_id: str | None = "conv-1"
    message_id: str | None = "msg-1"
    content: str | None = None
    attachments: list[FakeAttachment] | None = None


@dataclass
class FakeColumn:
    name: str = ""


@dataclass
class FakeSchema:
    columns: list[FakeColumn] = field(default_factory=list)


@dataclass
class FakeManifest:
    schema: FakeSchema | None = None


@dataclass
class FakeResultData:
    data_array: list[list[Any]] | None = None


@dataclass
class FakeStatementResponse:
    manifest: FakeManifest | None = None
    result: FakeResultData | None = None


class FakeGenie:
    """Mimics ``WorkspaceClient.genie``, scripting one message per call."""

    def __init__(
        self,
        message: FakeMessage,
        *,
        raise_on_send: Exception | None = None,
    ) -> None:
        self._message = message
        self._raise_on_send = raise_on_send
        self.start_calls: list[tuple[str, str]] = []
        self.create_calls: list[tuple[str, str, str]] = []

    def start_conversation_and_wait(
        self, space_id: str, content: str, timeout: Any = None
    ) -> FakeMessage:
        self.start_calls.append((space_id, content))
        if self._raise_on_send is not None:
            raise self._raise_on_send
        return self._message

    def create_message_and_wait(
        self, space_id: str, conversation_id: str, content: str, timeout: Any = None
    ) -> FakeMessage:
        self.create_calls.append((space_id, conversation_id, content))
        if self._raise_on_send is not None:
            raise self._raise_on_send
        return self._message


class FakeStatementExecution:
    """Mimics ``WorkspaceClient.statement_execution`` for result-row fetches."""

    def __init__(
        self,
        statement: FakeStatementResponse | None = None,
        *,
        raise_on_get: Exception | None = None,
    ) -> None:
        self._statement = statement
        self._raise_on_get = raise_on_get
        self.get_calls: list[object] = []

    def get_statement(self, statement_id: object) -> FakeStatementResponse | None:
        self.get_calls.append(statement_id)
        if self._raise_on_get is not None:
            raise self._raise_on_get
        return self._statement


class FakeWorkspaceClient:
    def __init__(
        self, genie: FakeGenie, statement_execution: FakeStatementExecution | None = None
    ) -> None:
        self.genie = genie
        self.statement_execution = statement_execution or FakeStatementExecution()


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def _events(executor: DatabricksGenieExecutor, messages: list[dict[str, Any]]) -> list[Any]:
    async def _collect() -> list[Any]:
        return [e async for e in executor.run_turn(messages, [], "SYS")]

    return _run(_collect())


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_text_only_answer_starts_conversation() -> None:
    """A text-only Genie answer streams one TextChunk + TurnComplete."""
    genie = FakeGenie(
        FakeMessage(attachments=[FakeAttachment(text=FakeTextAttachment(content="42 sales"))])
    )
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    events = _events(executor, [_user("how many sales?")])

    texts = [e.text for e in events if isinstance(e, TextChunk)]
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert texts == ["42 sales"]
    assert len(completes) == 1 and completes[0].response == "42 sales"
    # First turn → start_conversation, not create_message.
    assert genie.start_calls == [("sp", "how many sales?")]
    assert genie.create_calls == []


def test_query_answer_includes_sql_and_result_rows() -> None:
    """A query attachment renders title + SQL + the fetched result table."""
    statement = FakeStatementResponse(
        manifest=FakeManifest(schema=FakeSchema(columns=[FakeColumn("region"), FakeColumn("n")])),
        result=FakeResultData(data_array=[["EMEA", "1200"], ["APAC", None]]),
    )
    genie = FakeGenie(
        FakeMessage(
            attachments=[
                FakeAttachment(text=FakeTextAttachment(content="Here is the breakdown.")),
                FakeAttachment(
                    query=FakeQueryAttachment(
                        title="By region",
                        description="totals per region",
                        query="SELECT region, count(*) n FROM s GROUP BY region",
                        statement_id="stmt-region",
                    )
                ),
            ]
        ),
    )
    stmt_exec = FakeStatementExecution(statement)
    executor = DatabricksGenieExecutor(
        space_id="sp", workspace_client=FakeWorkspaceClient(genie, stmt_exec)
    )

    events = _events(executor, [_user("breakdown by region")])
    response = next(e.response for e in events if isinstance(e, TurnComplete))

    assert "Here is the breakdown." in response
    assert 'Generated SQL ("By region"):' in response
    assert "totals per region" in response
    assert "SELECT region, count(*) n FROM s GROUP BY region" in response
    assert "Result (2 rows):" in response
    assert "region | n" in response
    assert "EMEA | 1200" in response
    # None cell rendered as empty string.
    assert "APAC | " in response
    # The result is fetched via the Statement Execution API by statement id.
    assert stmt_exec.get_calls == ["stmt-region"]


def test_follow_up_turn_continues_conversation() -> None:
    """The second turn reuses the stored conversation id via create_message."""
    genie = FakeGenie(
        FakeMessage(attachments=[FakeAttachment(text=FakeTextAttachment(content="ok"))])
    )
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    _events(executor, [_user("first")])
    _events(executor, [_user("second")])

    assert genie.start_calls == [("sp", "first")]
    assert genie.create_calls == [("sp", "conv-1", "second")]


def test_message_content_fallback_when_no_attachments() -> None:
    """With no attachments, the message's own content is used as the answer."""
    genie = FakeGenie(FakeMessage(attachments=[], content="bare answer"))
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    events = _events(executor, [_user("q")])
    assert next(e.response for e in events if isinstance(e, TurnComplete)) == "bare answer"


def test_empty_answer_emits_turn_complete_without_text_chunk() -> None:
    """An empty response yields only TurnComplete (no empty TextChunk)."""
    genie = FakeGenie(FakeMessage(attachments=[], content=None))
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    events = _events(executor, [_user("q")])
    assert [type(e).__name__ for e in events] == ["TurnComplete"]
    assert events[0].response == ""


# ---------------------------------------------------------------------------
# Error / guard paths
# ---------------------------------------------------------------------------


def test_missing_space_id_errors() -> None:
    """No space id → an actionable ExecutorError, no SDK call."""
    genie = FakeGenie(FakeMessage())
    executor = DatabricksGenieExecutor(space_id=None, workspace_client=FakeWorkspaceClient(genie))

    events = _events(executor, [_user("q")])
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "executor.model" in events[0].message
    assert genie.start_calls == []


def test_empty_user_message_errors() -> None:
    """A blank/absent user message → ExecutorError before any SDK call."""
    genie = FakeGenie(FakeMessage())
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    events = _events(executor, [_user("   ")])
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no user message" in events[0].message
    assert genie.start_calls == []


def test_send_exception_becomes_executor_error() -> None:
    """An SDK error during send (e.g. Genie message FAILED / timed out — the SDK's
    ``*_and_wait`` raises ``OperationFailed`` — or auth failure) becomes an ExecutorError."""
    genie = FakeGenie(FakeMessage(), raise_on_send=RuntimeError("workspace down"))
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    events = _events(executor, [_user("q")])
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "workspace down" in events[0].message


def test_query_result_fetch_failure_is_best_effort() -> None:
    """A result-fetch failure drops the table but keeps the SQL/text answer."""
    genie = FakeGenie(
        FakeMessage(
            attachments=[
                FakeAttachment(query=FakeQueryAttachment(query="SELECT 1")),
            ]
        ),
    )
    stmt_exec = FakeStatementExecution(raise_on_get=RuntimeError("result expired"))
    executor = DatabricksGenieExecutor(
        space_id="sp", workspace_client=FakeWorkspaceClient(genie, stmt_exec)
    )

    events = _events(executor, [_user("q")])
    response = next(e.response for e in events if isinstance(e, TurnComplete))
    assert "Generated SQL:" in response
    assert "SELECT 1" in response
    assert "Result (" not in response


def test_query_attachment_without_sql_renders_description_only() -> None:
    """A query attachment with a description but no SQL omits the SQL line."""
    genie = FakeGenie(
        FakeMessage(
            attachments=[
                FakeAttachment(query=FakeQueryAttachment(description="just a note", query=None))
            ]
        )
    )
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))
    response = next(
        e.response for e in _events(executor, [_user("q")]) if isinstance(e, TurnComplete)
    )
    assert "Generated SQL:" in response
    assert "just a note" in response


def test_message_without_conversation_id_does_not_thread() -> None:
    """A message lacking a conversation id leaves the next turn starting fresh."""
    genie = FakeGenie(
        FakeMessage(
            conversation_id=None,
            attachments=[FakeAttachment(text=FakeTextAttachment(content="ok"))],
        )
    )
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    _events(executor, [_user("first")])
    _events(executor, [_user("second")])

    # No conversation id was recorded, so both turns start a new conversation.
    assert genie.start_calls == [("sp", "first"), ("sp", "second")]
    assert genie.create_calls == []


# ---------------------------------------------------------------------------
# Lazy client construction (databricks-sdk import)
# ---------------------------------------------------------------------------


def test_lazy_client_built_from_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no injected client, a WorkspaceClient is built from the SDK once."""
    genie = FakeGenie(
        FakeMessage(attachments=[FakeAttachment(text=FakeTextAttachment(content="hi"))])
    )
    constructed: list[dict[str, Any]] = []

    def _client_factory(**kwargs: Any) -> FakeWorkspaceClient:
        constructed.append(kwargs)
        return FakeWorkspaceClient(genie)

    fake_sdk = types.ModuleType("databricks.sdk")
    fake_sdk.WorkspaceClient = _client_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks.sdk", fake_sdk)

    executor = DatabricksGenieExecutor(space_id="sp", profile="dev")
    events = _events(executor, [_user("q")])

    assert next(e.response for e in events if isinstance(e, TurnComplete)) == "hi"
    # Built exactly once, with the configured profile, then reused.
    assert constructed == [{"profile": "dev"}]
    _events(executor, [_user("again")])
    assert len(constructed) == 1


def test_missing_sdk_errors_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing databricks-sdk surfaces an ExecutorError with an install hint."""

    real_import = __import__

    def _no_databricks_sdk(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "databricks.sdk":
            raise ImportError("No module named 'databricks.sdk'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "databricks.sdk", raising=False)
    monkeypatch.setattr("builtins.__import__", _no_databricks_sdk)

    executor = DatabricksGenieExecutor(space_id="sp")
    events = _events(executor, [_user("q")])

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "databricks-sdk" in events[0].message
    assert "omnigent[databricks]" in events[0].message


# ---------------------------------------------------------------------------
# Capability flags & message extraction
# ---------------------------------------------------------------------------


def test_capability_flags() -> None:
    executor = DatabricksGenieExecutor(space_id="sp")
    assert executor.supports_streaming() is False
    assert executor.supports_tool_calling() is False


def test_extract_text_edge_cases() -> None:
    """``_extract_text`` covers absent, string, multimodal, and non-text content."""
    assert _extract_text({"role": "user"}) == ""  # no content key → None
    assert _extract_text({"content": None}) == ""
    assert _extract_text({"content": "plain"}) == "plain"
    assert _extract_text({"content": [{"type": "text", "text": "a"}, {"no": "text"}]}) == "a"
    # A non-dict element in the parts list is skipped, not coerced.
    assert _extract_text({"content": ["skip-me", {"type": "text", "text": "a"}]}) == "a"
    # Non-str / non-list content falls back to str().
    assert _extract_text({"content": 123}) == "123"


def test_latest_user_text_no_user_message_returns_empty() -> None:
    """With no user-role message, the latest-user lookup yields ``""``."""
    assert _latest_user_text([{"role": "assistant", "content": "hi"}]) == ""
    assert _latest_user_text([]) == ""


def test_run_turn_with_no_user_role_errors() -> None:
    """A history with no user message → the empty-prompt guard fires."""
    genie = FakeGenie(FakeMessage())
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))
    events = _events(executor, [{"role": "assistant", "content": "hello"}])
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no user message" in events[0].message


def test_latest_user_text_handles_multimodal_parts() -> None:
    """The latest user message wins, and list-of-parts content is joined."""
    genie = FakeGenie(
        FakeMessage(attachments=[FakeAttachment(text=FakeTextAttachment(content="ok"))])
    )
    executor = DatabricksGenieExecutor(space_id="sp", workspace_client=FakeWorkspaceClient(genie))

    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}, {"type": "image"}]},
    ]
    _events(executor, messages)
    assert genie.start_calls == [("sp", "hello")]


# ---------------------------------------------------------------------------
# _render_statement (pure helper) — table edge cases
# ---------------------------------------------------------------------------


def test_render_statement_no_rows_returns_empty() -> None:
    assert _render_statement(FakeStatementResponse(result=FakeResultData(data_array=[]))) == ""
    assert _render_statement(FakeStatementResponse(result=None)) == ""


def test_render_statement_without_columns_renders_rows_only() -> None:
    stmt = FakeStatementResponse(
        manifest=None,
        result=FakeResultData(data_array=[["a", "b"]]),
    )
    rendered = _render_statement(stmt)
    assert rendered == "Result (1 row):\na | b"


def test_render_statement_truncates_beyond_cap() -> None:
    rows = [[str(i)] for i in range(55)]
    stmt = FakeStatementResponse(
        manifest=FakeManifest(schema=FakeSchema(columns=[FakeColumn("i")])),
        result=FakeResultData(data_array=rows),
    )
    rendered = _render_statement(stmt)
    assert "Result (55 rows):" in rendered
    assert "… (5 more rows)" in rendered
    # 1 header line + 1 column line + 50 rows + 1 omitted line.
    assert rendered.count("\n") == 1 + 1 + 50


def test_render_statement_single_omitted_row_is_singular() -> None:
    rows = [[str(i)] for i in range(51)]
    stmt = FakeStatementResponse(
        manifest=FakeManifest(schema=FakeSchema(columns=[FakeColumn("i")])),
        result=FakeResultData(data_array=rows),
    )
    assert "… (1 more row)" in _render_statement(stmt)


def test_statement_with_no_rows_renders_no_table() -> None:
    """A statement that resolves with no rows renders the SQL but no table."""
    genie = FakeGenie(
        FakeMessage(attachments=[FakeAttachment(query=FakeQueryAttachment(query="SELECT 1"))]),
    )
    stmt_exec = FakeStatementExecution(
        FakeStatementResponse(result=FakeResultData(data_array=None))
    )
    executor = DatabricksGenieExecutor(
        space_id="sp", workspace_client=FakeWorkspaceClient(genie, stmt_exec)
    )
    events = _events(executor, [_user("q")])
    response = next(e.response for e in events if isinstance(e, TurnComplete))
    assert "SELECT 1" in response
    assert "Result (" not in response


def test_query_with_missing_statement_id_skips_result_fetch() -> None:
    """No statement id → the result table is skipped without an SDK call."""
    genie = FakeGenie(
        FakeMessage(
            attachments=[
                FakeAttachment(query=FakeQueryAttachment(query="SELECT 1", statement_id=None))
            ],
        )
    )
    stmt_exec = FakeStatementExecution()
    executor = DatabricksGenieExecutor(
        space_id="sp", workspace_client=FakeWorkspaceClient(genie, stmt_exec)
    )
    events = _events(executor, [_user("q")])
    response = next(e.response for e in events if isinstance(e, TurnComplete))
    assert "SELECT 1" in response
    assert stmt_exec.get_calls == []


def test_genie_error_str() -> None:
    """The custom error type carries its message (used in ExecutorError text)."""
    assert "boom" in str(DatabricksGenieError("boom"))
