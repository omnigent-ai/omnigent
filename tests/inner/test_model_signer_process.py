"""Concrete signer subprocess protocol tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omnigent.inner.model_egress import FrozenModelRoute
from omnigent.inner.model_signer import (
    SignerLaunchConfig,
    SignerStartError,
    SubprocessModelSigner,
)


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
