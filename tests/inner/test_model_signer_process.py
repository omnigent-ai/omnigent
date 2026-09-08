"""Concrete signer subprocess protocol tests."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import signal
import socket
import ssl
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner._proc import process_alive
from omnigent.inner.egress.proxy import EgressProxy
from omnigent.inner.egress.relay import start_relay
from omnigent.inner.model_egress import FrozenModelRoute
from omnigent.inner.model_signer import (
    ProviderAuthRequired,
    SignerLaunchConfig,
    SignerStartError,
    SubprocessModelSigner,
    _parse_readiness,
)
from omnigent.inner.model_signer_service import _SignerRelay


def _config(binding_id: str) -> SignerLaunchConfig:
    return SignerLaunchConfig(
        binding_id=binding_id,
        endpoint="https://workspace.cloud.databricks.com/serving-endpoints/openai",
        routes=(
            FrozenModelRoute(
                method="POST",
                host="workspace.cloud.databricks.com",
                path="/serving-endpoints/openai/responses",
            ),
        ),
    )


def _ucode_config() -> SignerLaunchConfig:
    return SignerLaunchConfig(
        binding_id="databricks-ucode-v1",
        endpoint="https://workspace.cloud.databricks.com/serving-endpoints/openai",
        routes=(
            FrozenModelRoute(
                method="POST",
                host="workspace.cloud.databricks.com",
                path="/serving-endpoints/openai/responses",
            ),
        ),
        auth_profile="agent-profile",
    )


def _write_child(path: Path) -> None:
    path.write_text(
        """
import json
import os
import sys

fd = int(sys.argv[sys.argv.index("--config-fd") + 1])
with os.fdopen(fd, "rb", closefd=True) as stream:
    config = json.loads(stream.read())
binding = config["binding_id"]
if any("token" in key.lower() for key in config):
    raise SystemExit(9)
if binding == "hang":
    for line in sys.stdin:
        if line.strip() == "shutdown":
            break
    raise SystemExit(0)
if binding == "stderr":
    sys.stderr.write("SECRET_FROM_HELPER\\n")
    raise SystemExit(2)
payload = {
    "status": "ready",
    "relay_port": 43123,
    "socket_path": "/private/signer/relay.sock",
    "ca_bundle_path": "/private/signer/ca.pem",
    "placeholder": "oa_cred_session",
}
if binding == "extra-field":
    payload["bearer_token"] = "SECRET_FROM_HELPER"
sys.stdout.write(json.dumps(payload) + "\\n")
sys.stdout.flush()
for line in sys.stdin:
    if line.strip() == "shutdown":
        break
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _raw_proxy_post(
    *,
    proxy_port: int,
    ca_bundle_path: Path,
    host: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    try:
        sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
        connect_response = bytearray()
        while b"\r\n\r\n" not in connect_response:
            data = sock.recv(4096)
            if not data:
                raise OSError("proxy closed during CONNECT")
            connect_response.extend(data)
        if not connect_response.startswith(b"HTTP/1.1 200 "):
            raise OSError("proxy rejected CONNECT")
        tls_sock = ssl.create_default_context(cafile=str(ca_bundle_path)).wrap_socket(
            sock,
            server_hostname=host,
        )
        sock = tls_sock
        request = bytearray(f"POST {path} HTTP/1.1\r\nHost: {host}\r\n".encode())
        for name, value in headers.items():
            request.extend(f"{name}: {value}\r\n".encode())
        request.extend(f"Content-Length: {len(body)}\r\n\r\n".encode())
        request.extend(body)
        tls_sock.sendall(request)
        response = http.client.HTTPResponse(tls_sock)
        response.begin()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        sock.close()


def _raw_proxy_pipeline_is_reauthorized(
    *,
    proxy_port: int,
    ca_bundle_path: Path,
    placeholder: str,
) -> tuple[int, int]:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    try:
        sock.sendall(b"CONNECT model.test:443 HTTP/1.1\r\nHost: model.test:443\r\n\r\n")
        connect_response = bytearray()
        while b"\r\n\r\n" not in connect_response:
            data = sock.recv(4096)
            if not data:
                raise OSError("proxy closed during CONNECT")
            connect_response.extend(data)
        tls_sock = ssl.create_default_context(cafile=str(ca_bundle_path)).wrap_socket(
            sock,
            server_hostname="model.test",
        )
        sock = tls_sock
        body = b'{"model":"fake"}'
        authorized_request = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: model.test\r\n"
            + f"Authorization: Bearer {placeholder}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        denied_request = authorized_request.replace(
            f"Authorization: Bearer {placeholder}\r\n".encode(),
            b"Authorization: Bearer wrong-placeholder\r\n",
        )
        tls_sock.sendall(authorized_request + denied_request)
        first = http.client.HTTPResponse(tls_sock)
        first.begin()
        first.read()
        second = http.client.HTTPResponse(tls_sock)
        second.begin()
        second.read()
        return first.status, second.status
    finally:
        sock.close()


