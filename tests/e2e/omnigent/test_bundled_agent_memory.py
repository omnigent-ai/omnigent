"""The bundled Polly & Debby agents ship with Hindsight long-term memory.

Pure spec-load + config checks — no LLM, no credentials. Guards two things:

1. Each bundle declares the three Hindsight memory builtins, pinned to a stable
   bank (``polly`` / ``debby``).
2. Memory is safe on a base install: the builtins bake NO ``${...}`` secret, so
   the client-side env expansion (``omnigent run``) never crashes when
   ``HINDSIGHT_API_KEY`` is unset. (Breaker we deliberately avoid — see the
   hindsight env-var fallback.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.cli import _expand_config_env_vars
from omnigent.spec import expand_env_vars, load
from omnigent.spec.types import AgentSpec

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HINDSIGHT_TOOLS = {"hindsight_retain", "hindsight_recall", "hindsight_reflect"}


@pytest.fixture(scope="module", params=["polly", "debby"])
def bundle(request: pytest.FixtureRequest) -> tuple[str, AgentSpec, dict]:
    name = request.param
    root = _REPO_ROOT / "examples" / name
    spec = load(root)
    raw = yaml.safe_load((root / "config.yaml").read_text())
    return name, spec, raw


def test_declares_the_three_memory_tools(bundle: tuple[str, AgentSpec, dict]) -> None:
    name, spec, _ = bundle
    builtins = {b.name for b in spec.tools.builtins}
    assert builtins >= _HINDSIGHT_TOOLS, (
        f"{name} is missing memory tools: {_HINDSIGHT_TOOLS - builtins}"
    )


def test_memory_pinned_to_stable_bank(bundle: tuple[str, AgentSpec, dict]) -> None:
    name, spec, _ = bundle
    for b in spec.tools.builtins:
        if b.name in _HINDSIGHT_TOOLS:
            assert (b.config or {}).get("bank_id") == name, f"{b.name} should pin bank_id={name!r}"


def test_orchestration_still_intact(bundle: tuple[str, AgentSpec, dict]) -> None:
    # Adding memory must not disturb the sub-agent roster.
    name, spec, _ = bundle
    expected = {"polly": ["claude_code", "codex", "pi"], "debby": ["claude", "gpt"]}[name]
    assert sorted(spec.tools.agents) == expected


def test_no_baked_secret_in_builtins(bundle: tuple[str, AgentSpec, dict]) -> None:
    # No ${VAR} anywhere in the memory tool configs — the key comes from the env
    # at runtime, so an unset key can never crash spec parsing / env expansion.
    _, spec, _ = bundle
    for b in spec.tools.builtins:
        for key, value in (b.config or {}).items():
            assert "${" not in value and "$" not in value, (
                f"{b.name}.{key} bakes an env ref: {value!r}"
            )


def test_run_safe_when_key_unset(bundle: tuple[str, AgentSpec, dict], monkeypatch) -> None:
    # Reproduce the client-side expansion `omnigent run` does, with the key unset.
    # It must NOT raise (an unset ${HINDSIGHT_API_KEY} would — but we bake none).
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
    _, _, raw = bundle
    _expand_config_env_vars(raw, expand_env_vars)  # should not raise
