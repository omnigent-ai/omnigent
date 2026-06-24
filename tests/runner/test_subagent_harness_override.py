"""Tests for the allowlisted ``args.harness`` sub-agent override helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.runner.tool_dispatch import (
    _subagent_allowed_harnesses,
    _subagent_harness_override_from_args,
)


def test_harness_override_absent_returns_none() -> None:
    assert _subagent_harness_override_from_args({"args": {"input": "hi"}}) is None
    assert _subagent_harness_override_from_args({"args": "plain string"}) is None


def test_harness_override_extracted() -> None:
    args = {"args": {"input": "hi", "harness": "opencode-native"}}
    assert _subagent_harness_override_from_args(args) == "opencode-native"


def test_harness_override_non_string_raises() -> None:
    with pytest.raises(ValueError, match="harness"):
        _subagent_harness_override_from_args({"args": {"input": "hi", "harness": 5}})


def _spec_with(allowed: object) -> SimpleNamespace:
    config = {"harness": "codex-native"}
    if allowed is not None:
        config["allowed_harnesses"] = allowed
    sub = SimpleNamespace(name="codex", executor=SimpleNamespace(config=config))
    return SimpleNamespace(sub_agents=[sub])


def test_allowed_harnesses_reads_and_canonicalizes() -> None:
    spec = _spec_with(["codex-native", "native-opencode"])
    allowed = _subagent_allowed_harnesses("codex", spec)
    # native-opencode canonicalizes to opencode-native.
    assert allowed == frozenset({"codex-native", "opencode-native"})


def test_allowed_harnesses_empty_when_undeclared() -> None:
    assert _subagent_allowed_harnesses("codex", _spec_with(None)) == frozenset()


def test_allowed_harnesses_empty_for_unknown_subagent() -> None:
    assert _subagent_allowed_harnesses("nope", _spec_with(["codex-native"])) == frozenset()


def test_allowed_harnesses_no_spec() -> None:
    assert _subagent_allowed_harnesses("codex", None) == frozenset()