@pytest.mark.skipif(sys.platform == "win32", reason="v1 signer uses config fd")
async def test_signer_receives_non_secret_config_and_returns_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = tmp_path / "signer_child.py"
    _write_child(child)
    monkeypatch.setattr(
        "omnigent.inner.model_signer._signer_child_argv",
        lambda: [sys.executable, str(child)],
    )
    signer = SubprocessModelSigner(_config("ok"))

    readiness = await signer.start()

    assert readiness.relay_port == 43123
    assert readiness.placeholder == "oa_cred_session"
    assert "SECRET" not in repr(readiness)
    await signer.close()
    assert await signer.wait() == 0


@pytest.mark.skipif(sys.platform == "win32", reason="v1 signer uses config fd")
async def test_readiness_with_secret_field_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = tmp_path / "signer_child.py"
    _write_child(child)
    monkeypatch.setattr(
        "omnigent.inner.model_signer._signer_child_argv",
        lambda: [sys.executable, str(child)],
    )
    signer = SubprocessModelSigner(_config("extra-field"))

    with pytest.raises(SignerStartError, match="invalid readiness"):
        await signer.start()

    assert await signer.wait() != 0


@pytest.mark.skipif(sys.platform == "win32", reason="v1 signer uses config fd")
async def test_helper_stderr_is_not_exposed_in_start_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = tmp_path / "signer_child.py"
    _write_child(child)
    monkeypatch.setattr(
        "omnigent.inner.model_signer._signer_child_argv",
        lambda: [sys.executable, str(child)],
    )
    signer = SubprocessModelSigner(_config("stderr"))

    with pytest.raises(SignerStartError) as raised:
        await signer.start()

    assert "SECRET_FROM_HELPER" not in str(raised.value)


@pytest.mark.skipif(sys.platform == "win32", reason="v1 signer uses config fd")
async def test_cancelled_signer_start_terminates_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = tmp_path / "signer_child.py"
    _write_child(child)
    monkeypatch.setattr(
        "omnigent.inner.model_signer._signer_child_argv",
        lambda: [sys.executable, str(child)],
    )
    signer = SubprocessModelSigner(_config("hang"))

    start = asyncio.create_task(signer.start())
    while signer._proc is None:
        await asyncio.sleep(0)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start

    assert signer._proc.returncode is not None


@pytest.mark.parametrize(
    "override",
    [
        {"relay_port": True},
        {"placeholder": "oa_cred_valid\nSECRET_PROTOCOL_INJECTION"},
    ],
)
def test_readiness_rejects_noncanonical_ipc_values(override: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "status": "ready",
        "relay_port": 43123,
        "socket_path": "/private/signer/relay.sock",
        "ca_bundle_path": "/private/signer/ca.pem",
        "placeholder": "oa_cred_session",
    }
    payload.update(override)

    with pytest.raises(SignerStartError, match="invalid readiness"):
        _parse_readiness(json.dumps(payload).encode(), _config("ok"))


async def test_signer_pins_validated_dns_result_and_rejects_authority_reroute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    relay = object.__new__(_SignerRelay)
    relay._route = _config("ok").routes[0]
    relay._provider_port = None
    resolve = AsyncMock(return_value="203.0.113.8")
    monkeypatch.setattr(EgressProxy, "_assert_destination_allowed", resolve)

    assert (
        await relay._assert_destination_allowed("workspace.cloud.databricks.com", 443)
        == "203.0.113.8"
    )
    resolve.assert_awaited_once_with("workspace.cloud.databricks.com", 443)

    resolve.reset_mock()
    with pytest.raises(PermissionError, match="outside the signer route"):
        await relay._assert_destination_allowed("attacker.example", 443)
    resolve.assert_not_awaited()


