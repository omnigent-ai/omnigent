"""Tests for Polly's host-readiness preflight."""

from __future__ import annotations

from pathlib import Path

import yaml

_POLLY = Path(__file__).resolve().parents[2] / "examples" / "polly"


def _polly_prompt() -> str:
    cfg = yaml.safe_load((_POLLY / "config.yaml").read_text(encoding="utf-8"))
    return cfg.get("prompt", "")


def test_polly_preflight_references_host_readiness() -> None:
    prompt = _polly_prompt()
    assert "configured_harnesses" in prompt
    assert "sys_session_get_info" in prompt


def test_polly_preflight_uses_readiness_true_filter() -> None:
    prompt = _polly_prompt()
    lowered = prompt.lower()
    assert "configured_harnesses" in prompt
    assert "exactly `true`" in lowered
    for state in ("needs-auth", "binary-missing", "version-too-low"):
        assert state in lowered


def test_polly_preflight_readiness_map_is_primary_gate() -> None:
    prompt = _polly_prompt()
    readiness_pos = prompt.index("configured_harnesses")
    command_v_pos = prompt.index("command -v")
    assert readiness_pos < command_v_pos

    window = " ".join(prompt[readiness_pos:command_v_pos].lower().split())
    assert any(marker in window for marker in ("absent", "null", "fall back"))


def test_polly_preflight_defines_no_host_fallback() -> None:
    prompt = " ".join(_polly_prompt().lower().split())
    assert "absent" in prompt or "null" in prompt
    assert "fall back" in prompt
