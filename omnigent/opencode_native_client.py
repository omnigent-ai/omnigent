"""Typed HTTP + SSE client for an ``opencode serve`` native server.

Shaped from the pinned OpenCode OpenAPI (``opencode`` 1.17.x,
``packages/sdk/openapi.json``). This is a thin typed wrapper over the v1
REST endpoints the Omnigent OpenCode-native harness needs plus the SSE
``GET /event`` stream — not a full generated SDK. Unknown response fields
are preserved under ``raw`` for forward-compatible logging and fixtures.

Transport notes:

- REST + SSE over ``httpx.AsyncClient``; the server binds loopback only.
- Basic auth headers (``OPENCODE_SERVER_PASSWORD``) are attached per
  request when provided.
- SSE is parsed with standard ``event:`` / ``data:`` framing; each event
  payload is OpenCode's ``{id?, type, properties}`` envelope.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

_logger = logging.getLogger(__name__)

# Pinned OpenCode CLI/API version range. The source monorepo reports
# 1.17.7; we accept 1.17.x and refuse 1.18+ until validated.
OPENCODE_MIN_VERSION = "1.17.7"
OPENCODE_MAX_VERSION_EXCLUSIVE = "1.18.0"

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass(frozen=True)
class OpenCodeSession:
    """
    An OpenCode session as returned by the REST API.

    :param id: OpenCode session id, e.g. ``"ses_abc123"``.
    :param title: Optional human-readable title.
    :param parent_id: Parent session id for forked/child sessions.
    :param directory: Session working directory, when reported.
    :param raw: The full server payload for forward-compatibility.
    """

    id: str
    title: str | None = None
    parent_id: str | None = None
    directory: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OpenCodeSession:
        """
        Build an :class:`OpenCodeSession` from a raw server payload.

        :param payload: Decoded JSON object from ``/session`` endpoints.
        :returns: Parsed session.
        :raises ValueError: When the payload has no string ``id``.
        """
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("OpenCode session payload missing string 'id'")
        title = payload.get("title")
        parent_id = payload.get("parentID")
        directory = payload.get("directory")
        return cls(
            id=session_id,
            title=title if isinstance(title, str) else None,
            parent_id=parent_id if isinstance(parent_id, str) else None,
            directory=directory if isinstance(directory, str) else None,
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
    properties: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_envelope(
        cls, envelope: Mapping[str, Any], *, event_id: str | None = None
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
        return cls(
            id=envelope.get("id") if isinstance(envelope.get("id"), str) else event_id,
            type=type_value if isinstance(type_value, str) else "",
            properties=props if isinstance(props, dict) else {},
            raw=dict(envelope),
        )


class OpenCodeClientError(RuntimeError):
    """Raised when an OpenCode REST call returns a non-2xx response."""


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
            default_headers.setdefault("x-opencode-directory", directory)
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

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """
        Issue a request and return decoded JSON, raising on HTTP errors.

        :param method: HTTP method, e.g. ``"POST"``.
        :param path: Path relative to ``base_url``, e.g. ``"/session"``.
        :returns: Decoded JSON (object/array/scalar), or ``None`` for an
            empty body.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        response = await self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise OpenCodeClientError(
                f"OpenCode {method} {path} failed: {response.status_code} {response.text[:500]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return None

    # --- sessions --------------------------------------------------------

    async def create_session(self, payload: Mapping[str, Any] | None = None) -> OpenCodeSession:
        """
        Create an OpenCode session (``POST /session``).

        :param payload: Optional create body, e.g. ``{"title": "..."}``.
            Note: OpenCode's create body only accepts ``title`` / ``parentID``
            — it does NOT accept a model (the model is a per-prompt field on
            ``POST /session/{id}/message``). Pin the model per prompt via
            :func:`omnigent.opencode_http_transport.build_prompt_payload`.
        :returns: The created session.
        """
        data = await self._request_json("POST", "/session", json=dict(payload or {}))
        if not isinstance(data, Mapping):
            raise OpenCodeClientError("OpenCode create_session returned a non-object body")
        return OpenCodeSession.from_payload(data)

    async def get_session(self, session_id: str) -> OpenCodeSession | None:
        """
        Fetch one session (``GET /session/{id}``).

        :param session_id: OpenCode session id.
        :returns: The session, or ``None`` when it does not exist.
        """
        response = await self._client.request("GET", f"/session/{session_id}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise OpenCodeClientError(
                f"OpenCode get_session failed: {response.status_code} {response.text[:500]}"
            )
        data = response.json()
        if not isinstance(data, Mapping):
            return None
        return OpenCodeSession.from_payload(data)

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        """
        List a session's messages (``GET /session/{id}/message``).

        :param session_id: OpenCode session id.
        :returns: A list of message objects (each typically
            ``{"info": {...}, "parts": [...]}``).
        """
        data = await self._request_json("GET", f"/session/{session_id}/message")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def get_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        """
        Fetch one message (``GET /session/{id}/message/{messageID}``).

        :param session_id: OpenCode session id.
        :param message_id: OpenCode message id.
        :returns: The message object, or ``{}`` when absent.
        """
        data = await self._request_json("GET", f"/session/{session_id}/message/{message_id}")
        return data if isinstance(data, dict) else {}

    async def list_models(self) -> list[dict[str, Any]]:
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

    async def prompt(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """
        Send a (blocking) prompt (``POST /session/{id}/message``).

        :param session_id: OpenCode session id.
        :param payload: Prompt body, e.g. ``{"parts": [...]}``.
        :returns: The server response object (often the assistant message).
        """
        data = await self._request_json(
            "POST", f"/session/{session_id}/message", json=dict(payload)
        )
        return data if isinstance(data, dict) else {}

    async def prompt_async(self, session_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """
        Admit a prompt without blocking (``POST /session/{id}/prompt_async``).

        Preferred for native-server parity: the call returns once the
        prompt is admitted; the assistant output streams over SSE.

        :param session_id: OpenCode session id.
        :param payload: Prompt body, e.g. ``{"parts": [...]}``.
        :returns: The server response object (may be empty).
        """
        data = await self._request_json(
            "POST", f"/session/{session_id}/prompt_async", json=dict(payload)
        )
        return data if isinstance(data, dict) else {}

    async def abort(self, session_id: str) -> bool:
        """
        Abort active work (``POST /session/{id}/abort``).

        :param session_id: OpenCode session id.
        :returns: ``True`` when the server reports an abort happened.
        """
        data = await self._request_json("POST", f"/session/{session_id}/abort")
        return bool(data)

    async def summarize(self, session_id: str, *, provider_id: str, model_id: str) -> bool:
        """
        Compact a session in place (``POST /session/{id}/summarize``).

        opencode summarizes the session with the given model and emits a
        ``session.compacted`` event when done. (The v2
        ``POST /api/session/{id}/compact`` endpoint returns ``503 "Session
        compact is not available yet"`` in 1.17.x — verified against a live
        ``opencode serve`` — so this uses the v1 ``/summarize`` path, which
        requires the model explicitly.)

        :param session_id: OpenCode session id.
        :param provider_id: Provider id for the compaction model, e.g.
            ``"anthropic"``.
        :param model_id: Model id for the compaction model, e.g.
            ``"claude-sonnet-4-5"``.
        :returns: ``True`` once opencode has accepted the compaction request.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        await self._request_json(
            "POST",
            f"/session/{session_id}/summarize",
            json={"providerID": provider_id, "modelID": model_id},
        )
        return True

    async def seed_context(
        self,
        session_id: str,
        text: str,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> bool:
        """
        Inject a context message without triggering a reply (``noReply``).

        Used to rehydrate a fresh session with prior conversation context on a
        cross-host resume (opencode has no history-import API). ``noReply``
        admits the message as history without running a model turn.

        :param session_id: OpenCode session id.
        :param text: The context text to seed (e.g. the prior transcript).
        :param provider_id: Optional model provider (opencode requires only
            ``parts``, but a model keeps the seeded message attributed).
        :param model_id: Optional model id.
        :returns: ``True`` once opencode has accepted the message.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}], "noReply": True}
        if provider_id and model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        await self._request_json("POST", f"/session/{session_id}/message", json=body)
        return True

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
            "POST", f"/question/{request_id}/reply", json={"answers": answers}
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

    async def fork(
        self, session_id: str, payload: Mapping[str, Any] | None = None
    ) -> OpenCodeSession:
        """
        Fork a session (``POST /session/{id}/fork``).

        :param session_id: Source OpenCode session id.
        :param payload: Optional fork body, e.g. ``{"messageID": "msg_..."}``.
        :returns: The new forked session.
        """
        data = await self._request_json(
            "POST", f"/session/{session_id}/fork", json=dict(payload or {})
        )
        if not isinstance(data, Mapping):
            raise OpenCodeClientError("OpenCode fork returned a non-object body")
        return OpenCodeSession.from_payload(data)

    # --- permissions -----------------------------------------------------

    async def list_permissions(self) -> list[dict[str, Any]]:
        """
        List pending permission requests (``GET /permission``).

        :returns: A list of permission request objects.
        """
        data = await self._request_json("GET", "/permission")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def reply_permission(self, request_id: str, reply: Mapping[str, Any]) -> bool:
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
