"""Tests for jcode Databricks gateway configuration."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.host import databricks_credential as dc
from omnigent.host import jcode_databricks as jd
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # These tests model a managed sandbox host, where IS_SANDBOX=1 is baked into
    # the image and the host token is present.
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "host-tok")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(tmp_path / ".databrickscfg"))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    # Route per-session jcode runtime dirs under tmp_path (not the real /tmp).
    monkeypatch.setenv("OMNIGENT_HARNESS_TMP_PARENT", str(tmp_path))


def _write_profile_and_sidecar(tmp_path: Path) -> None:
    """Helper to write a host profile and broker sidecar."""
    cfg_path = tmp_path / ".databrickscfg"
    cfg_path.write_text("[omnigent]\nhost = https://ws.example\n")
    sidecar_path = tmp_path / dc._SIDECAR_NAME
    sidecar_data = {
        "server": "https://omni.example",
        "host_id": "host-1",
        "host_token": "host-tok",
        "workspace_host": "https://ws.example",
    }
    sidecar_path.write_text(json.dumps(sidecar_data))
    os.chmod(sidecar_path, 0o600)


class TestBuildJcodeConfigureCommand:
    """Tests for build_jcode_configure_command."""

    def test_builds_openai_compatible_provider_command(self) -> None:
        """The command configures jcode's openai-compatible provider with Databricks gateway."""
        cmd = jd.build_jcode_configure_command(
            ["/usr/bin/jcode"],
            host="https://ws.example.databricks.com",
            model="system.ai.claude-sonnet-4-6",
        )
        assert cmd == [
            "/usr/bin/jcode",
            "provider",
            "add",
            "dbx",
            "--base-url",
            "https://ws.example.databricks.com/ai-gateway/openai/v1",
            "--model",
            "system.ai.claude-sonnet-4-6",
            "--auth",
            "bearer",
            "--api-key-env",
            "JCODE_DBX_TOKEN",
            "--set-default",
            "--overwrite",
            "--quiet",
        ]

    def test_strips_trailing_slash_from_host(self) -> None:
        """A trailing slash on the host is stripped before constructing base_url."""
        cmd = jd.build_jcode_configure_command(
            ["jcode"],
            host="https://ws.example/",
            model="system.ai.claude-sonnet-4-6",
        )
        assert "--base-url" in cmd
        idx = cmd.index("--base-url")
        assert cmd[idx + 1] == "https://ws.example/ai-gateway/openai/v1"

    def test_raises_when_host_is_empty(self) -> None:
        """Raises ValueError when host is empty or whitespace."""
        with pytest.raises(ValueError, match="host and model must not be empty"):
            jd.build_jcode_configure_command(
                ["jcode"], host="", model="system.ai.claude-sonnet-4-6"
            )

    def test_raises_when_model_is_empty(self) -> None:
        """Raises ValueError when model is empty or whitespace."""
        with pytest.raises(ValueError, match="host and model must not be empty"):
            jd.build_jcode_configure_command(["jcode"], host="https://ws.example", model="")


class TestConnectJcodeGatewayEnv:
    """Tests for connect_jcode_gateway_env."""

    def test_returns_none_without_sidecar(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when no sidecar is present (not a managed-connect host)."""
        result = jd.connect_jcode_gateway_env()
        assert result is None

    def test_returns_bearer_and_runtime_dir_with_sidecar(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns a dict with JCODE_DBX_TOKEN and JCODE_RUNTIME_DIR when sidecar is present."""
        _write_profile_and_sidecar(tmp_path)

        # Monkeypatch fetch_broker_bearer to return a fresh bearer.
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer",
            Mock(return_value=("https://ws.example", "fresh-bearer-token")),
        )

        result = jd.connect_jcode_gateway_env(session_id="sess-abc")
        assert result is not None
        assert result["JCODE_DBX_TOKEN"] == "fresh-bearer-token"
        assert "JCODE_RUNTIME_DIR" in result
        # Runtime dir exists, sits under the harness tmp parent, and is keyed by session.
        runtime_dir = result["JCODE_RUNTIME_DIR"]
        assert Path(runtime_dir).exists()
        assert str(tmp_path) in runtime_dir
        assert runtime_dir.endswith("omnigent-jcode-run/sess-abc")

    def test_same_session_reuses_dir_new_session_differs_and_always_mints(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A session reuses one runtime dir across spawns (no per-turn leak); a
        different session gets its own dir; and the bearer is minted on every call
        (so a re-spawn after the jcode daemon idle-exits re-authenticates)."""
        _write_profile_and_sidecar(tmp_path)
        fetch = Mock(return_value=("https://ws.example", "bearer"))
        monkeypatch.setattr("omnigent.host.jcode_databricks.fetch_broker_bearer", fetch)

        a1 = jd.connect_jcode_gateway_env(session_id="A")
        a2 = jd.connect_jcode_gateway_env(session_id="A")
        b1 = jd.connect_jcode_gateway_env(session_id="B")
        assert a1 is not None and a2 is not None and b1 is not None
        # Same session → same dir (idempotent, no accumulation); different session differs.
        assert a1["JCODE_RUNTIME_DIR"] == a2["JCODE_RUNTIME_DIR"]
        assert b1["JCODE_RUNTIME_DIR"] != a1["JCODE_RUNTIME_DIR"]
        # The broker is called on every spawn — refresh stays correct for re-spawns.
        assert fetch.call_count == 3

    def test_runtime_dir_has_0700_permissions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The created runtime dir has 0700 permissions (owner only)."""
        _write_profile_and_sidecar(tmp_path)
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer",
            Mock(return_value=("https://ws.example", "bearer")),
        )

        result = jd.connect_jcode_gateway_env()
        assert result is not None
        runtime_dir = result["JCODE_RUNTIME_DIR"]
        assert stat.S_IMODE(os.stat(runtime_dir).st_mode) == 0o700

    def test_returns_none_when_broker_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when the broker is unreachable or raises an exception."""
        _write_profile_and_sidecar(tmp_path)
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer",
            Mock(side_effect=Exception("broker down")),
        )

        result = jd.connect_jcode_gateway_env()
        assert result is None

    def test_returns_none_when_broker_declines(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None when the broker returns None (owner not connected)."""
        _write_profile_and_sidecar(tmp_path)
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer",
            Mock(return_value=None),
        )

        result = jd.connect_jcode_gateway_env()
        assert result is None

    def test_returns_none_on_runtime_dir_creation_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Returns None if the runtime dir cannot be created."""
        _write_profile_and_sidecar(tmp_path)
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer",
            Mock(return_value=("https://ws.example", "bearer")),
        )
        # Monkeypatch os.makedirs (used by _session_runtime_dir) to raise.
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.os.makedirs",
            Mock(side_effect=OSError("permission denied")),
        )

        result = jd.connect_jcode_gateway_env(session_id="sess-x")
        assert result is None

    def test_returns_none_on_workspace_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Reconnect guard: if the broker vends a workspace different from the sidecar's
        pinned workspace (owner reconnected elsewhere), the bearer is withheld."""
        _write_profile_and_sidecar(tmp_path)  # sidecar pins https://ws.example
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.fetch_broker_bearer",
            Mock(return_value=("https://other.example", "bearer-for-other-ws")),
        )

        result = jd.connect_jcode_gateway_env()
        assert result is None


