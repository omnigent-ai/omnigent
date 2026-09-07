"""Tests for GenieExecutor and the genie harness registration.

Genie is Databricks' fork of the codex CLI; :class:`GenieExecutor` is a thin
:class:`CodexExecutor` subclass that only swaps the binary (``genie``) and the
on-disk home (``GENIE_HOME`` / ``~/.genie``). These tests pin those two seams
and the gateway-config inheritance, plus the ``genie`` harness registry entry.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.genie_executor import (
    GENIE_HOME_DIR_NAME,
    GENIE_HOME_ENV_VAR,
    GenieExecutor,
    _find_genie_cli,
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
        ex = _genie_executor(
            gateway=True,
            databricks_profile="eng-ml-agent-platform",
            base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
            gateway_auth_command="printf token",
            model="databricks-gpt-5-4-mini",
        )
        joined = "\n".join(ex._codex_config_overrides)
        assert "/ai-gateway/codex/v1" in joined
        assert 'model="databricks-gpt-5-4-mini"' in joined


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