def test_upstream_401_only_invalidates_for_a_future_request() -> None:
    credential = Mock()
    credential.token_for_request = AsyncMock()
    relay = object.__new__(_SignerRelay)
    relay._credential_source = credential

    relay._observe_upstream_status(401)

    credential.invalidate_after_unauthorized.assert_called_once_with()
    credential.token_for_request.assert_not_awaited()


@pytest.mark.skipif(sys.platform == "win32", reason="v1 signer uses config fd")
async def test_ucode_auth_failure_has_stable_safe_recovery_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ucode = tmp_path / "ucode"
    ucode.write_text(
        "#!/bin/sh\n"
        "printf 'SECRET_HELPER_STDERR' >&2\n"
        "printf 'SECRET_TOKEN\\nextra-output\\n'\n"
        "exit 19\n",
        encoding="utf-8",
    )
    ucode.chmod(ucode.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    signer = SubprocessModelSigner(_ucode_config())

    with pytest.raises(ProviderAuthRequired) as raised:
        await signer.start()

    assert raised.value.code == "PROVIDER_AUTH_REQUIRED"
    message = str(raised.value)
    assert "SECRET_HELPER_STDERR" not in message
    assert "SECRET_TOKEN" not in message
    assert "ucode configure" in message
    assert (
        "databricks auth login --host https://workspace.cloud.databricks.com "
        "--profile agent-profile"
    ) in message
    assert await signer.wait() != 0


@pytest.mark.skipif(sys.platform == "win32", reason="signer relay uses a Unix socket")
async def test_ucode_auth_preflight_returns_only_non_secret_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ucode = tmp_path / "ucode"
    ucode.write_text("#!/bin/sh\nprintf 'SECRET_BEARER_VALUE\\n'\n", encoding="utf-8")
    ucode.chmod(ucode.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    signer = SubprocessModelSigner(_ucode_config())

    readiness = await signer.start()

    assert "SECRET_BEARER_VALUE" not in repr(readiness)
    assert readiness.socket_path.exists()
    await signer.close()
    assert await signer.wait() == 0


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX abrupt process death")
async def test_sigkill_signer_during_ucode_preflight_reaps_helper_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "helper-child.pid"
    ucode = tmp_path / "ucode"
    ucode.write_text(
        "#!/bin/sh\n"
        f"(trap '' TERM; while true; do sleep 1; done) >/dev/null 2>&1 &\n"
        f"printf '%s' \"$!\" > {child_pid_path}\n"
        "trap '' TERM\n"
        "while true; do sleep 1; done\n",
        encoding="utf-8",
    )
    ucode.chmod(ucode.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    signer = SubprocessModelSigner(_ucode_config())

    start = asyncio.create_task(signer.start())
    while signer._proc is None or not child_pid_path.exists():
        await asyncio.sleep(0.01)
    signer_pid = signer._proc.pid
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    os.kill(signer_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    with pytest.raises(SignerStartError):
        await start

    for _ in range(300):
        if not process_alive(child_pid):
            break
        await asyncio.sleep(0.02)
    assert not process_alive(child_pid)


@pytest.mark.skipif(sys.platform == "win32", reason="signer relay uses a Unix socket")
async def test_real_signer_relays_only_placeholder_authorized_responses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ucode-ran"
    ucode = tmp_path / "ucode"
    ucode.write_text(f"#!/bin/sh\n: > {marker}\nexit 99\n", encoding="utf-8")
    ucode.chmod(ucode.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin:/bin")
    config = SignerLaunchConfig(
        binding_id="test-fake-provider-v1",
        endpoint="https://model.test/v1",
        routes=(
            FrozenModelRoute(
                method="POST",
                host="model.test",
                path="/v1/responses",
            ),
        ),
    )
    signer = SubprocessModelSigner(config)
    private_before = set(Path("/tmp").glob("omnigent-model-signer-private-*"))

    readiness = await signer.start()
    private_dirs = set(Path("/tmp").glob("omnigent-model-signer-private-*")) - private_before
    assert len(private_dirs) == 1
    private_dir = private_dirs.pop()
    assert stat.S_IMODE(private_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((private_dir / "ca.pem").stat().st_mode) == 0o400
    assert stat.S_IMODE((private_dir / "ca-key.pem").stat().st_mode) == 0o600
    ready_payload = {
        "relay_port": readiness.relay_port,
        "socket_path": str(readiness.socket_path),
        "ca_bundle_path": str(readiness.ca_bundle_path),
        "placeholder": readiness.placeholder,
    }
    serialized_non_secret_state = json.dumps(
        {"config": config.to_jsonable(), "readiness": ready_payload},
        sort_keys=True,
    )
    assert "bearer_token" not in serialized_non_secret_state
    assert "fake-provider-bearer" not in serialized_non_secret_state
    assert readiness.ca_bundle_path.is_file()
    assert stat.S_IMODE(readiness.ca_bundle_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(readiness.ca_bundle_path.stat().st_mode) == 0o444
    assert stat.S_IMODE(readiness.socket_path.stat().st_mode) == 0o600
    assert {path.name for path in readiness.ca_bundle_path.parent.iterdir()} == {
        "ca-bundle.pem",
        "relay.sock",
    }

    ready = start_relay(readiness.relay_port, readiness.socket_path)
    assert ready.wait(timeout=5)
    status, _, body = await asyncio.to_thread(
        _raw_proxy_post,
        proxy_port=readiness.relay_port,
        ca_bundle_path=readiness.ca_bundle_path,
        host="model.test",
        path="/v1/responses",
        headers={
            "Authorization": f"Bearer {readiness.placeholder}",
            "Content-Type": "application/json",
        },
        body=b'{"model":"fake"}',
    )
    assert status == 200
    output = json.loads(body)["output"]
    assert output[0]["content"][0]["text"].startswith("BROKERED_E2E_OK")
    assert await asyncio.to_thread(
        _raw_proxy_pipeline_is_reauthorized,
        proxy_port=readiness.relay_port,
        ca_bundle_path=readiness.ca_bundle_path,
        placeholder=readiness.placeholder,
    ) == (200, 403)

    missing_status, _, _ = await asyncio.to_thread(
        _raw_proxy_post,
        proxy_port=readiness.relay_port,
        ca_bundle_path=readiness.ca_bundle_path,
        host="model.test",
        path="/v1/responses",
        headers={"Content-Type": "application/json"},
        body=b'{"model":"fake"}',
    )
    assert missing_status == 403

    queried_status, _, _ = await asyncio.to_thread(
        _raw_proxy_post,
        proxy_port=readiness.relay_port,
        ca_bundle_path=readiness.ca_bundle_path,
        host="model.test",
        path="/v1/responses?debug=true",
        headers={
            "Authorization": f"Bearer {readiness.placeholder}",
            "Content-Type": "application/json",
        },
        body=b'{"model":"fake"}',
    )
    assert queried_status == 403

    redirect_status, redirect_headers, _ = await asyncio.to_thread(
        _raw_proxy_post,
        proxy_port=readiness.relay_port,
        ca_bundle_path=readiness.ca_bundle_path,
        host="model.test",
        path="/v1/responses",
        headers={
            "Authorization": f"Bearer {readiness.placeholder}",
            "Content-Type": "application/json",
        },
        body=b'{"test_redirect":"https://attacker.test/steal"}',
    )
    assert redirect_status == 307
    assert redirect_headers["Location"] == "https://attacker.test/steal"

    with pytest.raises(OSError, match="rejected CONNECT"):
        await asyncio.to_thread(
            _raw_proxy_post,
            proxy_port=readiness.relay_port,
            ca_bundle_path=readiness.ca_bundle_path,
            host="attacker.test",
            path="/steal",
            headers={
                "Authorization": f"Bearer {readiness.placeholder}",
                "Content-Type": "application/json",
            },
            body=b'{"model":"fake"}',
        )

    await signer.close()
    assert await signer.wait() == 0
    assert not marker.exists()
    assert not readiness.socket_path.exists()
    assert not readiness.ca_bundle_path.exists()
    assert not private_dir.exists()
