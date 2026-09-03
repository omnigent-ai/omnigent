"""Namespaced acp:<slug> harness overrides survive to spawn selection (#4855).

A session created with harness_override="acp:goose" on a non-ACP bundle used
to persist bare "acp" (the validator canonicalized), and the runner's spawn
fell back to the FIRST configured agent — the wrong one. The validator now
returns the namespaced value (validating the slug against the configured
acp: agents), and _build_spawn_env_from_spec threads the slug into
executor.config for _build_acp_spawn_env.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omnigent.server.schemas import ErrorDetail  # noqa: F401 — schemas import sanity


def _fake_agent() -> SimpleNamespace:
    """The minimal Agent stand-in _validated_harness_override needs."""
    return SimpleNamespace(
        id="agent-1",
        name="pi-bundle",
        bundle_location="mem://bundle",
        session_id=None,
    )


def _fake_spec() -> SimpleNamespace:
    """An omnigent-executor spec whose config carries NO acp harness."""
    return SimpleNamespace(
        executor=SimpleNamespace(type="omnigent", config={"harness": "pi"}),
        model_copy=None,  # spec is not copied in the validator path
    )


def _acp_entries(*slugs: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(slug=s) for s in slugs]


class _NoCopySpec(SimpleNamespace):
    """Spec wrapper that satisfies the validator's hasattr probes."""

    pass


def test_validator_returns_namespaced_value_for_configured_slug(monkeypatch) -> None:
    from omnigent.server.routes._sessions import helpers

    agent = _fake_agent()

    class _Cache:
        def load(self, *_a, **_k):  # noqa: ANN001
            return SimpleNamespace(spec=_fake_spec())

    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: _Cache(), raising=False)
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents",
        lambda *a, **k: _acp_entries("gemini-cli", "goose"),
    )

    out = helpers._validated_harness_override("acp:goose", agent)
    assert out == "acp:goose", "the namespaced override must be preserved for persistence"


def test_validator_rejects_unknown_slug(monkeypatch) -> None:
    from omnigent.server.routes._sessions import helpers
    from omnigent.errors import OmnigentError

    agent = _fake_agent()

    class _Cache:
        def load(self, *_a, **_k):  # noqa: ANN001
            return SimpleNamespace(spec=_fake_spec())

    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: _Cache(), raising=False)
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents",
        lambda *a, **k: _acp_entries("gemini-cli"),
    )

    with pytest.raises(OmnigentError, match="unknown acp agent slug"):
        helpers._validated_harness_override("acp:goose", agent)


def test_spawn_env_builder_threads_slug_into_acp_config(monkeypatch) -> None:
    """The builder injects acp:<slug> into executor.config so
    _build_acp_spawn_env resolves the requested agent instead of the first."""
    from omnigent.runner import app as runner_app

    seen: dict[str, object] = {}

    def _fake_acp_builder(spec, *, cwd=None, workdir=None):  # noqa: ANN001
        cfg = getattr(spec.executor, "config", None) or {}
        seen["harness"] = cfg.get("harness")
        return {"HARNESS_ACP_COMMAND": "x"}

    monkeypatch.setattr(
        "omnigent.runtime.workflow._build_acp_spawn_env",
        _fake_acp_builder,
        raising=False,
    )

    spec = SimpleNamespace(
        executor=SimpleNamespace(type="omnigent", config={"harness": "pi"}),
    )
    spec.model_copy = lambda *, update: SimpleNamespace(
        executor=SimpleNamespace(type="omnigent", config=update["executor"].__dict__.get("config", {}))
        if not isinstance(update["executor"], SimpleNamespace)
        else update["executor"]
    )
    spec.executor.model_copy = lambda *, update: SimpleNamespace(
        type=spec.executor.type, config={**spec.executor.config, **update.get("config", {})}
    )

    env = runner_app._build_spawn_env_from_spec(
        spec,
        "acp:goose",
        cwd=None,
        workdir=None,
    )
    assert env is not None
    assert seen.get("harness") == "acp:goose", "the acp builder must see the namespaced harness"
