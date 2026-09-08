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
from urllib.parse import urlsplit

from .egress.ca import ensure_ca, ensure_ca_bundle
from .egress.certs import HostCertCache
from .egress.proxy import EgressProxy, _parse_http_headers
from .egress.rules import parse_rules
from .model_auth import PROVIDER_AUTH_REQUIRED, ProviderAuthRequired, mint_ucode_token
from .model_egress import FrozenModelRoute
from .model_signing import SigningRejected, reconstruct_signed_request

_CONFIG_KEYS = frozenset({"binding_id", "endpoint", "routes"})
_UCODE_CONFIG_KEYS = _CONFIG_KEYS | {"auth_profile"}
_ROUTE_KEYS = frozenset({"method", "host", "path"})
_TEST_BINDING = "test-fake-provider-v1"
_UCODE_BINDING = "databricks-ucode-v1"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_HEADER_BYTES = 64 * 1024


class _SignerRelay(EgressProxy):
    """Exact-route MITM relay whose credential exists only in this process."""

    def __init__(
        self,
        *,
        route: FrozenModelRoute,
        placeholder: str,
        bearer_token: str,
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
        self._bearer_token = bearer_token
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
            signed = reconstruct_signed_request(
                route=self._route,
                placeholder=self._placeholder,
                bearer_token=self._bearer_token,
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
            bytes_relayed, _ = await self._relay_response(upstream_reader, client_writer)
            if bytes_relayed == 0:
                await self._send_bad_gateway(
                    client_writer,
                    "trusted provider returned no response",
                )
        finally:
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()


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
        json.loads(body)
        expected_line = f"{route.method} {route.path} HTTP/1.1\r\n".encode("ascii")
        authorized = (
            first_line == expected_line
            and headers.get_all("Authorization", []) == [f"Bearer {bearer_token}"]
            and headers.get_all("Host", []) == [route.host]
        )
        response_body = json.dumps(
            {"upstream_saw_fake_bearer": authorized},
            separators=(",", ":"),
        ).encode()
        status = b"200 OK" if authorized else b"401 Unauthorized"
        writer.write(
            b"HTTP/1.1 "
            + status
            + b"\r\nContent-Type: application/json\r\nContent-Length: "
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
    if binding_id == _UCODE_BINDING:
        assert auth_profile is not None
        bearer_token = await mint_ucode_token(
            host=f"https://{route.host}",
            profile=auth_profile,
        )
    else:
        bearer_token = f"fake-provider-bearer-{secrets.token_urlsafe(32)}"
    private_dir = Path(tempfile.mkdtemp(prefix="omnigent-model-signer-private-")).resolve()
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
                    bearer_token=bearer_token,
                ),
                "127.0.0.1",
                0,
                ssl=provider_context,
            )
            provider_port = int(provider.sockets[0].getsockname()[1])
        relay = _SignerRelay(
            route=route,
            placeholder=placeholder,
            bearer_token=bearer_token,
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
