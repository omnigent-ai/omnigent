"""Loopback proxy stripping Responses params non-OpenAI gateway models reject.

Codex serializes ``parallel_tool_calls`` on every Responses request and has no
config to omit it (a model-catalog ``supports_parallel_tool_calls`` flag only
flips the value), while Databricks-hosted non-OpenAI models reject any request
carrying the field — a capability gate on gpt-oss/llama, a schema rejection on
qwen/gemma — so every turn fails. Nothing client-side can drop the field, so a
non-OpenAI launch routes codex through this loopback proxy instead, which
removes it before forwarding to the real gateway.

The proxy holds no credentials: codex attaches its own ``Authorization``
header, which is forwarded verbatim. It listens on loopback only, the same
exposure as the app-server's own loopback websocket listener.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import httpx

from omnigent.models.codex_model_vocabulary import is_openai_codex_model

_logger = logging.getLogger(__name__)

#: Request fields OpenAI's own Responses API accepts but gateway-hosted
#: non-OpenAI models reject on sight, whatever the value.
_OPENAI_ONLY_REQUEST_FIELDS = ("parallel_tool_calls",)

# Hop-by-hop headers never forwarded in either direction (RFC 9110 §7.6.1).
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Generous single-request bounds: Codex turn payloads carry full context but
# stay far below these; anything bigger is malformed and refused.
_MAX_HEADER_BYTES = 256 * 1024
_MAX_BODY_BYTES = 512 * 1024 * 1024

# No read timeout: a Responses stream legitimately idles between tokens and
# codex already enforces its own ``stream_idle_timeout_ms`` client-side.
_UPSTREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0)


def rewritten_responses_request_body(raw: bytes) -> bytes:
    """Drop OpenAI-only fields from a Responses request body.

    :param raw: The request body as received from codex.
    :returns: The body to forward — rewritten when it is a JSON object whose
        ``model`` is not OpenAI-served and that carries an OpenAI-only field,
        otherwise the input unchanged (malformed JSON included, so the
        gateway, not this proxy, reports the error).
    """
    try:
        body = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(body, dict):
        return raw
    if is_openai_codex_model(body.get("model")):
        return raw
    if not any(field in body for field in _OPENAI_ONLY_REQUEST_FIELDS):
        return raw
    for field in _OPENAI_ONLY_REQUEST_FIELDS:
        body.pop(field, None)
    return json.dumps(body).encode("utf-8")


class CodexResponsesCompatProxy:
    """Loopback reverse proxy for one codex launch's model provider.

    Forwards every request to *upstream_base_url* with the incoming path
    appended, rewriting ``POST …/responses`` bodies through
    :func:`rewritten_responses_request_body`. Responses stream back
    unbuffered (SSE included) on close-delimited HTTP/1.1 connections.

    :param upstream_base_url: The provider base URL the launch resolved,
        e.g. ``"https://host/ai-gateway/codex/v1"``.
    """

    def __init__(self, upstream_base_url: str) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self._server: asyncio.base_events.Server | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        """The loopback URL codex's provider config should point at."""
        if self._server is None:
            raise RuntimeError("gateway compat proxy is not started")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        """Bind the loopback listener and the upstream client."""
        if self._server is not None:
            return
        self._client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT)
        self._server = await asyncio.start_server(
            self._handle_connection, "127.0.0.1", 0, limit=_MAX_HEADER_BYTES
        )

    async def aclose(self) -> None:
        """Stop the listener and release the upstream client. Idempotent."""
        server, self._server = self._server, None
        client, self._client = self._client, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._forward_one_request(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            # Client went away or sent garbage; nothing to answer.
            pass
        except httpx.HTTPError as exc:
            _logger.warning("codex gateway compat proxy upstream error: %s", exc)
            with contextlib.suppress(OSError):
                payload = json.dumps({"error": f"upstream request failed: {exc}"}).encode()
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"content-type: application/json\r\n"
                    b"content-length: " + str(len(payload)).encode() + b"\r\n"
                    b"connection: close\r\n\r\n" + payload
                )
                await writer.drain()
        finally:
            with contextlib.suppress(OSError):
                writer.close()
                await writer.wait_closed()

    async def _forward_one_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        method, target, headers = await _read_request_head(reader)
        body = await _read_request_body(reader, headers)
        header_names = {name.lower() for name, _ in headers}
        if (
            method == "POST"
            and target.split("?", 1)[0].rstrip("/").endswith("/responses")
            and body
            and "content-encoding" not in header_names
        ):
            body = rewritten_responses_request_body(body)
        forward_headers = [
            (name, value)
            for name, value in headers
            if name.lower() not in _HOP_BY_HOP_HEADERS
            and name.lower() not in ("host", "content-length", "accept-encoding")
        ]
        # Identity keeps the streamed-back bytes byte-identical to what the
        # response headers describe (httpx transparently decodes otherwise).
        forward_headers.append(("accept-encoding", "identity"))
        client = self._client
        if client is None:
            raise httpx.TransportError("proxy is closed")
        async with client.stream(
            method,
            f"{self._upstream_base_url}{target}",
            headers=forward_headers,
            content=body,
        ) as upstream:
            status = upstream.status_code
            reason = upstream.reason_phrase or ""
            head = [f"HTTP/1.1 {status} {reason}".rstrip().encode()]
            for name, value in upstream.headers.raw:
                lowered = name.decode("latin-1").lower()
                if lowered in _HOP_BY_HOP_HEADERS or lowered in (
                    "content-length",
                    "content-encoding",
                ):
                    continue
                head.append(name + b": " + value)
            head.append(b"connection: close")
            writer.write(b"\r\n".join(head) + b"\r\n\r\n")
            await writer.drain()
            async for chunk in upstream.aiter_bytes():
                writer.write(chunk)
                await writer.drain()


async def _read_request_head(
    reader: asyncio.StreamReader,
) -> tuple[str, str, list[tuple[str, str]]]:
    """Read the request line and headers, returning (method, target, headers)."""
    head = await reader.readuntil(b"\r\n\r\n")
    request_line, _, header_block = head.partition(b"\r\n")
    parts = request_line.decode("latin-1").split()
    if len(parts) != 3:
        raise asyncio.IncompleteReadError(partial=head, expected=None)
    method, target, _version = parts
    headers: list[tuple[str, str]] = []
    for line in header_block.decode("latin-1").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep:
            headers.append((name.strip(), value.strip()))
    return method, target, headers


async def _read_request_body(
    reader: asyncio.StreamReader, headers: list[tuple[str, str]]
) -> bytes:
    """Read a content-length or chunked request body; empty when neither."""
    lowered = {name.lower(): value for name, value in headers}
    if "chunked" in lowered.get("transfer-encoding", "").lower():
        chunks: list[bytes] = []
        total = 0
        while True:
            size_line = await reader.readline()
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                break
            total += size
            if total > _MAX_BODY_BYTES:
                raise asyncio.LimitOverrunError("request body too large", total)
            chunks.append(await reader.readexactly(size))
            await reader.readexactly(2)  # trailing CRLF after each chunk
        return b"".join(chunks)
    length = int(lowered.get("content-length") or 0)
    if length > _MAX_BODY_BYTES:
        raise asyncio.LimitOverrunError("request body too large", length)
    if length <= 0:
        return b""
    return await reader.readexactly(length)
