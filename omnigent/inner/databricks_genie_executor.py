"""
``harness: databricks-genie`` executor.

Drives a remote Databricks **AI/BI Genie space** over the Genie Conversation
API exposed by the ``databricks-sdk`` (``WorkspaceClient.genie``). Each Omnigent
turn maps to one Genie message: the first turn starts a conversation, later
turns continue it (Genie keeps the conversation state server-side, keyed by a
conversation id this executor remembers between turns).

Genie answers natural-language questions over the space's curated data. A turn's
response carries Genie's text summary plus — when Genie runs SQL — the generated
query and a rendering of its result rows.

Auth reuses the Databricks CLI: ``databricks auth login`` writes OAuth/PAT
credentials into ``~/.databrickscfg``; ``WorkspaceClient(profile=...)`` consumes
them and refreshes OAuth tokens transparently. The Genie space id is carried in
``executor.model`` (a Genie space is the conversational unit, so it maps onto
"model") and the profile in ``executor.auth`` / ``executor.profile``; the
:mod:`omnigent.inner.databricks_genie_harness` wrap passes both to this executor.

The SDK's ``*_and_wait`` helpers are blocking, so each call runs in a worker
thread via :func:`asyncio.to_thread` to keep the event loop responsive.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnComplete,
)

_logger = logging.getLogger(__name__)

# Display guard: cap the number of result rows rendered inline so a large query
# result doesn't blow up the turn's response payload. Genie truncates upstream
# too; this is a belt-and-braces bound on what we echo back.
_MAX_RESULT_ROWS = 50

# Default deadline handed to Genie's blocking ``*_and_wait`` helpers. Comfortably
# under Omnigent's per-turn ceiling while leaving room for a real warehouse query.
_DEFAULT_TIMEOUT_SECONDS = 300.0


class DatabricksGenieError(Exception):
    """Raised for databricks-genie setup / response failures.

    Carries an actionable, human-readable message (missing SDK + install hint,
    or a failed Genie message). :meth:`DatabricksGenieExecutor.run_turn`
    converts it — and any other exception raised while talking to Genie — into
    an :class:`~omnigent.inner.executor.ExecutorError` turn event.
    """


def _extract_text(msg: Message) -> str:
    """Return the plain text of a single message dict.

    Handles both string ``content`` and a list of multimodal parts (joining the
    ``text`` of each ``{"type": "text", "text": ...}`` block), mirroring the
    peer executors' message handling.

    :param msg: An executor-facing message dict, e.g.
        ``{"role": "user", "content": "hi"}``.
    :returns: The message's text, or ``""`` when it carries none.
    """
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return str(content)


def _latest_user_text(messages: list[Message]) -> str:
    """Return the text of the most recent ``user`` message.

    Genie maintains its own conversation history server-side, so only the latest
    user turn is forwarded; prior turns are already known to the Genie space.

    :param messages: The turn's message history (oldest first).
    :returns: The latest user message's text, or ``""`` when there is none.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _extract_text(msg)
    return ""


def _render_statement(statement: object) -> str:
    """Render a SQL ``StatementResponse`` as a compact text table.

    Reads column names from ``statement.manifest.schema.columns[*].name`` and
    rows from ``statement.result.data_array`` (Genie returns cell values as
    strings). Rows beyond :data:`_MAX_RESULT_ROWS` are summarized rather than
    printed.

    :param statement: A ``databricks.sdk.service.sql.StatementResponse`` (or any
        object exposing the same ``manifest`` / ``result`` shape).
    :returns: A ``"Result (N rows):\\n<header>\\n<rows>"`` block, or ``""`` when
        the statement carries no rows.
    """
    data = getattr(statement, "result", None)
    rows = getattr(data, "data_array", None) if data is not None else None
    if not rows:
        return ""

    manifest = getattr(statement, "manifest", None)
    schema = getattr(manifest, "schema", None) if manifest is not None else None
    columns = [str(getattr(col, "name", "")) for col in (getattr(schema, "columns", None) or [])]

    lines: list[str] = []
    if columns:
        lines.append(" | ".join(columns))
    shown = rows[:_MAX_RESULT_ROWS]
    for row in shown:
        lines.append(" | ".join("" if cell is None else str(cell) for cell in row))
    omitted = len(rows) - len(shown)
    if omitted > 0:
        lines.append(f"… ({omitted} more row{'s' if omitted != 1 else ''})")

    total = len(rows)
    header = f"Result ({total} row{'s' if total != 1 else ''}):"
    return header + "\n" + "\n".join(lines)


