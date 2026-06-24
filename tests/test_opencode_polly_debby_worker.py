"""Tests for the optional OpenCode worker in polly and debby specs."""

from __future__ import annotations

from pathlib import Path

from omnigent.spec import load

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _sub_agents(bundle: str) -> dict[str, object]:
    spec = load(_REPO_ROOT / "examples" / bundle)
    return {sa.name: sa for sa in (getattr(spec, "sub_agents", None) or [])}


def _config(sub_agent: object) -> dict[str, object]:
    executor = getattr(sub_agent, "executor", None)
    config = getattr(executor, "config", None)
    if isinstance(config, dict):
        return config
    return {}


def test_polly_declares_opencode_worker() -> None:
    subs = _sub_agents("polly")
    assert "opencode" in subs
    cfg = _config(subs["opencode"])
    assert cfg.get("harness") == "opencode-native"


def test_polly_codex_worker_allowlists_opencode_override() -> None:
    subs = _sub_agents("polly")
    cfg = _config(subs["codex"])
    allowed = cfg.get("allowed_harnesses")
    assert allowed is not None
    assert "opencode-native" in allowed
    assert "codex-native" in allowed


def test_polly_prompt_preflight_probes_opencode() -> None:
    config = (_REPO_ROOT / "examples" / "polly" / "config.yaml").read_text(encoding="utf-8")
    assert "command -v claude codex pi opencode" in config


def test_debby_declares_opencode_perspective() -> None:
    subs = _sub_agents("debby")
    assert "opencode" in subs
    cfg = _config(subs["opencode"])
    assert cfg.get("harness") == "opencode-native"
    # Default fanout is still the two heads.
    assert "claude" in subs
    assert "gpt" in subs


def test_debby_prompt_keeps_opencode_optional() -> None:
    config = (_REPO_ROOT / "examples" / "debby" / "config.yaml").read_text(encoding="utf-8")
    assert "Optional OpenCode perspective" in config
    assert "do not dispatch" in config.lower()
