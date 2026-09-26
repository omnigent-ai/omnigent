"""Typed HTTP + SSE client for an OpenCode 2.x ``opencode serve`` server.

Hand-shaped from the ``@opencode/cli`` 2.0.x OpenAPI (``/api/*`` routes). This
is a thin typed wrapper over the endpoints the Omnigent OpenCode-native harness
needs plus the SSE ``GET /api/event`` stream — not a full generated SDK.

Transport notes:

- REST + SSE over ``httpx.AsyncClient``; the server binds loopback only.
- Basic auth (``opencode:<OPENCODE_PASSWORD>``) is attached per request.
- JSON bodies arrive as ``{"data": ...}`` (or ``{"location", "data"}``);
  :func:`_unwrap` strips the envelope. ``/api/info`` and ``/interrupt``
  answer with bare objects, which :func:`_unwrap` passes through.
- SSE frames are ``data: {id, created, type, location?, data}`` lines;
  ``: heartbeat`` comments are skipped.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias

import httpx

from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

# Supported OpenCode CLI/API range: the 2.x ``/api/*`` protocol.
OPENCODE_MIN_VERSION = "2.0.0"
OPENCODE_MAX_VERSION_EXCLUSIVE = "3.0.0"

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

_JsonMapping: TypeAlias = Mapping[str, object]

# Upper bound on message pages fetched by list_messages (guards a cursor loop).
_MAX_MESSAGE_PAGES = 1000


def _unwrap(body: object) -> object:
    """
    Strip OpenCode's response envelope.

    :param body: Decoded JSON, e.g. ``{"data": {...}}``,
        ``{"location": {...}, "data": [...]}``, or a bare ``/api/info`` object.
    :returns: ``body["data"]`` when present, else *body* unchanged.
    """
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


@dataclass(frozen=True)
class OpenCodeSession:
    """
    An OpenCode session as returned by ``/api/session`` endpoints.

    :param id: OpenCode session id, e.g. ``"ses_abc123"``.
    :param title: Optional human-readable title.
    :param parent_id: Parent session id for child (subagent) sessions.
    :param directory: Session location directory, when reported.
    :param model: The session's ``{"id", "providerID", "variant"?}``, when set.
    :param raw: The full server payload for forward-compatibility.
    """

    id: str
    title: str | None = None
    parent_id: str | None = None
    directory: str | None = None
    model: dict[str, Any] | None = None
    raw: _JsonObject = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: _JsonMapping) -> OpenCodeSession:
        """
        Build an :class:`OpenCodeSession` from a ``Session.Info`` payload.

        :param payload: Decoded, unwrapped session object.
        :returns: Parsed session.
        :raises ValueError: When the payload has no string ``id``.
        """
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("OpenCode session payload missing string 'id'")
        title = payload.get("title")
        parent_id = payload.get("parentID")
        location = payload.get("location")
        directory = location.get("directory") if isinstance(location, Mapping) else None
        model = payload.get("model")
        return cls(
            id=session_id,
            title=title if isinstance(title, str) else None,
            parent_id=parent_id if isinstance(parent_id, str) else None,
            directory=directory if isinstance(directory, str) else None,
            model=dict(model) if isinstance(model, Mapping) else None,
            raw=dict(payload),
        )


@dataclass(frozen=True)
class OpenCodeEvent:
    """
    One decoded OpenCode SSE event.

    :param id: Optional SSE event id.
    :param type: Event discriminator, e.g. ``"message.part.updated"`` or
        ``"session.next.text.delta"``.
    :param properties: The event's ``properties`` object.
    :param raw: The full decoded envelope for debugging/forward-compat.
    """

    id: str | None
    type: str
    properties: _JsonObject
    raw: _JsonObject

    @classmethod
    def from_envelope(
        cls, envelope: _JsonMapping, *, event_id: str | None = None
    ) -> OpenCodeEvent:
        """
        Build an :class:`OpenCodeEvent` from a decoded SSE data object.

        :param envelope: Decoded JSON, e.g.
            ``{"type": "message.part.updated", "properties": {...}}``.
        :param event_id: Optional SSE ``id:`` framing value.
        :returns: Parsed event; unknown shapes get ``type=""``.
        """
        type_value = envelope.get("type")
        props = envelope.get("properties")
        envelope_id = envelope.get("id")
        return cls(
            id=envelope_id if isinstance(envelope_id, str) else event_id,
            type=type_value if isinstance(type_value, str) else "",
            properties=props if isinstance(props, dict) else {},
            raw=dict(envelope),
        )


class OpenCodeClientError(RuntimeError):
    """
    Raised when an OpenCode REST call fails.

    :param message: Human-readable failure.
    :param status_code: HTTP status of the failing response, or ``None`` when
        the failure was not an HTTP status (e.g. a malformed body).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenCodeClient:
    """
    Async HTTP + SSE client for one ``opencode serve`` server.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:49231"``.
    :param headers: Optional default headers (e.g. basic auth).
    :param directory: Optional workspace directory; sent as the
        ``x-opencode-directory`` header so ``serve`` routes per-request
        instances to the right workspace.
    :param client: Optional injected ``httpx.AsyncClient`` (tests pass a
        client backed by ``httpx.MockTransport``).
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        directory: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        default_headers: dict[str, str] = dict(headers or {})
        if directory:
            # The server URI-decodes this header, so non-ASCII paths survive.
            default_headers.setdefault(
                "x-opencode-directory", urllib.parse.quote(directory, safe="/")
            )
        self._directory = directory
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers=default_headers,
            timeout=_DEFAULT_TIMEOUT,
        )
        # When a client is injected (tests), still apply our headers so
        # auth/directory routing is exercised.
        if client is not None:
            for key, value in default_headers.items():
                self._client.headers.setdefault(key, value)

    @property
    def base_url(self) -> str:
        """:returns: The server base URL this client targets."""
        return self._base_url

    async def aclose(self) -> None:
        """Close the underlying client when this wrapper owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenCodeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- helpers ---------------------------------------------------------

    async def _request_body(
        self,
        method: str,
        path: str,
        *,
        json_body: _JsonMapping | None = None,
        params: Mapping[str, str] | None = None,
    ) -> object:
        """
        Issue a request and return the decoded JSON body without unwrapping.

        :param method: HTTP method, e.g. ``"POST"``.
        :param path: Path relative to ``base_url``, e.g. ``"/api/session"``.
        :param json_body: Optional JSON request body.
        :param params: Optional query parameters.
        :returns: Decoded JSON, or ``None`` for an empty (e.g. 204) body.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        response = await self._client.request(
            method,
            path,
            json=dict(json_body) if json_body is not None else None,
            params=dict(params) if params is not None else None,
        )
        if response.status_code >= 400:
            raise OpenCodeClientError(
                f"OpenCode {method} {path} failed: {response.status_code} {response.text[:500]}",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        try:
            decoded: object = response.json()
        except json.JSONDecodeError:
            return None
        return decoded

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: _JsonMapping | None = None,
        params: Mapping[str, str] | None = None,
    ) -> object:
        """
        Issue a request and return the ``data``-unwrapped JSON body.

        :param method: HTTP method.
        :param path: Path relative to ``base_url``.
        :param json_body: Optional JSON request body.
        :param params: Optional query parameters.
        :returns: The unwrapped body (see :func:`_unwrap`).
        :raises OpenCodeClientError: On a non-2xx status.
        """
        body = await self._request_body(method, path, json_body=json_body, params=params)
        return _unwrap(body)

    # --- server ----------------------------------------------------------

    async def info(self) -> _JsonObject:
        """
        Fetch server info (``GET /api/info``).

        :returns: ``{"version": "2.0.18", "pid": ..., "urls": [...], "paths": {...}}``.
        :raises OpenCodeClientError: On a non-2xx status or a non-object body.
        """
        data = await self._request_json("GET", "/api/info")
        if not isinstance(data, dict):
            raise OpenCodeClientError("OpenCode /api/info returned a non-object body")
        return data

    # --- sessions --------------------------------------------------------

    async def create_session(
        self,
        *,
        title: str,
        directory: str,
        permissions: list[_JsonObject] | None = None,
        model: _JsonObject | None = None,
        metadata: _JsonObject | None = None,
    ) -> OpenCodeSession:
        """
        Create a session (``POST /api/session``).

        :param title: Session title, e.g. ``"omnigent:conv_abc"``.
        :param directory: Workspace directory the session is located in.
        :param permissions: Session rules, e.g.
            ``[{"action": "*", "resource": "*", "effect": "ask"}]``.
        :param model: Initial ``{"id", "providerID", "variant"?}``.
        :param metadata: Free-form metadata, e.g.
            ``{"omnigent_conversation": "conv_abc"}``.
        :returns: The created session.
        :raises OpenCodeClientError: On a non-2xx status or a non-object body.
        """
        body: _JsonObject = {"title": title, "location": {"directory": directory}}
        if permissions is not None:
            body["permissions"] = permissions
        if model is not None:
            body["model"] = model
        if metadata is not None:
            body["metadata"] = metadata
        data = await self._request_json("POST", "/api/session", json_body=body)
        if not isinstance(data, Mapping):
            raise OpenCodeClientError("OpenCode create_session returned a non-object body")
        return OpenCodeSession.from_payload(data)

    async def get_session(self, session_id: str) -> OpenCodeSession | None:
        """
        Fetch one session (``GET /api/session/{id}``).

        :param session_id: OpenCode session id.
        :returns: The session, or ``None`` when it does not exist (404).
        :raises OpenCodeClientError: On any other non-2xx status.
        """
        try:
            data = await self._request_json("GET", f"/api/session/{session_id}")
        except OpenCodeClientError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(data, Mapping):
            return None
        return OpenCodeSession.from_payload(data)

    async def list_messages(
        self, session_id: str, *, after_id: str | None = None
    ) -> list[_JsonObject]:
        """
        List a session's messages, oldest first (``GET /api/session/{id}/message``).

        Follows ``cursor.next`` across pages until the server stops returning one.

        :param session_id: OpenCode session id.
        :param after_id: When set, only messages after this message id are
            returned; all messages are returned when the id is not found.
        :returns: v2 message objects, e.g. ``{"id": "msg_1", "type": "assistant", ...}``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        path = f"/api/session/{session_id}/message"
        messages: list[_JsonObject] = []
        params: dict[str, str] = {"order": "asc"}
        seen_cursors: set[str] = set()
        for _ in range(_MAX_MESSAGE_PAGES):
            body = await self._request_body("GET", path, params=params)
            if not isinstance(body, dict):
                break
            page = body.get("data")
            if isinstance(page, list):
                messages.extend(item for item in page if isinstance(item, dict))
            cursor = body.get("cursor")
            next_cursor = cursor.get("next") if isinstance(cursor, dict) else None
            if not page or not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            params = {"cursor": next_cursor}
        if after_id is None:
            return messages
        for index, message in enumerate(messages):
            if message.get("id") == after_id:
                return messages[index + 1 :]
        return messages

    async def list_root_sessions(self, *, limit: int = 100) -> list[OpenCodeSession]:
        """
        List top-level sessions, newest first (``GET /api/session?parentID=null``).

        :param limit: Maximum sessions to return.
        :returns: Root (non-subagent) sessions; malformed rows are skipped.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json(
            "GET",
            "/api/session",
            params={"parentID": "null", "order": "desc", "limit": str(limit)},
        )
        if not isinstance(data, list):
            return []
        return [
            OpenCodeSession.from_payload(item)
            for item in data
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item.get("id")
        ]

    async def get_context(self, session_id: str) -> list[_JsonObject]:
        """
        Fetch the messages the model sees next turn (``GET .../context``).

        :param session_id: OpenCode session id.
        :returns: v2 message objects after the latest compaction boundary.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json("GET", f"/api/session/{session_id}/context")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def list_models(self) -> list[_JsonObject]:
        """
        List available models (``GET /api/model``).

        :returns: A list of model objects; empty when the server exposes
            no model catalog.
        """
        data = await self._request_json("GET", "/api/model")
        if isinstance(data, dict):
            models = data.get("models")
            if isinstance(models, list):
                return [m for m in models if isinstance(m, dict)]
        return []

    async def prompt(
        self,
        session_id: str,
        *,
        text: str,
        files: Sequence[Mapping[str, str]] | None = None,
        delivery: str = "steer",
        message_id: str | None = None,
    ) -> _JsonObject:
        """
        Admit a user prompt (``POST /api/session/{id}/prompt``).

        Returns once OpenCode has accepted the input; output streams over SSE.

        :param session_id: OpenCode session id.
        :param text: Prompt text.
        :param files: Attachments, each ``{"uri": "data:<mime>;base64,...", "name": ...}``.
        :param delivery: ``"steer"`` (join the active turn or start one) or
            ``"queue"`` (run after the active turn).
        :param message_id: Optional client-chosen ``msg_`` id.
        :returns: The admitted inbox entry, e.g. ``{"id": "msg_1", "type": "user", ...}``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        body: _JsonObject = {"text": text, "delivery": delivery}
        if files:
            body["files"] = [dict(entry) for entry in files]
        if message_id is not None:
            body["id"] = message_id
        data = await self._request_json(
            "POST", f"/api/session/{session_id}/prompt", json_body=body
        )
        return data if isinstance(data, dict) else {}

    async def seed_context(self, session_id: str, text: str) -> None:
        """
        Record context in a session without running a turn.

        Used to rehydrate a fresh session with a prior transcript. Sends
        ``prompt {resume: false}``; when the server rejects that with a 4xx,
        falls back to ``POST .../synthetic {resume: false}``.

        :param session_id: OpenCode session id.
        :param text: Context to record, e.g. the rendered prior transcript.
        :raises OpenCodeClientError: When the prompt fails with a 5xx, or both
            calls fail.
        """
        body = {"text": text, "resume": False}
        try:
            await self._request_json("POST", f"/api/session/{session_id}/prompt", json_body=body)
        except OpenCodeClientError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise
            await self._request_json(
                "POST", f"/api/session/{session_id}/synthetic", json_body=body
            )

    async def set_model(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        variant: str | None = None,
    ) -> None:
        """
        Switch the session's model (``POST /api/session/{id}/model``).

        :param session_id: OpenCode session id.
        :param provider_id: Provider id, e.g. ``"opencode"``.
        :param model_id: Model id within the provider, e.g. ``"big-pickle"``.
        :param variant: Optional model variant, e.g. ``"high"``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        model: _JsonObject = {"id": model_id, "providerID": provider_id}
        if variant is not None:
            model["variant"] = variant
        await self._request_json(
            "POST", f"/api/session/{session_id}/model", json_body={"model": model}
        )

    async def interrupt(self, session_id: str) -> bool:
        """
        Interrupt active work (``POST /api/session/{id}/interrupt``).

        :param session_id: OpenCode session id.
        :returns: ``True`` when an active execution was interrupted.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json("POST", f"/api/session/{session_id}/interrupt")
        return bool(data.get("interrupted")) if isinstance(data, dict) else False

    async def compact(self, session_id: str) -> _JsonObject:
        """
        Queue a compaction (``POST /api/session/{id}/compact``).

        Progress arrives as ``session.compaction.*`` events.

        :param session_id: OpenCode session id.
        :returns: The queued compaction inbox entry.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json("POST", f"/api/session/{session_id}/compact", json_body={})
        return data if isinstance(data, dict) else {}

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        """
        Answer a ``question`` tool request (``POST /question/{id}/reply``).

        The opencode ``question`` tool blocks until answered. ``answers`` is one
        entry per question, each a list of the selected option labels (single
        choice → a one-element list). Verified live against ``opencode serve``
        1.17.7: ``{"answers": [["Tabs"]]}`` resolves the question (emits
        ``question.replied`` → ``session.idle``). The GLOBAL ``/question`` path
        is used (the session-scoped one is not an API route).

        :param request_id: OpenCode question request id (``que_…``).
        :param answers: Selected labels per question, in question order.
        :returns: ``True`` on a 2xx response.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        await self._request_json(
            "POST", f"/question/{request_id}/reply", json_body={"answers": answers}
        )
        return True

    async def reject_question(self, request_id: str) -> bool:
        """
        Reject a ``question`` tool request (``POST /question/{id}/reject``).

        Unblocks the opencode ``question`` tool without an answer (the tool
        reports the question was declined).

        :param request_id: OpenCode question request id (``que_…``).
        :returns: ``True`` on a 2xx response.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        await self._request_json("POST", f"/question/{request_id}/reject")
        return True

    async def fork(self, session_id: str, *, before: str | None = None) -> OpenCodeSession:
        """
        Fork a session (``POST /api/session/{id}/fork``).

        :param session_id: Source OpenCode session id.
        :param before: Optional ``msg_`` id; the fork keeps history before it.
        :returns: The new forked session.
        :raises OpenCodeClientError: On a non-2xx status or a non-object body.
        """
        body: _JsonObject = {"before": before} if before is not None else {}
        data = await self._request_json("POST", f"/api/session/{session_id}/fork", json_body=body)
        if not isinstance(data, Mapping):
            raise OpenCodeClientError("OpenCode fork returned a non-object body")
        return OpenCodeSession.from_payload(data)

    # --- permissions -----------------------------------------------------

    async def list_permissions(self) -> list[_JsonObject]:
        """
        List pending permission requests (``GET /permission``).

        :returns: A list of permission request objects.
        """
        data = await self._request_json("GET", "/permission")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def reply_permission(self, request_id: str, reply: _JsonMapping) -> bool:
        """
        Reply to a permission request (``POST /permission/{id}/reply``).

        :param request_id: OpenCode permission request id.
        :param reply: Reply body, e.g. ``{"reply": "once"}`` where reply is
            one of ``once`` / ``always`` / ``reject``.
        :returns: ``True`` on a 2xx response.
        """
        response = await self._client.request(
            "POST", f"/permission/{request_id}/reply", json=dict(reply)
        )
        if response.status_code >= 400:
            raise OpenCodeClientError(
                f"OpenCode reply_permission failed: {response.status_code} {response.text[:500]}"
            )
        return True

    # --- events ----------------------------------------------------------

    async def events(self) -> AsyncIterator[OpenCodeEvent]:
        """
        Stream server events over SSE (``GET /event``).

        Yields one :class:`OpenCodeEvent` per parsed SSE event. The
        iterator ends when the server closes the stream; callers own
        reconnect/backoff.

        :returns: Async iterator of decoded events.
        """
        async with self._client.stream("GET", "/event", timeout=None) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise OpenCodeClientError(
                    f"OpenCode /event failed: {response.status_code} {body[:200]!r}"
                )
            async for event in _parse_sse(response.aiter_lines()):
                yield event


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[OpenCodeEvent]:
    """
    Parse a stream of SSE lines into :class:`OpenCodeEvent` objects.

    Implements the subset of the SSE spec OpenCode uses: ``id:``,
    ``event:`` and (possibly multi-line) ``data:`` fields, with a blank
    line dispatching the accumulated event. ``data`` payloads are decoded
    as JSON; non-JSON data blocks are skipped (logged at debug).

    :param lines: Async iterator of decoded SSE text lines.
    :returns: Async iterator of parsed events.
    """
    event_id: str | None = None
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\n").rstrip("\r")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                current_id = event_id
                event_id = None
                parsed = _decode_event(payload, current_id)
                if parsed is not None:
                    yield parsed
            else:
                event_id = None
            continue
        if line.startswith(":"):
            # SSE comment / heartbeat.
            continue
        field_name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            data_lines.append(value)
        elif field_name == "id":
            event_id = value
        # ``event:`` and ``retry:`` are accepted but unused; OpenCode
        # encodes the discriminator inside the JSON ``type`` field.
    # Flush a trailing event with no terminating blank line.
    if data_lines:
        parsed = _decode_event("\n".join(data_lines), event_id)
        if parsed is not None:
            yield parsed


def _decode_event(payload: str, event_id: str | None) -> OpenCodeEvent | None:
    """
    Decode one SSE ``data`` payload into an :class:`OpenCodeEvent`.

    :param payload: Raw JSON text from one or more ``data:`` lines.
    :param event_id: Optional SSE ``id:`` value for the event.
    :returns: Parsed event, or ``None`` when the payload is not a JSON
        object.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        _logger.debug("Skipping non-JSON OpenCode SSE data: %s", payload[:200])
        return None
    if not isinstance(decoded, dict):
        return None
    return OpenCodeEvent.from_envelope(decoded, event_id=event_id)
