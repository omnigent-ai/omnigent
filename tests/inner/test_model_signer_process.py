"""Concrete signer subprocess protocol tests."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

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

    readiness = await signer.start()
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
    assert {path.name for path in readiness.ca_bundle_path.parent.iterdir()} == {
        "ca-bundle.pem",
        "relay.sock",
    }

    ready = start_relay(readiness.relay_port, readiness.socket_path)
    assert ready.wait(timeout=5)
    proxy = f"http://127.0.0.1:{readiness.relay_port}"
    async with httpx.AsyncClient(
        proxy=proxy,
        verify=ssl.create_default_context(cafile=str(readiness.ca_bundle_path)),
        trust_env=False,
        timeout=10,
    ) as client:
        response = await client.post(
            "https://model.test/v1/responses",
            headers={"Authorization": f"Bearer {readiness.placeholder}"},
            json={"model": "fake"},
        )
        assert response.status_code == 200
        assert response.json() == {"upstream_saw_fake_bearer": True}

        missing = await client.post(
            "https://model.test/v1/responses",
            json={"model": "fake"},
        )
        assert missing.status_code == 403

        queried = await client.post(
            "https://model.test/v1/responses?debug=true",
            headers={"Authorization": f"Bearer {readiness.placeholder}"},
            json={"model": "fake"},
        )
        assert queried.status_code == 403

        redirect = await client.post(
            "https://model.test/v1/responses",
            headers={"Authorization": f"Bearer {readiness.placeholder}"},
            json={"test_redirect": "https://attacker.test/steal"},
        )
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "https://attacker.test/steal"

    async with httpx.AsyncClient(
        proxy=proxy,
        verify=ssl.create_default_context(cafile=str(readiness.ca_bundle_path)),
        trust_env=False,
        follow_redirects=True,
        timeout=10,
    ) as redirecting_client:
        with pytest.raises(httpx.ProxyError):
            await redirecting_client.post(
                "https://model.test/v1/responses",
                headers={"Authorization": f"Bearer {readiness.placeholder}"},
                json={"test_redirect": "https://attacker.test/steal"},
            )

    await signer.close()
    assert await signer.wait() == 0
    assert not marker.exists()
    assert not readiness.socket_path.exists()
    assert not readiness.ca_bundle_path.exists()
