"""Signer-owned model relay process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import stat
import sys
import tempfile
import threading
import weakref
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

from .egress.ca import ensure_ca, ensure_ca_bundle
from .egress.certs import HostCertCache
from .egress.proxy import EgressProxy, _parse_http_headers
from .egress.rules import parse_rules
from .model_auth import (
    PROVIDER_AUTH_REQUIRED,
    ProviderAuthRequired,
    _provider_auth_message,
    mint_ucode_token,
)
from .model_credential import CredentialLifecycle
from .model_egress import FrozenModelRoute
from .model_signing import SigningRejected, reconstruct_signed_request

_CONFIG_KEYS = frozenset({"binding_id", "endpoint", "routes"})
_UCODE_CONFIG_KEYS = _CONFIG_KEYS | {"auth_profile"}
_ROUTE_KEYS = frozenset({"method", "host", "path"})
_TEST_BINDING = "test-fake-provider-v1"
_UCODE_BINDING = "databricks-ucode-v1"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_HEADER_BYTES = 64 * 1024
_MAX_HEADER_LINE_BYTES = 8 * 1024
_MAX_BODY_BYTES = 10 * 1024 * 1024
_MAX_RESPONSE_BODY_BYTES = 64 * 1024 * 1024
_RESPONSE_IDLE_TIMEOUT_SECONDS = 60.0
_RESPONSE_TOTAL_TIMEOUT_SECONDS = 10 * 60.0
_HEADER_NAME_RE = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_STATUS_LINE_RE = re.compile(rb"HTTP/1\.1 ([1-5][0-9]{2})(?: ([\x20-\x7e]*))?\r\n\Z")
_REQUEST_REJECTED_HEADERS = frozenset(
    {
        "connection",
        "expect",
        "forwarded",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "websocket",
        "x-http-method-override",
        "x-original-url",
        "x-rewrite-url",
    }
)
_RESPONSE_ALLOWED_HEADERS = frozenset(
    {
        "cache-control",
        "content-type",
        "location",
        "openai-processing-ms",
        "openai-request-id",
        "request-id",
        "retry-after",
        "x-request-id",
    }
)
_UCODE_MAX_CACHE_AGE_SECONDS = 5 * 60.0
_UCODE_REFRESH_SKEW_SECONDS = 30.0
_UCODE_MIN_REFRESH_INTERVAL_SECONDS = 2.0
_UCODE_BACKOFF_BASE_SECONDS = 2.0
_UCODE_BACKOFF_MAX_SECONDS = 30.0
_E2E_MARKER = "BROKERED_E2E_OK upstream_saw_signer_only_fake_bearer=true"
_VALIDATION_BEARER = "request-validation-only"
_T = TypeVar("_T")


class _CredentialSource(Protocol):
    async def token_for_request(self) -> str: ...

    def invalidate_after_unauthorized(self) -> None: ...


class _StaticCredential:
    def __init__(self, token: str) -> None:
        self._token = token

    async def token_for_request(self) -> str:
        return self._token

    def invalidate_after_unauthorized(self) -> None:
        pass


@dataclass(frozen=True)
class _ParsedResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    content_length: int | None
    chunked: bool


def _parse_strict_request_line(raw: bytes, route: FrozenModelRoute) -> tuple[str, str]:
    expected = f"{route.method} {route.path} HTTP/1.1\r\n".encode("ascii")
    if raw != expected:
        raise SigningRejected("request line must be the exact frozen HTTP/1.1 route")
    return route.method, route.path


def _parse_strict_header_lines(
    raw: bytes,
    *,
    repeatable_names: frozenset[str] = frozenset(),
) -> list[tuple[str, str]]:
    if len(raw) > _MAX_HEADER_BYTES:
        raise SigningRejected("header block exceeds the configured limit")
    if not raw.endswith(b"\r\n\r\n"):
        raise SigningRejected("header block is incomplete or not CRLF-delimited")
    lines = raw[:-4].split(b"\r\n")
    if any(b"\n" in line or b"\r" in line for line in lines):
        raise SigningRejected("header block is not CRLF-delimited")
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        if not line or len(line) > _MAX_HEADER_LINE_BYTES or line[:1] in (b" ", b"\t"):
            raise SigningRejected("request contains a malformed header line")
        name_raw, separator, value_raw = line.partition(b":")
        if not separator or _HEADER_NAME_RE.fullmatch(name_raw) is None:
            raise SigningRejected("request contains an invalid header name")
        if value_raw.startswith(b" "):
            value_raw = value_raw[1:]
        if value_raw.startswith(b" ") or value_raw.endswith(b" "):
            raise SigningRejected("request contains ambiguous header whitespace")
        if any(byte < 0x20 or byte > 0x7E for byte in value_raw):
            raise SigningRejected("request contains an invalid header value")
        name = name_raw.decode("ascii")
        lowered = name.lower()
        if lowered in seen and lowered not in repeatable_names:
            raise SigningRejected("duplicate request header is not allowed")
        seen.add(lowered)
        parsed.append((name, value_raw.decode("ascii")))
    return parsed


def _parse_strict_request_headers(raw: bytes) -> tuple[list[tuple[str, str]], int]:
    parsed = _parse_strict_header_lines(raw)
    by_name = {name.lower(): value for name, value in parsed}
    for name in by_name:
        if name in _REQUEST_REJECTED_HEADERS or name.startswith(
            ("proxy-", "sec-websocket-", "x-forwarded-")
        ):
            raise SigningRejected("request contains a forbidden transport or routing header")
    length_raw = by_name.get("content-length")
    if (
        length_raw is None
        or not length_raw
        or not length_raw.isascii()
        or not length_raw.isdecimal()
    ):
        raise SigningRejected("exactly one decimal Content-Length is required")
    if len(length_raw) > 20:
        raise SigningRejected("Content-Length exceeds the configured limit")
    length = int(length_raw)
    if length > _MAX_BODY_BYTES:
        raise SigningRejected("request body exceeds the configured limit")
    return parsed, length


def _parse_strict_response_head(status_line: bytes, headers_raw: bytes) -> _ParsedResponse:
    match = _STATUS_LINE_RE.fullmatch(status_line)
    if match is None:
        raise ValueError("malformed upstream status line")
    status = int(match.group(1))
    parsed = _parse_strict_header_lines(
        headers_raw,
        repeatable_names=frozenset({"set-cookie"}),
    )
    by_name = {name.lower(): value for name, value in parsed}
    transfer_encoding = by_name.get("transfer-encoding")
    content_length_raw = by_name.get("content-length")
    if transfer_encoding is not None and content_length_raw is not None:
        raise ValueError("ambiguous upstream response framing")
    if transfer_encoding is not None and transfer_encoding.lower() != "chunked":
        raise ValueError("unsupported upstream response transfer coding")
    content_length: int | None = None
    if content_length_raw is not None:
        if (
            not content_length_raw
            or not content_length_raw.isascii()
            or not content_length_raw.isdecimal()
            or len(content_length_raw) > 20
        ):
            raise ValueError("invalid upstream response Content-Length")
        content_length = int(content_length_raw)
        if content_length > _MAX_RESPONSE_BODY_BYTES:
            raise ValueError("upstream response body exceeds the configured limit")
    allowed = tuple(
        (name, value)
        for name, value in parsed
        if name.lower() in _RESPONSE_ALLOWED_HEADERS or name.lower().startswith("x-ratelimit-")
    )
    return _ParsedResponse(
        status=status,
        headers=allowed,
        content_length=content_length,
        chunked=transfer_encoding is not None,
    )


class _SignerRelay(EgressProxy):
    """Exact-route MITM relay whose credential exists only in this process."""

    def __init__(
        self,
        *,
        route: FrozenModelRoute,
        placeholder: str,
        bearer_token: str | None = None,
        credential_source: _CredentialSource | None = None,
        ca_cert_path: Path,
        ca_key_path: Path,
        ca_bundle_path: Path,
        provider_port: int | None,
    ) -> None:
        super().__init__(
            parse_rules([f"{route.method} {route.host}{route.path}"]),
            ca_cert_path,
            ca_key_path,
            upstream_ca_bundle=ca_bundle_path,
            block_private_destinations=provider_port is None,
        )
        self._route = route
        self._placeholder = placeholder
        if (bearer_token is None) == (credential_source is None):
            raise ValueError("signer relay requires exactly one credential source")
        self._credential_source = credential_source or _StaticCredential(bearer_token or "")
        self._provider_port = provider_port
        self._tls_sni_by_task: dict[asyncio.Task[object], str | None] = {}
        self._tls_sni_by_object: weakref.WeakKeyDictionary[
            ssl.SSLObject | ssl.SSLSocket, str | None
        ] = weakref.WeakKeyDictionary()

    def _configure_server_ssl_context(self, host: str, ssl_ctx: ssl.SSLContext) -> None:
        del host

        def _capture_sni(
            ssl_socket: ssl.SSLObject | ssl.SSLSocket,
            server_name: str | None,
            context: ssl.SSLSocket,
        ) -> int | None:
            del context
            self._tls_sni_by_object[ssl_socket] = server_name.lower() if server_name else None
            return None

        ssl_ctx.set_servername_callback(_capture_sni)

    def _tls_handshake_completed(self, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        ssl_object = writer.get_extra_info("ssl_object")
        if task is not None and ssl_object is not None:
            self._tls_sni_by_task[task] = self._tls_sni_by_object.pop(ssl_object, None)

    def _tls_connection_closed(self) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tls_sni_by_task.pop(task, None)

    def _parse_inner_request_line(self, raw: bytes) -> tuple[str, str]:
        return _parse_strict_request_line(raw, self._route)

    @staticmethod
    def _parse_inner_content_length(headers_raw: bytes) -> int:
        _, length = _parse_strict_request_headers(headers_raw)
        return length

    async def _assert_destination_allowed(self, host: str, port: int) -> str | None:
        if host.lower() != self._route.host or port != 443:
            raise PermissionError("destination is outside the signer route")
        if self._provider_port is not None:
            return "127.0.0.1"
        return await super()._assert_destination_allowed(host, port)

    async def _forward_https(
        self,
        client_writer: asyncio.StreamWriter,
        host: str,
        port: int,
        method: str,
        path: str,
        request_line: bytes,
        headers_raw: bytes,
        body: bytes,
    ) -> None:
        del request_line
        headers, _ = _parse_strict_request_headers(headers_raw)
        request_host = next(
            (value for name, value in headers if name.lower() == "host"),
            "",
        )
        task = asyncio.current_task()
        sni_host = self._tls_sni_by_task.pop(task, None) if task is not None else None
        try:
            reconstruct_signed_request(
                route=self._route,
                placeholder=self._placeholder,
                bearer_token=_VALIDATION_BEARER,
                method=method,
                connect_host=host,
                sni_host=sni_host or "",
                request_host=request_host,
                target=path,
                headers=headers,
                body=body,
            )
        except SigningRejected:
            await self._send_forbidden(client_writer, "")
            return
        try:
            bearer_token = await self._credential_source.token_for_request()
        except ProviderAuthRequired:
            await self._send_auth_required(client_writer)
            return
        signed = reconstruct_signed_request(
            route=self._route,
            placeholder=self._placeholder,
            bearer_token=bearer_token,
            method=method,
            connect_host=host,
            sni_host=sni_host or "",
            request_host=request_host,
            target=path,
            headers=headers,
            body=body,
        )
        if port != 443:
            await self._send_forbidden(client_writer, "")
            return

        try:
            connect_host = (
                "127.0.0.1"
                if self._provider_port is not None
                else await self._assert_destination_allowed(signed.host, 443)
            )
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    connect_host or signed.host,
                    self._provider_port or 443,
                    ssl=self._upstream_ssl_ctx,
                    server_hostname=signed.host,
                ),
                timeout=30,
            )
        except (asyncio.TimeoutError, OSError):
            await self._send_bad_gateway(client_writer, "")
            return

        try:
            upstream_writer.write(f"{signed.method} {signed.path} HTTP/1.1\r\n".encode("ascii"))
            for name, value in signed.headers:
                upstream_writer.write(f"{name}: {value}\r\n".encode("latin-1"))
            upstream_writer.write(b"Connection: close\r\n\r\n")
            upstream_writer.write(signed.body)
            await upstream_writer.drain()
            bytes_relayed, _ = await self._relay_response_observing_status(
                upstream_reader,
                client_writer,
            )
            if bytes_relayed == 0:
                await self._send_bad_gateway(
                    client_writer,
                    "",
                )
        finally:
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()

    def _observe_upstream_status(self, status: int) -> None:
        if status == 401:
            self._credential_source.invalidate_after_unauthorized()

    async def _relay_response_observing_status(
        self,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> tuple[int, int]:
        """Validate, sanitize, and stream one bounded upstream response."""
        started = asyncio.get_running_loop().time()
        try:
            status_line = await self._read_response_part(
                upstream_reader.readline(),
                started=started,
            )
            headers_raw = await self._read_response_headers(upstream_reader, started=started)
            response = _parse_strict_response_head(status_line, headers_raw)
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            OSError,
            SigningRejected,
            ValueError,
        ):
            return 0, 0
        self._observe_upstream_status(response.status)
        try:
            reason = http.HTTPStatus(response.status).phrase
        except ValueError:
            reason = ""
        downstream_head = f"HTTP/1.1 {response.status} {reason}\r\n".encode("ascii")
        for name, value in response.headers:
            downstream_head += f"{name}: {value}\r\n".encode("ascii")
        if response.content_length is not None:
            downstream_head += f"Content-Length: {response.content_length}\r\n".encode("ascii")
        downstream_head += b"Connection: close\r\n\r\n"
        client_writer.write(downstream_head)
        await self._read_response_part(client_writer.drain(), started=started)
        bytes_relayed = len(downstream_head)
        try:
            if response.chunked:
                body_bytes = await self._relay_chunked_body(
                    upstream_reader,
                    client_writer,
                    started=started,
                )
            elif response.content_length is not None:
                body_bytes = await self._relay_fixed_body(
                    upstream_reader,
                    client_writer,
                    length=response.content_length,
                    started=started,
                )
            elif response.status in (204, 304) or 100 <= response.status < 200:
                body_bytes = 0
            else:
                body_bytes = await self._relay_eof_body(
                    upstream_reader,
                    client_writer,
                    started=started,
                )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError, ValueError):
            return bytes_relayed, response.status
        return bytes_relayed + body_bytes, response.status

    @staticmethod
    async def _read_response_part(
        awaitable: Awaitable[_T],
        *,
        started: float,
    ) -> _T:
        remaining = _RESPONSE_TOTAL_TIMEOUT_SECONDS - (asyncio.get_running_loop().time() - started)
        if remaining <= 0:
            close = getattr(awaitable, "close", None)
            if close is not None:
                close()
            raise asyncio.TimeoutError
        return await asyncio.wait_for(
            awaitable,
            timeout=min(_RESPONSE_IDLE_TIMEOUT_SECONDS, remaining),
        )

    async def _read_response_headers(
        self,
        reader: asyncio.StreamReader,
        *,
        started: float,
    ) -> bytes:
        raw = bytearray()
        while True:
            line = await self._read_response_part(reader.readline(), started=started)
            if not line or not line.endswith(b"\r\n"):
                raise ValueError("incomplete upstream response headers")
            raw.extend(line)
            if len(raw) > _MAX_HEADER_BYTES:
                raise ValueError("upstream response headers exceed the configured limit")
            if line == b"\r\n":
                return bytes(raw)

    async def _relay_fixed_body(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        length: int,
        started: float,
    ) -> int:
        relayed = 0
        while relayed < length:
            size = min(64 * 1024, length - relayed)
            data = await self._read_response_part(reader.read(size), started=started)
            if not data:
                raise asyncio.IncompleteReadError(b"", size)
            writer.write(data)
            await self._read_response_part(writer.drain(), started=started)
            relayed += len(data)
        return relayed

    async def _relay_eof_body(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        started: float,
    ) -> int:
        relayed = 0
        while True:
            data = await self._read_response_part(reader.read(64 * 1024), started=started)
            if not data:
                return relayed
            relayed += len(data)
            if relayed > _MAX_RESPONSE_BODY_BYTES:
                raise ValueError("upstream response body exceeds the configured limit")
            writer.write(data)
            await self._read_response_part(writer.drain(), started=started)

    async def _relay_chunked_body(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        started: float,
    ) -> int:
        relayed = 0
        while True:
            line = await self._read_response_part(reader.readline(), started=started)
            if (
                not line.endswith(b"\r\n")
                or len(line) > 32
                or not line[:-2]
                or any(byte not in b"0123456789abcdefABCDEF" for byte in line[:-2])
            ):
                raise ValueError("invalid upstream chunk framing")
            size = int(line[:-2], 16)
            if size == 0:
                trailer_end = await self._read_response_part(
                    reader.readexactly(2),
                    started=started,
                )
                if trailer_end != b"\r\n":
                    raise ValueError("upstream response trailers are not supported")
                return relayed
            if relayed + size > _MAX_RESPONSE_BODY_BYTES:
                raise ValueError("upstream response body exceeds the configured limit")
            chunk_relayed = await self._relay_fixed_body(
                reader,
                writer,
                length=size,
                started=started,
            )
            terminator = await self._read_response_part(reader.readexactly(2), started=started)
            if terminator != b"\r\n":
                raise ValueError("invalid upstream chunk framing")
            relayed += chunk_relayed

    @staticmethod
    async def _send_forbidden(writer: asyncio.StreamWriter, message: str) -> None:
        del message
        await _send_generic_error(writer, 403, "Forbidden")

    @staticmethod
    async def _send_bad_gateway(writer: asyncio.StreamWriter, message: str) -> None:
        del message
        await _send_generic_error(writer, 502, "Bad Gateway")

    @staticmethod
    async def _send_auth_required(writer: asyncio.StreamWriter) -> None:
        body = b'{"error":{"code":"PROVIDER_AUTH_REQUIRED"}}'
        response = (
            b"HTTP/1.1 401 Unauthorized\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
        )
        with contextlib.suppress(Exception):
            writer.write(response)
            await writer.drain()


async def _send_generic_error(
    writer: asyncio.StreamWriter,
    status: int,
    reason: str,
) -> None:
    body = f"{status} {reason}\r\n".encode("ascii")
    response = (
        f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
        + b"Content-Type: text/plain\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: close\r\n\r\n"
        + body
    )
    with contextlib.suppress(Exception):
        writer.write(response)
        await writer.drain()


async def _fake_provider(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    route: FrozenModelRoute,
    bearer_token: str,
) -> None:
    try:
        first_line = await asyncio.wait_for(reader.readline(), timeout=10)
        header_block = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=10,
        )
        if len(first_line) + len(header_block) > _MAX_HEADER_BYTES:
            raise ValueError("request headers too large")
        headers = _parse_http_headers(header_block)
        length_values = headers.get_all("Content-Length", [])
        if len(length_values) != 1:
            raise ValueError("invalid content length")
        body = await asyncio.wait_for(reader.readexactly(int(length_values[0])), timeout=10)
        payload = json.loads(body)
        expected_line = f"{route.method} {route.path} HTTP/1.1\r\n".encode("ascii")
        authorized = (
            first_line == expected_line
            and headers.get_all("Authorization", []) == [f"Bearer {bearer_token}"]
            and headers.get_all("Host", []) == [route.host]
        )
        test_redirect = (
            isinstance(payload, dict)
            and payload.get("test_redirect") == "https://attacker.test/steal"
        )
        response_body, content_type = _fake_responses_payload(
            payload=payload,
            authorized=authorized,
        )
        status = (
            b"307 Temporary Redirect"
            if authorized and test_redirect
            else b"200 OK"
            if authorized
            else b"401 Unauthorized"
        )
        redirect_header = (
            b"Location: https://attacker.test/steal\r\n" if authorized and test_redirect else b""
        )
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\n"
            + redirect_header
            + b"Content-Type: "
            + content_type
            + b"\r\nContent-Length: "
            + str(len(response_body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + response_body
        )
        await writer.drain()
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        asyncio.TimeoutError,
        OSError,
        ValueError,
    ):
        with contextlib.suppress(Exception):
            writer.write(
                b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _fake_responses_payload(
    *,
    payload: object,
    authorized: bool,
) -> tuple[bytes, bytes]:
    """Return enough of the Responses protocol for an installed Codex turn."""
    if not isinstance(payload, dict) or not payload.get("stream"):
        return (
            json.dumps(
                {
                    "id": "resp_brokered_e2e",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": str(payload.get("model", "fake"))
                    if isinstance(payload, dict)
                    else "fake",
                    "output": (
                        [
                            {
                                "id": "msg_brokered_e2e",
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": _E2E_MARKER,
                                        "annotations": [],
                                    }
                                ],
                            }
                        ]
                        if authorized
                        else []
                    ),
                    "usage": {
                        "input_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 1,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 2,
                    },
                },
                separators=(",", ":"),
            ).encode(),
            b"application/json",
        )

    model = str(payload.get("model", "fake"))
    message = {
        "id": "msg_brokered_e2e",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [
            {
                "type": "output_text",
                "text": _E2E_MARKER,
                "annotations": [],
            }
        ],
    }
    completed = {
        "id": "resp_brokered_e2e",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": model,
        "output": [message],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
    }
    events = [
        (
            "response.created",
            {**completed, "status": "in_progress", "output": [], "usage": None},
        ),
        (
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {**message, "status": "in_progress", "content": []},
            },
        ),
        (
            "response.content_part.added",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ),
        (
            "response.output_text.delta",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "delta": _E2E_MARKER,
            },
        ),
        (
            "response.output_text.done",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "text": _E2E_MARKER,
            },
        ),
        (
            "response.content_part.done",
            {
                "item_id": message["id"],
                "output_index": 0,
                "content_index": 0,
                "part": message["content"][0],
            },
        ),
        ("response.output_item.done", {"output_index": 0, "item": message}),
        ("response.completed", {"response": completed}),
    ]
    body = b"".join(
        b"event: "
        + event.encode()
        + b"\ndata: "
        + json.dumps({"type": event, **data}, separators=(",", ":")).encode()
        + b"\n\n"
        for event, data in events
    )
    return body, b"text/event-stream"


def _load_config(fd: int) -> tuple[str, FrozenModelRoute, str | None]:
    with os.fdopen(fd, "rb", closefd=True) as stream:
        raw = stream.read(_MAX_CONFIG_BYTES + 1)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise ValueError("signer config is too large")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("signer config fields are invalid")
    binding_id = payload.get("binding_id")
    expected_keys = _UCODE_CONFIG_KEYS if binding_id == _UCODE_BINDING else _CONFIG_KEYS
    if set(payload) != expected_keys:
        raise ValueError("signer config fields are invalid")
    if binding_id not in (_TEST_BINDING, _UCODE_BINDING):
        raise ValueError("provider binding is unavailable")
    endpoint = urlsplit(str(payload["endpoint"]))
    routes = payload["routes"]
    if not isinstance(routes, list) or len(routes) != 1:
        raise ValueError("signer requires exactly one route")
    route_payload = routes[0]
    if not isinstance(route_payload, dict) or set(route_payload) != _ROUTE_KEYS:
        raise ValueError("signer route fields are invalid")
    route = FrozenModelRoute(
        method=str(route_payload["method"]),
        host=str(route_payload["host"]),
        path=str(route_payload["path"]),
    )
    endpoint_prefix = endpoint.path.rstrip("/")
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != route.host
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in (None, 443)
        or endpoint.query
        or endpoint.fragment
        or endpoint.path in ("", "/")
        or route.method != "POST"
        or route.path != f"{endpoint_prefix}/responses"
    ):
        raise ValueError("signer authority must be exact POST trusted /responses")
    auth_profile = str(payload["auth_profile"]) if binding_id == _UCODE_BINDING else None
    return str(binding_id), route, auth_profile


def _pick_relay_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _remove_owned_directory(path: Path | None, identity: tuple[int, int] | None) -> None:
    """Remove only the exact directory created by this signer."""
    if path is None or identity is None:
        return
    try:
        current = path.lstat()
    except OSError:
        return
    if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == identity:
        shutil.rmtree(path, ignore_errors=True)


async def _run(config_fd: int) -> int:
    binding_id, route, auth_profile = _load_config(config_fd)
    credential_lifecycle: CredentialLifecycle | None = None
    if binding_id == _UCODE_BINDING:
        assert auth_profile is not None
        host = f"https://{route.host}"
        credential_lifecycle = CredentialLifecycle(
            helper=lambda: mint_ucode_token(host=host, profile=auth_profile),
            auth_required=lambda: ProviderAuthRequired(_provider_auth_message(host, auth_profile)),
            max_cache_age_s=_UCODE_MAX_CACHE_AGE_SECONDS,
            refresh_skew_s=_UCODE_REFRESH_SKEW_SECONDS,
            min_refresh_interval_s=_UCODE_MIN_REFRESH_INTERVAL_SECONDS,
            backoff_base_s=_UCODE_BACKOFF_BASE_SECONDS,
            backoff_max_s=_UCODE_BACKOFF_MAX_SECONDS,
        )
        await credential_lifecycle.start()
        fake_bearer_token = None
    else:
        fake_bearer_token = f"fake-provider-bearer-{secrets.token_urlsafe(32)}"
    try:
        private_dir = Path(tempfile.mkdtemp(prefix="omnigent-model-signer-private-")).resolve()
    except BaseException:
        if credential_lifecycle is not None:
            await credential_lifecycle.close()
        raise
    public_dir: Path | None = None
    private_identity: tuple[int, int] | None = None
    public_identity: tuple[int, int] | None = None
    relay: _SignerRelay | None = None
    provider: asyncio.Server | None = None
    try:
        private_stat = private_dir.lstat()
        private_identity = (private_stat.st_dev, private_stat.st_ino)
        os.chmod(private_dir, 0o700)
        public_dir = Path(tempfile.mkdtemp(prefix="omnigent-model-signer-public-")).resolve()
        public_stat = public_dir.lstat()
        public_identity = (public_stat.st_dev, public_stat.st_ino)
        os.chmod(public_dir, 0o700)
        ca_cert, ca_key = ensure_ca(private_dir)
        ca_bundle = ensure_ca_bundle(ca_cert, public_dir)
        os.chmod(ca_cert, 0o400)
        os.chmod(ca_key, 0o600)
        os.chmod(ca_bundle, 0o444)
        placeholder = f"oa_cred_{secrets.token_urlsafe(24)}"
        provider_port: int | None = None
        if binding_id == _TEST_BINDING:
            provider_context = HostCertCache(ca_cert, ca_key).get_ssl_context(route.host)
            provider = await asyncio.start_server(
                lambda reader, writer: _fake_provider(
                    reader,
                    writer,
                    route=route,
                    bearer_token=fake_bearer_token or "",
                ),
                "127.0.0.1",
                0,
                ssl=provider_context,
            )
            provider_port = int(provider.sockets[0].getsockname()[1])
        relay = _SignerRelay(
            route=route,
            placeholder=placeholder,
            bearer_token=fake_bearer_token,
            credential_source=credential_lifecycle,
            ca_cert_path=ca_cert,
            ca_key_path=ca_key,
            ca_bundle_path=ca_bundle,
            provider_port=provider_port,
        )
        socket_path = public_dir / "relay.sock"
        await relay.start_unix(socket_path)
        os.chmod(socket_path, 0o600)
        readiness = {
            "status": "ready",
            "relay_port": _pick_relay_port(),
            "socket_path": str(socket_path),
            "ca_bundle_path": str(ca_bundle),
            "placeholder": placeholder,
        }
        sys.stdout.write(json.dumps(readiness, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        await asyncio.Future()
        raise AssertionError("unreachable")
    finally:
        if relay is not None:
            await relay.stop()
        if credential_lifecycle is not None:
            await credential_lifecycle.close()
        if provider is not None:
            provider.close()
            await provider.wait_closed()
        _remove_owned_directory(private_dir, private_identity)
        _remove_owned_directory(public_dir, public_identity)


async def _run_supervised(config_fd: int) -> int:
    """Cancel initialization and cleanup as soon as runner stdin closes."""
    loop = asyncio.get_running_loop()
    command_future: asyncio.Future[bytes] = loop.create_future()

    def _read_command() -> None:
        command = sys.stdin.buffer.readline()

        def _publish() -> None:
            if not command_future.done():
                command_future.set_result(command)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_publish)

    threading.Thread(target=_read_command, daemon=True).start()
    run_task = asyncio.create_task(_run(config_fd))
    done, _ = await asyncio.wait(
        (run_task, command_future),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if command_future in done:
        command = command_future.result()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        return 0 if command in (b"shutdown\n", b"") else 2
    command_future.cancel()
    return await run_task


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config-fd", required=True, type=int)
    args = parser.parse_args()
    try:
        return asyncio.run(_run_supervised(args.config_fd))
    except ProviderAuthRequired:
        sys.stdout.write(
            json.dumps(
                {"status": "error", "code": PROVIDER_AUTH_REQUIRED},
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()
        return 1
    except Exception:  # noqa: BLE001 - signer failures are intentionally opaque
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
