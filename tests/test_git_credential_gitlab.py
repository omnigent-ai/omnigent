"""Tests for the managed-sandbox GitLab Git and glab credential helpers."""

from __future__ import annotations

import io
import sys

import pytest
import yaml

import omnigent.git_credential_gitlab as h
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture(autouse=True)
def _force_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")


def test_get_vends_credentials_only_for_connected_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *args: {
            "connected": True,
            "host": "https://gitlab.example",
            "token": "token",
            "username": "oauth2",
        },
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("protocol=https\nhost=gitlab.example\n\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    assert (
        h.main(["--server", "https://server", "--host-id", "host", "--host-token", "launch"]) == 0
    )
    assert out.getvalue() == "username=oauth2\npassword=token\n"


@pytest.mark.parametrize(
    "credential_request",
    [
        "protocol=http\nhost=gitlab.example\n\n",
        "protocol=https\nhost=other.example\n\n",
        "protocol=https\n\n",
    ],
)
def test_get_declines_untrusted_git_requests(
    monkeypatch: pytest.MonkeyPatch, credential_request: str
) -> None:
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *args: {"connected": True, "host": "https://gitlab.example", "token": "token"},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(credential_request))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    assert (
        h.main(["--server", "https://server", "--host-id", "host", "--host-token", "launch"]) == 0
    )
    assert out.getvalue() == ""


def test_get_requires_exact_instance_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *args: {"connected": True, "host": "https://gitlab.example:8443", "token": "token"},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("protocol=https\nhost=gitlab.example\n\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    assert (
        h.main(["--server", "https://server", "--host-id", "host", "--host-token", "launch"]) == 0
    )
    assert out.getvalue() == ""


def test_configure_host_gitlab_scopes_broker_to_connected_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch")
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *args: {"connected": True, "host": "https://gitlab.example"},
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **kwargs: calls.append(args))

    assert h.configure_host_gitlab("https://server", "host") is True
    key = "credential.https://gitlab.example.helper"
    assert calls[0] == ["git", "config", "--global", "--replace-all", key, ""]
    assert calls[1][:5] == ["git", "config", "--global", "--add", key]
    assert "git_credential_gitlab" in calls[1][-1]


def test_configure_host_gitlab_leaves_existing_helpers_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch")
    monkeypatch.setattr(h, "_fetch", lambda *args: {"connected": False})
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **kwargs: calls.append(args))

    assert h.configure_host_gitlab("https://server", "host") is False
    assert calls == []


def test_configure_host_glab_merges_hosts_and_writes_private_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    config_dir = tmp_path / "glab"
    config_dir.mkdir()
    (config_dir / "hosts.yml").write_text("other.example:\n  token: keep\n", encoding="utf-8")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch")
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *args: {
            "connected": True,
            "host": "https://gitlab.example",
            "token": "gitlab-token",
            "login": "alice",
        },
    )

    assert h.configure_host_glab("https://server", "host") is True
    hosts_path = config_dir / "hosts.yml"
    hosts = yaml.safe_load(hosts_path.read_text(encoding="utf-8"))
    assert hosts["other.example"]["token"] == "keep"
    assert hosts["gitlab.example"] == {
        "token": "gitlab-token",
        "user": "alice",
        "git_protocol": "https",
    }
    assert hosts_path.stat().st_mode & 0o777 == 0o600


def test_glab_refresh_interval_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(h._GLAB_REFRESH_INTERVAL_ENV_VAR, "0")
    assert h._glab_refresh_interval_s() == 0
    assert h.start_host_glab_refresh("https://server", "host") is None


def test_local_host_uses_isolated_glab_config_without_touching_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "omnigent"))
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(tmp_path / "personal-glab"))
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch")
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *args: {
            "connected": True,
            "host": "https://gitlab.example",
            "token": "t",
            "login": "alice",
        },
    )

    assert h.configure_host_gitlab("https://server", "host") is False
    assert h.configure_host_glab("https://server", "host") is True
    expected_dir = tmp_path / "omnigent" / "hosts" / "host" / "glab-cli"
    assert h.os.environ["GLAB_CONFIG_DIR"] == str(expected_dir)
    hosts = yaml.safe_load((expected_dir / "hosts.yml").read_text())
    assert hosts["gitlab.example"]["user"] == "alice"
    assert not (tmp_path / "personal-glab" / "hosts.yml").exists()
    assert expected_dir.stat().st_mode & 0o777 == 0o700
    assert (expected_dir / "hosts.yml").stat().st_mode & 0o777 == 0o600


def test_local_glab_setup_requires_host_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    assert h.configure_host_glab("https://server", "host") is False
    assert "GLAB_CONFIG_DIR" not in h.os.environ
