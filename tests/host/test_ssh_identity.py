"""SSH discovery uses the active loopback service and degrades without SSH."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from omnigent.host.ssh_identity import ssh_host_key_fingerprints


@pytest.mark.parametrize(
    "failure", [None, FileNotFoundError(), subprocess.TimeoutExpired("ssh-keyscan", 3)]
)
def test_ssh_identity_probe(monkeypatch: pytest.MonkeyPatch, failure: Exception | None) -> None:
    monkeypatch.setattr("omnigent.host.ssh_identity.sys.platform", "linux")
    run = Mock(
        side_effect=failure,
        return_value=subprocess.CompletedProcess(
            [],
            0,
            "# server banner\n127.0.0.1 ssh-ed25519 aGVsbG8=\n"
            "127.0.0.1 ssh-ed25519 aGVsbG8=\n127.0.0.1 ssh-rsa !!!\n"
            "other-host ssh-rsa d29ybGQ=\n",
        ),
    )
    monkeypatch.setattr("omnigent.host.ssh_identity.subprocess.run", run)
    result = ssh_host_key_fingerprints()
    assert result == (None if failure else ["SHA256:LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ"])
    run.assert_called_once_with(
        ["ssh-keyscan", "-T", "2", "-t", "ecdsa,ed25519,rsa", "127.0.0.1"],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_ssh_identity_unavailable(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr("omnigent.host.ssh_identity.sys.platform", platform)
    run = Mock(return_value=subprocess.CompletedProcess([], 1, ""))
    monkeypatch.setattr("omnigent.host.ssh_identity.subprocess.run", run)
    assert ssh_host_key_fingerprints() is None
    assert run.call_count == (1 if platform == "linux" else 0)