class DatabricksGenieExecutor(Executor):
    """Executor that drives a Databricks Genie space over the Genie API.

    :param space_id: The Genie space id to converse with (from
        ``executor.model``). When unset, :meth:`run_turn` yields an
        :class:`ExecutorError` instructing the user to set it.
    :param profile: The Databricks profile from ``~/.databrickscfg`` used to
        build the workspace client. ``None`` lets the SDK use its own resolution
        order (``DATABRICKS_CONFIG_PROFILE`` env / ``DEFAULT`` section).
    :param workspace_client: An injected ``WorkspaceClient`` (tests pass a fake).
        When ``None``, one is built lazily on the first turn so a missing
        ``databricks-sdk`` install surfaces as a turn error, not a boot crash.
    :param timeout_seconds: Deadline handed to Genie's blocking
        ``*_and_wait`` helpers.
    """

    def __init__(
        self,
        *,
        space_id: str | None,
        profile: str | None = None,
        workspace_client: object | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._space_id = space_id
        self._profile = profile
        self._client = workspace_client
        self._timeout_seconds = timeout_seconds
        # Set on the first turn that reaches Genie; subsequent turns continue
        # this conversation instead of starting a new one.
        self._conversation_id: str | None = None

    def supports_streaming(self) -> bool:
        """Genie returns a complete answer per message — there is no token stream."""
        return False

    def supports_tool_calling(self) -> bool:
        """Genie has no Omnigent-dispatched tools; it queries the space directly."""
        return False

    def _ensure_client(self) -> object:
        """Return the workspace client, building one lazily on first use.

        :returns: The (possibly newly constructed) ``WorkspaceClient``.
        :raises DatabricksGenieError: When ``databricks-sdk`` is not installed.
        """
        if self._client is not None:
            return self._client
        try:
            from databricks.sdk import WorkspaceClient
        except ImportError as exc:
            from omnigent.onboarding.databricks_config import DATABRICKS_EXTRA_INSTALL_HINT

            raise DatabricksGenieError(
                "the databricks-sdk package is required for the databricks-genie "
                f"harness but is not installed. {DATABRICKS_EXTRA_INSTALL_HINT}"
            ) from exc
        self._client = WorkspaceClient(profile=self._profile)
        return self._client

    def _send(self, client: object, space_id: str, content: str) -> object:
        """Send *content* to Genie, starting or continuing the conversation.

        :param client: The workspace client whose ``.genie`` API is called.
        :param space_id: The Genie space id.
        :param content: The user's natural-language message.
        :returns: The resolved ``GenieMessage``.
        """
        genie = client.genie  # type: ignore[attr-defined]  # untyped databricks-sdk
        timeout = timedelta(seconds=self._timeout_seconds)
        if self._conversation_id is None:
            return genie.start_conversation_and_wait(space_id, content, timeout=timeout)
        return genie.create_message_and_wait(
            space_id, self._conversation_id, content, timeout=timeout
        )

    def _fetch_statement_table(self, client: object, statement_id: object) -> str:
        """Fetch and render the executed query's result rows, best-effort.

        A result-fetch failure (e.g. the result expired) must not sink the turn —
        Genie's text answer is still useful — so this swallows errors and returns
        ``""`` after logging.

        :param client: The workspace client.
        :param statement_id: The executed query's statement id (from the query
            attachment). ``None`` / empty → no table.
        :returns: A rendered result table, or ``""`` when unavailable.
        """
        if not statement_id:
            return ""
        try:
            statement = client.statement_execution.get_statement(  # type: ignore[attr-defined]
                statement_id
            )
        except Exception as exc:  # noqa: BLE001 — result is optional; never fail the turn
            _logger.debug("databricks-genie: could not fetch query result: %s", exc)
            return ""
        return _render_statement(statement)

    def _format_query(self, client: object, query: object) -> str:
        """Render a Genie query attachment: title/description + SQL + result rows.

        The executed query's rows are fetched via the SQL Statement Execution
        API keyed by the attachment's ``statement_id`` — Genie's
        ``get_message_query_result`` returns the row metadata but not the inline
        rows, whereas ``statement_execution.get_statement`` returns them.

        :param client: The workspace client (for fetching the result).
        :param query: The ``GenieQueryAttachment`` to render.
        :returns: A multi-line block describing the generated query and its data.
        """
        title = getattr(query, "title", None)
        description = getattr(query, "description", None)
        sql = getattr(query, "query", None)

        lines: list[str] = [f'Generated SQL ("{title}"):' if title else "Generated SQL:"]
        if description:
            lines.append(str(description))
        if sql:
            lines.append(str(sql))
        table = self._fetch_statement_table(client, getattr(query, "statement_id", None))
        if table:
            lines.append(table)
        return "\n".join(lines)

    def _format_message(self, client: object, message: object) -> str:
        """Assemble the turn's response text from a ``GenieMessage``.

        Concatenates each attachment's text summary and rendered query (with
        result rows). Falls back to the message's own ``content`` when Genie
        returns no attachments.

        :param client: The workspace client.
        :param message: The resolved ``GenieMessage``.
        :returns: The combined response text (possibly ``""``).
        """
        parts: list[str] = []
        for att in getattr(message, "attachments", None) or []:
            text = getattr(att, "text", None)
            content = getattr(text, "content", None) if text is not None else None
            if content:
                parts.append(str(content))
            query = getattr(att, "query", None)
            if query is not None:
                parts.append(self._format_query(client, query))

        if not parts:
            content = getattr(message, "content", None)
            if content:
                parts.append(str(content))
        return "\n\n".join(part for part in parts if part)

    def _run_genie_turn(self, client: object, space_id: str, prompt: str) -> str:
        """Blocking: send the prompt and format the response.

        Runs inside :func:`asyncio.to_thread`. Genie's ``*_and_wait`` helpers
        raise (``OperationFailed`` / ``TimeoutError``) when a message does not
        reach ``COMPLETED``, so failures arrive as the exception
        :meth:`run_turn` turns into an :class:`ExecutorError` — this method only
        handles the success path. The conversation id is recorded so the next
        turn continues the same conversation.

        :param client: The workspace client.
        :param space_id: The Genie space id.
        :param prompt: The user's natural-language message.
        :returns: The formatted response text.
        """
        message = self._send(client, space_id, prompt)
        conversation_id = getattr(message, "conversation_id", None)
        if conversation_id:
            self._conversation_id = conversation_id
        return self._format_message(client, message)

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 — Genie has no Omnigent-dispatched tools
        system_prompt: str,  # noqa: ARG002 — the Genie space carries its own instructions
        config: ExecutorConfig | None = None,  # noqa: ARG002 — space id comes from __init__
    ) -> AsyncIterator[ExecutorEvent]:
        """Run one Genie turn and yield its events.

        Yields an :class:`ExecutorError` (and stops) when no space id is
        configured, there is no user message, or talking to Genie fails;
        otherwise yields a :class:`TextChunk` with the response followed by a
        :class:`TurnComplete`.
        """
        space_id = self._space_id
        if not space_id:
            yield ExecutorError(
                message=(
                    "no Genie space id configured for the databricks-genie harness; "
                    "set executor.model to the Genie space id in the agent spec."
                )
            )
            return

        prompt = _latest_user_text(messages)
        if not prompt.strip():
            yield ExecutorError(message="databricks-genie: no user message to send to Genie.")
            return

        try:
            client = self._ensure_client()
            response = await asyncio.to_thread(self._run_genie_turn, client, space_id, prompt)
        except Exception as exc:  # noqa: BLE001 — any SDK/auth/Genie error becomes a turn error
            yield ExecutorError(message=f"databricks-genie request failed: {exc}")
            return

        if response:
            yield TextChunk(text=response)
        yield TurnComplete(response=response)
