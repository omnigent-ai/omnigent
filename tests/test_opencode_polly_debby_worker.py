"""Guards that the shipped example agents ship **no** OpenCode worker.

Both polly and debby once declared an optional ``opencode`` sub-agent
(``harness: opencode-native``). Shipping a sub-agent whose harness older
clients don't recognize made every old runner/host fail to launch the agent at
all — the version-skew incident behind omnigent-ai/omnigent#1145. Both were
reverted to their original rosters (polly: claude_code / codex / pi; debby:
claude / gpt), and the negative tests below guard that OpenCode does not creep
back into either shipped spec.
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


def test_debby_does_not_declare_opencode_head() -> None:
    """debby stays opencode-free, so an older client can load it without skew.

    debby is reverted to its two-head roster (claude + gpt). Re-adding an
    ``opencode`` head (or any ``opencode-native`` harness override) would
    reintroduce the harness that broke old clients on spec validation.
    """
    subs = _sub_agents("debby")
    assert "opencode" not in subs
    assert {"claude", "gpt"} <= set(subs)
    config_text = (_REPO_ROOT / "examples" / "debby" / "config.yaml").read_text(encoding="utf-8")
    assert "opencode" not in config_text.lower()
    # No head re-introduces opencode-native via a harness override either.
    for sub in subs.values():
        assert "opencode-native" not in (_config(sub).get("allowed_harnesses") or [])
