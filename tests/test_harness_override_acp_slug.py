"""Regression tests for namespaced ACP harness overrides."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.spec.types import AgentSpec, ExecutorSpec


def _fake_agent() -> SimpleNamespace:
    return SimpleNamespace(
        id="agent-1",
        name="pi-bundle",
        bundle_location="mem://bundle",
        session_id=None,
    )


def _fake_spec(executor_type: str = "omnigent") -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="pi-bundle",
        executor=ExecutorSpec(type=executor_type, config={"harness": "pi"}),
    )


def _acp_entries(*slugs: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(slug=s) for s in slugs]


def _patch_agent_cache(monkeypatch: pytest.MonkeyPatch, executor_type: str = "omnigent") -> None:
    class _Cache:
        def load(self, *_a, **_k):
            return SimpleNamespace(spec=_fake_spec(executor_type))

    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: _Cache())


def test_validator_returns_namespaced_value_for_configured_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes._sessions import helpers

    _patch_agent_cache(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents",
        lambda *a, **k: _acp_entries("gemini-cli", "goose"),
    )

    out = helpers._validated_harness_override("acp:goose", _fake_agent())
    assert out == "acp:goose", "the namespaced override must be preserved for persistence"


def test_validator_rejects_unknown_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.errors import OmnigentError
    from omnigent.server.routes._sessions import helpers

    _patch_agent_cache(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents",
        lambda *a, **k: _acp_entries("gemini-cli"),
    )

    with pytest.raises(OmnigentError, match="unknown acp agent slug"):
        helpers._validated_harness_override("acp:goose", _fake_agent())


def test_validator_keeps_executor_type_gate_for_namespaced_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.errors import OmnigentError
    from omnigent.server.routes._sessions import helpers

    _patch_agent_cache(monkeypatch, executor_type="container")
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents",
        lambda *a, **k: _acp_entries("goose"),
    )

    with pytest.raises(OmnigentError, match=r"executor\.type"):
        helpers._validated_harness_override("acp:goose", _fake_agent())


def test_spawn_env_builder_hands_slug_to_acp_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner import app as runner_app

    seen: dict[str, object] = {}

    def _fake_acp_builder(spec, *, harness=None, cwd=None, workdir=None):
        seen["harness"] = harness
        return {"HARNESS_ACP_COMMAND": "x"}

    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_acp_spawn_env",
        _fake_acp_builder,
    )

    spec = _fake_spec()

    env = runner_app._build_spawn_env_from_spec(
        spec,
        "acp:goose",
        cwd=None,
        workdir=None,
    )
    assert env is not None
    assert seen.get("harness") == "acp:goose", "the acp builder must see the namespaced harness"


def test_acp_builder_selects_agent_from_harness_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runtime import workflow

    gemini = SimpleNamespace(
        slug="fake-gemini",
        name="Fake Gemini",
        command="gemini-cmd",
        model=None,
        session_id_mode="server",
        send_model=False,
        omnigent_mcp=False,
        inject_system_prompt=False,
        env_passthrough=[],
    )
    goose = SimpleNamespace(
        slug="fake-goose",
        name="Fake Goose",
        command="goose-cmd",
        model=None,
        session_id_mode="server",
        send_model=False,
        omnigent_mcp=False,
        inject_system_prompt=False,
        env_passthrough=[],
    )
    monkeypatch.setattr("omnigent.onboarding.acp_auth.acp_agents", lambda *a, **k: [gemini, goose])
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.resolve_acp_agent",
        lambda slug, *a, **k: {"fake-gemini": gemini, "fake-goose": goose}.get(slug),
    )

    env = workflow._build_acp_spawn_env(_fake_spec(), harness="acp:fake-goose")
    assert env["HARNESS_ACP_COMMAND"] == "goose-cmd", (
        "the harness kwarg must select the named agent, not the first configured one"
    )
