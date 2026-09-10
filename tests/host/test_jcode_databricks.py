"""Tests for jcode Databricks managed-connect gateway wiring (session-private config)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tomllib

from omnigent.host import databricks_credential as dc
from omnigent.host import jcode_databricks as jd
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Model a managed sandbox host (IS_SANDBOX baked into the image).
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "host-tok")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / ".databrickscfg"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    # Per-session jcode homes are created under the harness tmp parent → tmp_path.
    monkeypatch.setenv("OMNIGENT_HARNESS_TMP_PARENT", str(tmp_path))
    # Pin the model so _jcode_default_model doesn't depend on the bundled catalog.
    monkeypatch.setenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", "system.ai.claude-sonnet-4-6")


def _write_sidecar(tmp_path: Path, workspace_host: str = "https://ws.example") -> None:
    """Write the broker sidecar (the managed-connect signal)."""
    sidecar_path = tmp_path / dc._SIDECAR_NAME
    sidecar_path.write_text(
        json.dumps(
            {
                "server": "https://omni.example",
                "host_id": "host-1",
                "host_token": "host-tok",
                "workspace_host": workspace_host,
            }
        )
    )
    os.chmod(sidecar_path, 0o600)


def _mock_broker(
    monkeypatch: pytest.MonkeyPatch, *, workspace: str, bearer: str = "fresh-bearer"
) -> Mock:
    m = Mock(return_value=(workspace, bearer))
    monkeypatch.setattr("omnigent.host.jcode_databricks.fetch_broker_bearer", m)
    return m


class TestConnectJcodeGatewayEnv:
    def test_returns_none_without_sidecar(self, tmp_path: Path) -> None:
        """No broker sidecar → complete no-op (not a managed-connect host)."""
        assert jd.connect_jcode_gateway_env(session_id="s") is None

    def test_writes_session_private_config_and_returns_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """On a connect host: a session-private JCODE_HOME with a config.toml pinning the
        dbx gateway provider, a per-session runtime dir, and the bearer — with base_url
        set by Omnigent (not read from a shared file) and the bearer never on disk."""
        _write_sidecar(tmp_path)
        _mock_broker(monkeypatch, workspace="https://ws.example", bearer="tok-123")

        result = jd.connect_jcode_gateway_env(session_id="sess-abc")
        assert result is not None
        assert result["JCODE_DBX_TOKEN"] == "tok-123"

        home = Path(result["JCODE_HOME"])
        assert home.exists() and str(tmp_path) in str(home)
        assert "/omnigent-jcode-run/" in str(home)
        assert "sess-abc" not in str(home)  # raw session id never in the path
        # Runtime dir is under the private home.
        assert result["JCODE_RUNTIME_DIR"] == str(home / "run")
        assert Path(result["JCODE_RUNTIME_DIR"]).exists()

        # Config pins the dbx provider at the workspace openai gateway, by construction.
        cfg = tomllib.loads((home / "config.toml").read_text())
        assert cfg["provider"]["default_provider"] == "dbx"
        dbx = cfg["providers"]["dbx"]
        assert dbx["base_url"] == "https://ws.example/ai-gateway/openai/v1"
        assert dbx["type"] == "openai-compatible"
        assert dbx["api_key_env"] == "JCODE_DBX_TOKEN"
        # The bearer VALUE must never be persisted — only the env-var name.
        assert "tok-123" not in (home / "config.toml").read_text()

    def test_home_0700_and_config_0600(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_sidecar(tmp_path)
        _mock_broker(monkeypatch, workspace="https://ws.example")
        result = jd.connect_jcode_gateway_env(session_id="s")
        assert result is not None
        home = Path(result["JCODE_HOME"])
        assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(home / "config.toml").st_mode) == 0o600

    def test_same_session_reuses_home_new_session_differs_and_always_mints(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_sidecar(tmp_path)
        fetch = _mock_broker(monkeypatch, workspace="https://ws.example")
        a1 = jd.connect_jcode_gateway_env(session_id="A")
        a2 = jd.connect_jcode_gateway_env(session_id="A")
        b1 = jd.connect_jcode_gateway_env(session_id="B")
        assert a1 and a2 and b1
        assert a1["JCODE_HOME"] == a2["JCODE_HOME"]  # same session → same home
        assert b1["JCODE_HOME"] != a1["JCODE_HOME"]  # different session → different
        assert fetch.call_count == 3  # bearer minted every spawn

    def test_returns_none_when_broker_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_sidecar(tmp_path)
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer",
            Mock(side_effect=Exception("broker down")),
        )
        assert jd.connect_jcode_gateway_env(session_id="s") is None

    def test_returns_none_when_broker_declines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_sidecar(tmp_path)
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer", Mock(return_value=None)
        )
        assert jd.connect_jcode_gateway_env(session_id="s") is None

    def test_returns_none_on_workspace_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Reconnect guard: broker vends a different workspace than the sidecar pins."""
        _write_sidecar(tmp_path, workspace_host="https://ws.example")
        _mock_broker(monkeypatch, workspace="https://other.example")
        assert jd.connect_jcode_gateway_env(session_id="s") is None

    def test_malicious_session_id_stays_within_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A traversal-shaped session id can't escape the run-dir root (hashed leaf)."""
        _write_sidecar(tmp_path)
        _mock_broker(monkeypatch, workspace="https://ws.example")
        result = jd.connect_jcode_gateway_env(session_id="../../etc/evil")
        assert result is not None
        root = os.path.realpath(str(tmp_path / "omnigent-jcode-run"))
        assert os.path.realpath(result["JCODE_HOME"]).startswith(root + os.sep)


class TestDefaultModel:
    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _write_sidecar(tmp_path)
        _mock_broker(monkeypatch, workspace="https://ws.example")
        monkeypatch.setenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", "databricks-claude-sonnet-4-6")
        result = jd.connect_jcode_gateway_env(session_id="s")
        assert result is not None
        cfg = tomllib.loads((Path(result["JCODE_HOME"]) / "config.toml").read_text())
        assert cfg["providers"]["dbx"]["default_model"] == "databricks-claude-sonnet-4-6"

    def test_falls_back_to_catalog_without_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _write_sidecar(tmp_path)
        _mock_broker(monkeypatch, workspace="https://ws.example")
        monkeypatch.delenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", raising=False)
        monkeypatch.setattr(
            "omnigent.models.model_catalog.resolve_catalog_model",
            lambda *a, **k: SimpleNamespace(model_id="databricks-claude-from-catalog"),
        )
        result = jd.connect_jcode_gateway_env(session_id="s")
        assert result is not None
        cfg = tomllib.loads((Path(result["JCODE_HOME"]) / "config.toml").read_text())
        assert cfg["providers"]["dbx"]["default_model"] == "databricks-claude-from-catalog"
