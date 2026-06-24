"""Tests for the optional OpenCode worker in the debby spec.

polly intentionally ships **no** OpenCode worker: shipping a sub-agent whose
``opencode-native`` harness older clients don't recognize made every old
runner/host fail to launch *any* polly (the version-skew incident behind
omnigent-ai/omnigent#1145). polly was reverted to its three-worker roster
(claude_code / codex / pi) and the negative test below guards that. debby keeps
the optional OpenCode perspective (default fanout is still claude + gpt).
"""

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


def test_polly_does_not_declare_opencode_worker() -> None:
    """polly stays opencode-free, so an older client can load it without skew.

    Re-adding an ``opencode`` sub-agent (or any ``opencode-native`` harness, e.g.
    a codex ``allowed_harnesses`` opt-in) would reintroduce the harness that
    broke old runners on spec validation. If OpenCode is wanted back, it must
    land with a server/runner floor that guarantees clients recognize it.
    """
    subs = _sub_agents("polly")
    assert "opencode" not in subs
    config_text = (_REPO_ROOT / "examples" / "polly" / "config.yaml").read_text(encoding="utf-8")
    assert "opencode" not in config_text.lower()
    # No sub-agent re-introduces opencode-native via a harness override either.
    for sub in subs.values():
        assert "opencode-native" not in (_config(sub).get("allowed_harnesses") or [])


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
