"""Tests for GenieExecutor and the genie harness registration.

Genie is Databricks' fork of the codex CLI; :class:`GenieExecutor` is a thin
:class:`CodexExecutor` subclass that swaps the binary (``genie``), the on-disk
home (``GENIE_HOME`` / ``~/.genie``), moves ``-c`` overrides into the private
``config.toml`` (genie denies ``-c``), and maps Databricks endpoint names to
genie catalog slugs. These tests pin those seams, the gateway-config
inheritance, the brokered-auth differences, plus the ``genie`` harness
registry entry.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from packaging.version import Version

from omnigent.inner.codex_executor import (
    CodexExecutor,
    _CodexAppServerSession,
    _databricks_codex_config_overrides,
    _require_brokered_codex_version,
    write_codex_config_overrides,
)
from omnigent.inner.genie_executor import (
    GENIE_HOME_DIR_NAME,
    GENIE_HOME_ENV_VAR,
    GenieExecutor,
    _find_genie_cli,
    genie_model_id,
)


def _genie_executor(**overrides: object) -> GenieExecutor:
    """Build a GenieExecutor with the genie binary resolution stubbed."""
    with patch(
        "omnigent.inner.genie_executor._find_genie_cli",
        return_value="/usr/bin/genie",
    ):
        return GenieExecutor(**overrides)  # type: ignore[arg-type]


class TestGenieExecutor:
    def test_is_codex_executor_subclass(self) -> None:
        # The whole design: reuse the codex app-server driver, don't fork it.
        assert issubclass(GenieExecutor, CodexExecutor)

    def test_forces_genie_home(self) -> None:
        ex = _genie_executor()
        assert ex._home_env_var == GENIE_HOME_ENV_VAR == "GENIE_HOME"
        assert ex._home_dir_name == GENIE_HOME_DIR_NAME == ".genie"

    def test_home_override_cannot_be_smuggled_in(self) -> None:
        # Even if a caller passes codex's home, genie forces its own.
        ex = _genie_executor(home_env_var="CODEX_HOME", home_dir_name=".codex")
        assert ex._home_env_var == "GENIE_HOME"
        assert ex._home_dir_name == ".genie"

    def test_resolves_genie_binary(self) -> None:
        with patch(
            "omnigent.inner.genie_executor._find_genie_cli",
            return_value="/opt/genie",
        ):
            ex = GenieExecutor()
        assert ex._codex_path == "/opt/genie"

    def test_missing_binary_raises_with_genie_hint(self) -> None:
        with patch("omnigent.inner.genie_executor._find_genie_cli", return_value=None):
            with pytest.raises(ImportError, match="OMNIGENT_GENIE_PATH"):
                GenieExecutor()

    def test_find_genie_cli_uses_genie_name_and_env(self) -> None:
        with patch("omnigent.inner.genie_executor.resolve_cli_binary") as resolve:
            resolve.return_value = "/usr/bin/genie"
            assert _find_genie_cli() == "/usr/bin/genie"
        resolve.assert_called_once_with("genie", env_var="OMNIGENT_GENIE_PATH")

    def test_inherits_databricks_gateway_config(self) -> None:
        # Genie routes through the same Databricks Codex Responses gateway
        # (/ai-gateway/codex/v1) as codex; the gateway -c overrides come from
        # the shared CodexExecutor path unchanged.
        # An explicit gateway host skips the ~/.databrickscfg profile lookup,
        # so the test does not depend on the machine's Databricks config.
        ex = _genie_executor(
            gateway=True,
            gateway_host="https://example.databricks.com",
            base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
            gateway_auth_command="printf token",
            model="databricks-gpt-5-4-mini",
        )
        joined = "\n".join(ex._codex_config_overrides)
        assert "/ai-gateway/codex/v1" in joined
        # Endpoint name is mapped to genie's catalog slug.
        assert 'model="gpt-5.4-mini"' in joined

    def test_gateway_without_profile_or_host_fails_loud(self) -> None:
        # Codex would derive the workspace from the default ~/.databrickscfg
        # profile, routing genie through whichever workspace that is.
        with pytest.raises(OSError, match="requires a Databricks profile or gateway host"):
            _genie_executor(gateway=True, model="databricks-gpt-5-4-mini")

    def test_brokered_session_skips_bundled_catalog_probe(self) -> None:
        # Genie has no ``debug models`` command; its catalog is compiled in.
        genie = _genie_executor()
        codex = CodexExecutor(codex_path="/usr/bin/codex")
        assert genie._app_session_factory.keywords["brokered_catalog_probe"] is False
        assert codex._app_session_factory.keywords["brokered_catalog_probe"] is True

    @pytest.mark.parametrize(
        ("banner", "accepted"),
        [
            ("genie 0.2.0", True),
            ("genie 0.3.1", True),
            ("genie 0.1.9", False),
            ("genie 0.2.0-alpha.8", False),
            ("codex-cli 0.163.0", False),
        ],
    )
    async def test_brokered_version_floor_reads_the_genie_banner(
        self, tmp_path: Path, banner: str, accepted: bool
    ) -> None:
        cli = tmp_path / "genie"
        cli.write_text(f"#!/bin/sh\necho '{banner}'\n")
        cli.chmod(0o755)
        check = _require_brokered_codex_version(
            str(cli),
            banner=GenieExecutor._brokered_version_banner,
            minimum=GenieExecutor._brokered_min_version,
            cli_name=GenieExecutor._brokered_cli_name,
        )
        if accepted:
            await check
        else:
            with pytest.raises(RuntimeError, match="Genie wire version"):
                await check
        assert GenieExecutor._brokered_min_version == Version("0.2.0")

    def test_writes_overrides_to_config_instead_of_argv(self) -> None:
        ex = _genie_executor()
        assert ex._config_overrides_in_file is True

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("databricks-gpt-5-4-mini", "gpt-5.4-mini"),
            ("databricks-gpt-5-6-luna", "gpt-5.6-luna"),
            ("databricks-gpt-5-5", "gpt-5.5"),
            ("databricks-gpt-5-2", "gpt-5.2"),
            ("gpt-5.4-mini", "gpt-5.4-mini"),
            ("databricks-gpt-5-4-nano", "gpt-5.4-nano"),
            ("databricks-gpt-5-2-codex", "gpt-5.2-codex"),
            ("databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-6"),
            ("gpt-x", "gpt-x"),
        ],
    )
    def test_genie_model_id(self, model: str, expected: str) -> None:
        # Mirrors genie's databricks_codex_model_id; genie silently swaps a
        # model missing from its catalog for the provider default.
        assert genie_model_id(model) == expected


class _FakePipe:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        return None

    async def readline(self) -> bytes:
        return b""

    async def read(self, n: int) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self.returncode: int | None = None
        self.pid = 12345

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


async def _start_session(
    tmp_path: Path, **session_kwargs: Any
) -> tuple[list[str], dict[str, Any]]:
    """Start an app-server session against a fake process; return argv + config."""
    captured_argv: list[str] = []
    captured_env: dict[str, str] = {}

    async def _fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _FakeProcess:
        captured_argv.extend(args)
        captured_env.update(kwargs["env"])
        return _FakeProcess()

    source_home = tmp_path / "source-home"
    source_home.mkdir()
    session = _CodexAppServerSession(
        cwd=str(tmp_path), env={}, tool_executor=None, **session_kwargs
    )
    session._request = AsyncMock(return_value={"result": {}})
    with (
        patch(
            "omnigent.inner.codex_executor._create_subprocess_exec",
            new=_fake_create_subprocess_exec,
        ),
        patch(
            "omnigent.inner.codex_executor._codex_home_config_source_from_env",
            return_value=source_home,
        ),
    ):
        await session.start()
        home = Path(captured_env[session._home_env_var])
        config_path = home / "config.toml"
        config = (
            tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        )
        await session.close()
    return captured_argv, config


class TestConfigOverridesInFile:
    async def test_genie_session_launches_bare_app_server(self, tmp_path: Path) -> None:
        overrides = _databricks_codex_config_overrides(
            model="gpt-5.4-mini",
            base_url="https://example.databricks.com/ai-gateway/codex/v1",
            auth_command="printf token",
        )
        argv, config = await _start_session(
            tmp_path,
            codex_path="/usr/bin/genie",
            codex_config_overrides=[*overrides, 'web_search="disabled"'],
            home_env_var=GENIE_HOME_ENV_VAR,
            home_dir_name=GENIE_HOME_DIR_NAME,
            config_overrides_in_file=True,
        )
        # genie's CLI allowlist denies every app-server flag, including -c.
        assert argv == ["/usr/bin/genie", "app-server"]
        assert config["model"] == "gpt-5.4-mini"
        assert config["model_provider"] == "omnigent_databricks"
        assert config["model_supports_reasoning_summaries"] is True
        assert config["web_search"] == "disabled"
        assert (
            "/ai-gateway/codex/v1" in config["model_providers"]["omnigent_databricks"]["base_url"]
        )

    async def test_codex_session_still_passes_c_flags(self, tmp_path: Path) -> None:
        argv, config = await _start_session(
            tmp_path,
            codex_path="/usr/bin/codex",
            codex_config_overrides=['model="gpt-5.4-mini"'],
        )
        assert argv == ["/usr/bin/codex", "app-server", "-c", 'model="gpt-5.4-mini"']
        assert "model" not in config


class TestWriteCodexConfigOverrides:
    def test_parses_toml_values_and_dotted_keys(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text('model = "old"\n[features]\nkeep = true\n')
        write_codex_config_overrides(
            tmp_path,
            ['model="new"', "features.unified_exec=false", "web_search=disabled"],
        )
        config = tomllib.loads((tmp_path / "config.toml").read_text())
        assert config["model"] == "new"
        assert config["features"] == {"keep": True, "unified_exec": False}
        # Unparseable TOML falls back to a literal string, like ``-c``.
        assert config["web_search"] == "disabled"

    def test_model_id_toml_breakout_stays_inert(self, tmp_path: Path) -> None:
        payload = 'x",auth={command="sh",args=["-c","touch /tmp/pwned"]},wire_api="responses"}'
        overrides = _databricks_codex_config_overrides(
            model=payload,
            base_url="https://example.databricks.com/ai-gateway/codex/v1",
            auth_command="printf token",
        )
        write_codex_config_overrides(tmp_path, [o for o in overrides if o.startswith("model=")])
        config = tomllib.loads((tmp_path / "config.toml").read_text())
        assert config == {"model": payload}

    def test_rejects_override_without_equals(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="key=value"):
            write_codex_config_overrides(tmp_path, ["model"])


class TestGenieRegistration:
    def test_registered_as_valid_harness(self) -> None:
        from omnigent import harness_plugins as hp

        assert "genie" in hp.valid_harnesses()
        assert hp.harness_modules()["genie"] == "omnigent.inner.genie_harness"
        assert hp.model_env_keys()["genie"] == "HARNESS_GENIE_MODEL"

    def test_consumes_openai_family(self) -> None:
        from omnigent.onboarding.provider_config import _HARNESS_FAMILY, OPENAI_FAMILY

        assert _HARNESS_FAMILY["genie"] == OPENAI_FAMILY

    def test_workflow_gateway_and_profile_maps(self) -> None:
        from omnigent.runtime import workflow as w

        assert w._HARNESS_GATEWAY_FLAG["genie"] == "HARNESS_GENIE_GATEWAY"
        assert w._HARNESS_DATABRICKS_PROFILE["genie"] == "HARNESS_GENIE_DATABRICKS_PROFILE"
        assert w._PROVIDER_HARNESS_FAMILY["genie"] == "openai"
        assert "genie" in w._UCODE_HARNESS_CONFIGS
