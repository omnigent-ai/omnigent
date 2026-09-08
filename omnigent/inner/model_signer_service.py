"""Signer-owned model relay process."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Protocol
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
_UCODE_MAX_CACHE_AGE_SECONDS = 5 * 60.0
_UCODE_REFRESH_SKEW_SECONDS = 30.0
_UCODE_MIN_REFRESH_INTERVAL_SECONDS = 2.0
_UCODE_BACKOFF_BASE_SECONDS = 2.0
_UCODE_BACKOFF_MAX_SECONDS = 30.0
_E2E_MARKER = "BROKERED_E2E_OK upstream_saw_signer_only_fake_bearer=true"


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
        message = _parse_http_headers(headers_raw)
        host_values = message.get_all("Host", [])
        request_host = host_values[0] if len(host_values) == 1 else ""
        try:
            bearer_token = await self._credential_source.token_for_request()
        except ProviderAuthRequired:
            await self._send_auth_required(client_writer)
            return
        try:
            signed = reconstruct_signed_request(
                route=self._route,
                placeholder=self._placeholder,
                bearer_token=bearer_token,
                method=method,
                connect_host=host,
                sni_host=host,
                request_host=request_host,
                target=path,
                headers=list(message.raw_items()),
                body=body,
            )
        except SigningRejected as exc:
            await self._send_forbidden(client_writer, str(exc))
            return
        if port != 443:
            await self._send_forbidden(client_writer, "model route requires port 443")
            return

        try:
            connect_host = (
                "127.0.0.1"
                if self._provider_port is not None
                else await self._assert_destination_allowed(signed.host, 443)
            )
            upstream_reader, upstream_writer = await asyncio.open_connection(
                connect_host or signed.host,
                self._provider_port or 443,
                ssl=self._upstream_ssl_ctx,
                server_hostname=signed.host,
            )
        except OSError:
            await self._send_bad_gateway(client_writer, "trusted provider unavailable")
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
                    "trusted provider returned no response",
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
        """Observe only the status line needed for credential invalidation."""
        try:
            status_line = await asyncio.wait_for(
                upstream_reader.readline(),
                timeout=60,
            )
        except (asyncio.TimeoutError, OSError):
            return 0, 0
        if (
            not status_line.endswith(b"\r\n")
            or len(status_line) > _MAX_HEADER_BYTES
            or len(status_line) < 14
            or status_line[:7] not in (b"HTTP/1.",)
        ):
            return 0, 0
        parts = status_line.rstrip(b"\r\n").split(b" ", 2)
        if (
            len(parts) < 2
            or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1")
            or len(parts[1]) != 3
            or not parts[1].isdigit()
        ):
            return 0, 0
        status = int(parts[1])
        self._observe_upstream_status(status)
        client_writer.write(status_line)
        await client_writer.drain()
        relayed, _ = await self._relay_response(upstream_reader, client_writer)
        return len(status_line) + relayed, status

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
    relay: _SignerRelay | None = None
    provider: asyncio.Server | None = None
    try:
        os.chmod(private_dir, 0o700)
        public_dir = Path(tempfile.mkdtemp(prefix="omnigent-model-signer-public-")).resolve()
        os.chmod(public_dir, 0o700)
        ca_cert, ca_key = ensure_ca(private_dir)
        ca_bundle = ensure_ca_bundle(ca_cert, public_dir)
        os.chmod(ca_cert, 0o444)
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
        readiness = {
            "status": "ready",
            "relay_port": _pick_relay_port(),
            "socket_path": str(socket_path),
            "ca_bundle_path": str(ca_bundle),
            "placeholder": placeholder,
        }
        sys.stdout.write(json.dumps(readiness, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        command = await asyncio.to_thread(sys.stdin.buffer.readline)
        return 0 if command == b"shutdown\n" or command == b"" else 2
    finally:
        if relay is not None:
            await relay.stop()
        if credential_lifecycle is not None:
            await credential_lifecycle.close()
        if provider is not None:
            provider.close()
            await provider.wait_closed()
        shutil.rmtree(private_dir, ignore_errors=True)
        if public_dir is not None:
            shutil.rmtree(public_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--config-fd", required=True, type=int)
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args.config_fd))
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
