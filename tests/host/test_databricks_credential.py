"""Tests for the host-side Databricks credential materializer."""

from __future__ import annotations

import configparser
import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from omnigent.host import databricks_credential as dc
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


class _Resp:
    def __init__(self, status_code: int, payload: dict | None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # These tests model a managed sandbox host, where IS_SANDBOX=1 is baked into
    # the image; see test_noop_off_sandbox for the local-host (unset) no-op.
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "host-tok")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / ".databrickscfg"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)


def _patch_get(monkeypatch: pytest.MonkeyPatch, resp_or_exc) -> dict:
    seen: dict = {}

    def fake_get(url: str, headers: dict, timeout: float):
        seen["url"] = url
        seen["headers"] = headers
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr(dc.httpx, "get", fake_get)
    return seen


def _connected(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch the broker to return a connected credential; return the seen dict."""
    return _patch_get(
        monkeypatch,
        _Resp(
            200, {"connected": True, "token": "dbx-tok", "workspace_host": "https://ws.example/"}
        ),
    )


def test_writes_host_only_profile_and_sets_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example/", "host-1") is True
    # Broker hit with the host token header, generic provider-keyed URL.
    assert seen["url"] == "https://omni.example/v1/hosts/host-1/credentials/databricks"
    assert seen["headers"]["X-Omnigent-Host-Token"] == "host-tok"
    # Profile written with only the host (trailing slash stripped) — no token.
    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / ".databrickscfg")
    assert cfg["omnigent"]["host"] == "https://ws.example"
    assert "token" not in cfg["omnigent"]
    assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "omnigent"


def test_bearer_is_never_persisted_to_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # The broker returned "dbx-tok"; it must appear nowhere on disk.
    assert "dbx-tok" not in (tmp_path / ".databrickscfg").read_text()
    assert "dbx-tok" not in (tmp_path / dc._SIDECAR_NAME).read_text()


def test_sidecar_records_broker_coords_privately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "host-1") is True
    sidecar = tmp_path / dc._SIDECAR_NAME
    assert json.loads(sidecar.read_text()) == {
        "server": "https://omni.example",
        "host_id": "host-1",
        "host_token": "host-tok",
        "workspace_host": "https://ws.example",
    }
    assert stat.S_IMODE(os.stat(sidecar).st_mode) == 0o600


def test_preserves_existing_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / ".databrickscfg"
    cfg_path.write_text("[other]\nhost = https://keep.example\ntoken = keep\n")
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    assert cfg["other"]["host"] == "https://keep.example"  # untouched
    assert cfg["other"]["token"] == "keep"
    assert cfg["omnigent"]["host"] == "https://ws.example"


def test_replaces_stale_token_from_older_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An older build wrote a static token; the new write must drop it.
    cfg_path = tmp_path / ".databrickscfg"
    cfg_path.write_text("[omnigent]\nhost = https://old.example\ntoken = stale\n")
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    assert cfg["omnigent"]["host"] == "https://ws.example"
    assert "token" not in cfg["omnigent"]
    assert "stale" not in cfg_path.read_text()


def test_noop_when_not_connected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_get(monkeypatch, _Resp(200, {"connected": False}))
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    assert "DATABRICKS_CONFIG_PROFILE" not in os.environ
    assert not (tmp_path / dc._SIDECAR_NAME).exists()


def test_noop_without_host_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    called = _patch_get(monkeypatch, _Resp(200, {"connected": True, "token": "t"}))
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    assert called == {}  # broker never hit


def test_noop_off_sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Local-host safety: a connected owner running ``omnigent host`` on a laptop
    (no IS_SANDBOX) must not get their real ~/.databrickscfg written. This is an
    auto-applied sandbox integration — it no-ops entirely off a managed sandbox,
    even with a valid host token and a connected broker, and never hits the broker."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    called = _patch_get(
        monkeypatch,
        _Resp(200, {"connected": True, "token": "t", "workspace_host": "https://ws.example"}),
    )
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    assert called == {}  # broker never hit — pure no-op
    assert not (tmp_path / ".databrickscfg").exists()  # no local footprint
    assert not (tmp_path / dc._SIDECAR_NAME).exists()
    assert "DATABRICKS_CONFIG_PROFILE" not in os.environ


def test_noop_on_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, httpx.ConnectError("down"))
    assert dc.configure_host_databricks("https://omni.example", "h") is False


def test_noop_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Resp(404, None))
    assert dc.configure_host_databricks("https://omni.example", "h") is False


def test_broker_token_command_none_without_sidecar() -> None:
    assert dc.broker_token_command("https://ws.example") is None


def test_broker_token_command_bakes_sidecar_path_for_the_connected_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # Trailing-slash-insensitive match against the connected workspace host.
    cmd = dc.broker_token_command("https://ws.example/")
    assert cmd is not None
    assert "python3 -m omnigent.host.databricks_credential token" in cmd
    assert str(tmp_path / dc._SIDECAR_NAME) in cmd


def test_broker_token_command_none_for_a_different_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # A gateway pinned to another workspace must not borrow the broker token.
    assert dc.broker_token_command("https://other.example") is None


def test_main_prints_broker_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out.strip() == "dbx-tok"


def test_main_uses_explicit_coords_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    sidecar = tmp_path / dc._SIDECAR_NAME
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["token", "--coords", str(sidecar)]) == 0
    assert capsys.readouterr().out.strip() == "dbx-tok"


def test_main_silent_without_sidecar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = _connected(monkeypatch)
    assert dc.main(["token"]) == 0  # no sidecar on disk
    assert capsys.readouterr().out == ""
    assert called == {}  # never reached the broker


def test_main_silent_when_broker_declines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    # Owner later disconnects: broker now returns not-connected.
    _patch_get(monkeypatch, _Resp(200, {"connected": False}))
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["token"]) == 0
    assert capsys.readouterr().out == ""


def test_main_ignores_unknown_operation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _connected(monkeypatch)
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    capsys.readouterr()  # discard configure-time log output
    assert dc.main(["store"]) == 0
    assert capsys.readouterr().out == ""
