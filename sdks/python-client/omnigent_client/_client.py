"""OmnigentClient — the top-level client tying all namespaces together."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, overload

import httpx

from ._files import FilesNamespace
from ._query import QueryResult, QueryStream
from ._responses import ResponsesNamespace
from ._session import Session
from ._sessions import SessionsNamespace
from ._sessions_chat import SessionsChat, ToolCallable
from ._tool_handler import StreamHooks, ToolHandler


class OmnigentClient:
    """Typed Python client for the omnigent server API.

    One-shot::

        async with OmnigentClient(base_url="http://localhost:8080") as client:
            result = await client.query(model="archer", input="hello")
            print(result.text)        # the assistant's reply
            print(result.files)       # any files the agent produced

    Streaming::

        stream = await client.query(model="archer", input="hi", stream=True)
        async for chunk in stream:
            print(chunk, end="", flush=True)
        print(stream.files)            # populated after the stream ends

    Multi-turn conversation::

        session = client.session(model="archer")
        await session.query("hello")
        await session.query("what did I just say?")

    For access to raw events or semantic blocks (tool-call display,
    reasoning, lifecycle), drop to :attr:`responses` or
    :class:`BlockStream`.

    :param base_url: Server base URL, e.g. ``"http://localhost:8080"``.
    :param headers: Extra headers sent on every request (e.g. auth).
    :param auth: Optional ``httpx.Auth`` for per-request
        authentication. When set, the auth handler runs on every
        request, allowing transparent token refresh for OAuth
        flows. ``None`` (default) relies on static ``headers``.
    :param timeout: Default timeout for HTTP requests in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        auth: httpx.Auth | None = None,
        # Public SDK API surface. Removing would be a breaking
        # change for downstream consumers; leaving it
        # accepted-but-ignored preserves compatibility while the
        # SSE client internally uses a fixed 600s read timeout
        # (tool calls can legitimately hold the stream open for
        # minutes).
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Long read timeout for SSE streams (tool execution can
        # pause the stream for minutes).
        sse_timeout = httpx.Timeout(
            connect=30.0,
            read=600.0,
            write=30.0,
            pool=30.0,
        )
        self._http = httpx.AsyncClient(
            headers=headers or {},
            auth=auth,
            timeout=sse_timeout,
        )

        self.sessions = SessionsNamespace(self._http, self._base_url)
        self.files = FilesNamespace(self._http, self._base_url)
        self.responses = ResponsesNamespace(self._http, self._base_url)

    def session(
        self,
        model: str,
        *,
        tool_handler: ToolHandler | None = None,
        hooks: StreamHooks | None = None,
    ) -> Session:
        """Create a conversation session.

        A session tracks ``previous_response_id`` automatically.
        ``send()`` auto-steers if a response is in progress, or
        starts a new turn if the response is terminal.

        :param model: Agent name.
        :param tool_handler: Optional client-side tool execution config.
        :param hooks: Optional lifecycle hooks.
        :returns: A new :class:`Session`.
        """
        return Session(
            client=self,
            model=model,
            tool_handler=tool_handler,
            hooks=hooks,
        )

    @overload
    async def query(
        self,
        *,
        model: str,
        input: str | list[dict[str, object]],
        tools: list[Callable[..., Any]] | None = ...,
        tool_handler: ToolHandler | None = ...,
        files: list[str] | None = ...,
        reasoning: dict[str, str] | None = ...,
        model_override: str | None = ...,
        stream: Literal[False] = ...,
    ) -> QueryResult: ...

    @overload
    async def query(
        self,
        *,
        model: str,
        input: str | list[dict[str, object]],
        tools: list[Callable[..., Any]] | None = ...,
        tool_handler: ToolHandler | None = ...,
        files: list[str] | None = ...,
        reasoning: dict[str, str] | None = ...,
        model_override: str | None = ...,
        stream: Literal[True],
    ) -> QueryStream: ...

    async def query(
        self,
        *,
        model: str,
        input: str | list[dict[str, object]],
        tools: list[Callable[..., Any]] | None = None,
        tool_handler: ToolHandler | None = None,
        files: list[str] | None = None,
        reasoning: dict[str, str] | None = None,
        model_override: str | None = None,
        stream: bool = False,
    ) -> QueryResult | QueryStream:
        """One-shot invocation: send a prompt, get text (plus any files) back.

        Non-streaming (default) returns a :class:`QueryResult`::

            result = await client.query(model="archer", input="hi")
            print(result.text)
            for f in result.files:
                await client.files.for_session("<session-id>").download(
                    f.id, f"./out/{f.filename}"
                )

        Streaming returns a :class:`QueryStream`::

            stream = await client.query(model="archer", input="hi", stream=True)
            async for chunk in stream:
                print(chunk, end="", flush=True)
            # After iteration, stream.files holds the produced files.

        With client-side tools, pass ``@tool``-decorated functions::

            from omnigent_client import tool

            @tool
            def get_time() -> str:
                '''Return the current time.'''
                return datetime.now().isoformat()

            result = await client.query(
                model="archer", input="what time?", tools=[get_time],
            )

        Creates a single-turn session internally. For multi-turn
        conversations, call :meth:`session` and use its ``query()``.

        :param model: Agent name, e.g. ``"archer"``.
        :param input: User text or a list of content-block dicts.
        :param tools: List of ``@tool``-decorated Python functions
            the agent may call. Mutually exclusive with ``tool_handler``.
        :param tool_handler: Low-level escape hatch — a pre-built
            :class:`ToolHandler` with custom schemas/dispatch. Most
            callers should use ``tools=`` instead.
        :param files: Optional list of local file paths to attach.
        :param reasoning: Optional Responses API reasoning config, e.g. {"effort": "high"}.
        :param model_override: Optional per-request LLM model override
            (e.g. ``"openai/gpt-5.4-mini"``). Shadows the spec model
            for this one-shot call; mirrors
            :meth:`Session.set_model_override`.
        :param stream: If True, return a :class:`QueryStream`. If
            False (default), return a :class:`QueryResult`.
        :returns: :class:`QueryResult` (``stream=False``) or
            :class:`QueryStream` (``stream=True``).
        :raises ValueError: If both ``tools`` and ``tool_handler``
            are provided.
        """
        handler = _resolve_tool_handler(tools=tools, tool_handler=tool_handler)
        session = self.session(model=model, tool_handler=handler)
        effort = reasoning.get("effort") if reasoning is not None else None
        if effort is not None:
            session.set_reasoning_effort(effort)
        if model_override is not None:
            session.set_model_override(model_override)
        if stream:
            return await session.query(input, files=files, stream=True)
        return await session.query(input, files=files)

    async def sessions_chat(
        self,
        bundle: bytes,
        *,
        filename: str = "agent.tar.gz",
        tool_callables: dict[str, ToolCallable] | None = None,
        hooks: StreamHooks | None = None,
    ) -> SessionsChat:
        """Create a sessions-API-native chat helper bound to a new session.

        Counterpart to :meth:`session` but built on ``/v1/sessions``
        rather than ``/v1/responses``. Use this for new code; the
        legacy :meth:`session` is preserved for in-flight migrations.

        :param bundle: Gzipped agent tarball bytes uploaded through
            multipart ``POST /v1/sessions``.
        :param filename: Filename for the multipart upload, e.g.
            ``"agent.tar.gz"``.
        :param tool_callables: Optional mapping from tool name to
            an executable callable (sync or async) for client-side
            tool execution. Validated against the agent's
            spec-declared tools at stream-start time (the first
            ``send()`` / ``query()`` / ``stream()`` call), not at
            construction. See :class:`SessionsChat` for the
            validation rules.
        :param hooks: Optional lifecycle hooks fired from sessions
            stream events.
        :returns: A :class:`SessionsChat` ready for use.
        :raises OmnigentError: If session creation fails.
        """
        return await SessionsChat.create(
            namespace=self.sessions,
            bundle=bundle,
            filename=filename,
            files_namespace=self.files,
            tool_callables=tool_callables,
            agent_tools_getter=self._fetch_agent_tools,
            hooks=hooks,
        )

    async def _fetch_agent_tools(
        self, agent_id: str, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Fetch the spec-declared tool entries for an agent.

        Used as the ``agent_tools_getter`` injection for
        :class:`SessionsChat`. Reads the tool list off the
        :class:`Agent` returned by ``GET /api/agents/{agent_id}``.

        The server's :class:`AgentObject`
        carries a ``tools`` list where each entry has a ``name``
        and a ``runtime`` discriminator
        (``"server"`` or ``"client"``). When that field is not yet
        present, the SDK's :class:`Agent` dataclass simply lacks
        the field and this returns ``[]`` — which means
        validation succeeds for any caller that doesn't pass
        ``tool_callables``, and fails loud (with a clear "extra
        callable" message) for any caller that does. That is the
        correct degraded behavior: in F1's absence we cannot
        verify the spec, but we will never silently accept a
        broken setup.

        :param agent_id: The agent's durable identifier, e.g.
            ``"ag_abc123"``.
        :returns: List of tool-entry dicts with at least ``name``
            and (post-F1) ``runtime`` keys. Empty if the agent
            declares no tools or the server response shape
            predates F1.
        :raises OmnigentError: If the agents endpoint returns
            a non-2xx (e.g. 404).
        """
        if session_id is None:
            return []  # No session context — cannot resolve agent tools
        path = f"{self._base_url}/v1/sessions/{session_id}/agent"
        resp = await self._http.get(path)
        if resp.status_code != 200:
            return []
        agent_data = resp.json()
        tools = agent_data.get("tools")
        if isinstance(tools, list):
            return [t for t in tools if isinstance(t, dict)]
        return []

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self) -> OmnigentClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _resolve_tool_handler(
    *,
    tools: list[Callable[..., Any]] | None,
    tool_handler: ToolHandler | None,
) -> ToolHandler | None:
    """Pick one of ``tools=`` or ``tool_handler=``; reject both.

    :param tools: High-level list of ``@tool``-decorated functions.
    :param tool_handler: Low-level pre-built handler.
    :returns: The handler to use, or ``None`` if neither was given.
    :raises ValueError: If both were provided.
    """
    if tools is not None and tool_handler is not None:
        raise ValueError(
            "Pass either `tools=[...]` or `tool_handler=...`, not both. "
            "`tools=` is the high-level API (auto-builds a handler from "
            "@tool-decorated functions); `tool_handler=` is the low-level "
            "escape hatch."
        )
    if tools is not None:
        # Local import keeps the dep inside the tools subpackage.
        from .tools import build_tool_handler

        return build_tool_handler(tools)
    return tool_handler