class TestConfigureJcodeForSandbox:
    """Tests for configure_jcode_for_sandbox (daemon thread behavior)."""

    def test_noop_without_managed_connect_signals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No sidecar/profile → the gate fails before building or running anything."""
        # No sidecar written, so the gate is not satisfied.
        build_spy = Mock()
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.build_jcode_configure_command", build_spy
        )
        jd.configure_jcode_for_sandbox()
        build_spy.assert_not_called()

    def test_noop_when_jcode_not_found(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """jcode binary absent → no configure command is built or run."""
        _write_profile_and_sidecar(tmp_path)
        monkeypatch.setattr("omnigent.host.jcode_databricks.shutil.which", Mock(return_value=None))
        build_spy = Mock()
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.build_jcode_configure_command", build_spy
        )

        jd.configure_jcode_for_sandbox()
        build_spy.assert_not_called()

    def test_spawns_configure_thread_when_ready(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """With all gates satisfied, the configure command is built for the connected
        workspace's openai gateway (resolved synchronously, before the daemon thread)."""
        _write_profile_and_sidecar(tmp_path)  # workspace https://ws.example
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.shutil.which",
            Mock(return_value="/usr/bin/jcode"),
        )
        original_build = jd.build_jcode_configure_command

        def capture_build(*args, **kwargs):
            capture_build.argv = original_build(*args, **kwargs)
            return capture_build.argv

        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.build_jcode_configure_command", capture_build
        )
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.subprocess.run",
            Mock(return_value=Mock(return_value=Mock(returncode=0))),
        )

        jd.configure_jcode_for_sandbox()

        assert "provider" in capture_build.argv and "add" in capture_build.argv
        assert "https://ws.example/ai-gateway/openai/v1" in capture_build.argv

    def test_uses_env_override_for_model(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Uses OMNIGENT_DATABRICKS_GATEWAY_MODEL_ENV override if set."""
        _write_profile_and_sidecar(tmp_path)
        monkeypatch.setenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", "system.ai.claude-opus-4-6")

        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.shutil.which",
            Mock(return_value="/usr/bin/jcode"),
        )

        # Mock build_jcode_configure_command to capture the call.
        original_build = jd.build_jcode_configure_command

        def capture_build(*args, **kwargs):
            capture_build.last_model = kwargs.get("model")
            return original_build(*args, **kwargs)

        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.build_jcode_configure_command", capture_build
        )
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.subprocess.run", Mock(return_value=Mock(returncode=0))
        )

        jd.configure_jcode_for_sandbox()

        # The model is resolved synchronously (before the daemon thread starts), so the
        # capture is populated by the time configure_jcode_for_sandbox returns.
        assert capture_build.last_model == "system.ai.claude-opus-4-6"

    def test_env_override_ignored_when_not_system_ai(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A non-``system.ai.*`` gateway-model override (e.g. a ``databricks-*``
        serving-endpoint id for opencode) is ignored — jcode's openai path serves the
        ``system.ai`` namespace, so the served default is used instead."""
        _write_profile_and_sidecar(tmp_path)
        monkeypatch.setenv("OMNIGENT_DATABRICKS_GATEWAY_MODEL", "databricks-kimi-k3")
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.shutil.which",
            Mock(return_value="/usr/bin/jcode"),
        )
        original_build = jd.build_jcode_configure_command

        def capture_build(*args, **kwargs):
            capture_build.last_model = kwargs.get("model")
            return original_build(*args, **kwargs)

        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.build_jcode_configure_command", capture_build
        )
        monkeypatch.setattr(
            "omnigent.host.jcode_databricks.subprocess.run", Mock(return_value=Mock(returncode=0))
        )

        jd.configure_jcode_for_sandbox()

        assert capture_build.last_model == jd._JCODE_DATABRICKS_DEFAULT_MODEL
