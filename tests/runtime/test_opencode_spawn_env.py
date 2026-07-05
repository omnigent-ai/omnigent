"""
Tests for the opencode harness spawn-env builder in
``omnigent/runtime/workflow.py``.

Covers the minimal contracts:
- ``HARNESS_OPENCODE_MODEL`` and ``HARNESS_OPENCODE_CWD`` from spec model +
  workdir.
- ``HARNESS_OPENCODE_GATEWAY_API_KEY`` from an explicit ``executor.auth``
  ``api_key`` — the shortest end-to-end proof that the auth branch fires.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_opencode_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    ExecutorSpec,
    LLMConfig,
    ProviderAuth,
)


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir so ambient global
    config (auth:, providers:) cannot leak into the builder's resolution.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABRICKS_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def _spec(
    *,
    model: str | None = None,
    auth: ApiKeyAuth | ProviderAuth | None = None,
) -> AgentSpec:
    """
    Build a minimal AgentSpec for the opencode harness.

    :param model: Optional model string, e.g. ``"anthropic/claude-sonnet-4-5"``.
    :param auth: Optional executor auth.
    :returns: A populated AgentSpec.
    """
    config: dict[str, object] = {"harness": "opencode"}
    if model is not None:
        config["model"] = model
    return AgentSpec(
        spec_version=1,
        name="oc",
        instructions="hi",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_model_and_cwd(tmp_path: Path) -> None:
    """HARNESS_OPENCODE_MODEL and HARNESS_OPENCODE_CWD are set from spec."""
    env = _build_opencode_spawn_env(
        _spec(model="anthropic/claude-sonnet-4-5"),
        workdir=tmp_path,
    )
    assert env["HARNESS_OPENCODE_MODEL"] == "anthropic/claude-sonnet-4-5"
    assert env["HARNESS_OPENCODE_CWD"] == str(tmp_path)


def test_api_key_auth_bakes_gateway_key() -> None:
    """An explicit api_key auth sets HARNESS_OPENCODE_GATEWAY_API_KEY."""
    env = _build_opencode_spawn_env(
        _spec(auth=ApiKeyAuth(api_key="sk-xyz")),
    )
    assert env["HARNESS_OPENCODE_GATEWAY_API_KEY"] == "sk-xyz"


def test_opencode_family_provider_sets_account_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A named provider with an ``opencode:`` family delivers the account key.

    OpenCode Zen / Go credentials route as ``HARNESS_OPENCODE_API_KEY`` (the
    executor exports it as ``OPENCODE_API_KEY``), NOT through the
    ``HARNESS_OPENCODE_GATEWAY_*`` provider override — the key authenticates
    OpenCode's own gateway, which needs no baseURL/apiKey config rewrite. The
    family's ``models.default`` also becomes the spawn model when the spec
    pins none.
    """
    monkeypatch.setenv("OC_GO_KEY", "oc-secret")
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  opencode-go:\n"
        "    kind: key\n"
        "    opencode:\n"
        "      api_key_ref: env:OC_GO_KEY\n"
        "      models:\n"
        "        default: opencode-go/kimi-k2.7-code\n",
        encoding="utf-8",
    )
    env = _build_opencode_spawn_env(_spec(auth=ProviderAuth(name="opencode-go")))
    assert env["HARNESS_OPENCODE_API_KEY"] == "oc-secret"
    assert env["HARNESS_OPENCODE_MODEL"] == "opencode-go/kimi-k2.7-code"
    assert "HARNESS_OPENCODE_GATEWAY_API_KEY" not in env
    assert "HARNESS_OPENCODE_GATEWAY_BASE_URL" not in env


def test_opencode_family_preferred_over_anthropic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider carrying both opencode and anthropic families routes opencode.

    The opencode family is OpenCode's native credential, so it outranks the
    gateway-override families in the dispatch order.
    """
    monkeypatch.setenv("OC_KEY", "oc-secret")
    monkeypatch.setenv("ANTH_KEY", "sk-ant")
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  both:\n"
        "    kind: key\n"
        "    opencode:\n"
        "      api_key_ref: env:OC_KEY\n"
        "    anthropic:\n"
        "      base_url: https://api.anthropic.com\n"
        "      api_key_ref: env:ANTH_KEY\n",
        encoding="utf-8",
    )
    env = _build_opencode_spawn_env(_spec(auth=ProviderAuth(name="both")))
    assert env["HARNESS_OPENCODE_API_KEY"] == "oc-secret"
    assert "HARNESS_OPENCODE_GATEWAY_API_KEY" not in env
