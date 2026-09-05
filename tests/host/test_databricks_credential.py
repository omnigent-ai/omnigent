"""Tests for the host-side Databricks credential materializer."""

from __future__ import annotations

import configparser
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


def test_writes_profile_and_sets_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _patch_get(
        monkeypatch,
        _Resp(
            200, {"connected": True, "token": "dbx-tok", "workspace_host": "https://ws.example/"}
        ),
    )
    assert dc.configure_host_databricks("https://omni.example/", "host-1") is True
    # Broker hit with the host token header, correct URL.
    assert seen["url"] == "https://omni.example/v1/hosts/host-1/credentials/databricks"
    assert seen["headers"]["X-Omnigent-Host-Token"] == "host-tok"
    # Profile written with trailing slash stripped from the host.
    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / ".databrickscfg")
    assert cfg["omnigent"]["host"] == "https://ws.example"
    assert cfg["omnigent"]["token"] == "dbx-tok"
    import os

    assert os.environ["DATABRICKS_CONFIG_PROFILE"] == "omnigent"


def test_preserves_existing_profiles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / ".databrickscfg"
    cfg_path.write_text("[other]\nhost = https://keep.example\ntoken = keep\n")
    _patch_get(
        monkeypatch,
        _Resp(200, {"connected": True, "token": "t", "workspace_host": "https://ws.example"}),
    )
    assert dc.configure_host_databricks("https://omni.example", "h") is True
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    assert cfg["other"]["host"] == "https://keep.example"  # untouched
    assert cfg["omnigent"]["host"] == "https://ws.example"


def test_noop_when_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Resp(200, {"connected": False}))
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    import os

    assert "DATABRICKS_CONFIG_PROFILE" not in os.environ


def test_noop_without_host_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    called = _patch_get(monkeypatch, _Resp(200, {"connected": True, "token": "t"}))
    assert dc.configure_host_databricks("https://omni.example", "h") is False
    assert called == {}  # broker never hit


def test_noop_on_broker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, httpx.ConnectError("down"))
    assert dc.configure_host_databricks("https://omni.example", "h") is False


def test_noop_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, _Resp(404, None))
    assert dc.configure_host_databricks("https://omni.example", "h") is False
