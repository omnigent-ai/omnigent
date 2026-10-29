"""Catalog-only process checks against the installed Pi model resolver."""

from __future__ import annotations

import os
import shutil

import pytest

from omnigent.harnesses.pi_native.model_scope import ScopableModel, scope_models

pytestmark = pytest.mark.skipif(
    shutil.which(os.environ.get("OMNIGENT_PI_PATH", "pi")) is None or shutil.which("node") is None,
    reason="requires the installed Pi resolver and Node",
)

_SONNET = ScopableModel("anthropic", "claude-sonnet-4-5", "Claude Sonnet 4.5")
_OPUS = ScopableModel("anthropic", "claude-opus-4-6", "Claude Opus 4.6")
_DATED_OPUS = ScopableModel("anthropic", "claude-opus-4-6-20260115", "Claude Opus 4.6 (dated)")
_GPT = ScopableModel("openai", "gpt-5.2", "GPT-5.2")
_ROUTED_JAMBA = ScopableModel("openrouter", "ai21/jamba-large-1.7", "AI21: Jamba Large 1.7")
_ROUTED_GLM = ScopableModel("openrouter", "z-ai/glm-5.3:batch", "Z.ai: GLM 5.3 (batch)")

_CATALOG = [_SONNET, _OPUS, _DATED_OPUS, _GPT, _ROUTED_JAMBA, _ROUTED_GLM]


@pytest.mark.parametrize(
    ("patterns", "expected"),
    [
        (["anthropic/claude-sonnet-4-5"], [_SONNET]),
        (["gpt-5.2"], [_GPT]),
        (["openrouter/ai21/jamba-large-1.7"], [_ROUTED_JAMBA]),
        (["openrouter/**"], [_ROUTED_JAMBA, _ROUTED_GLM]),
        (["anthropic/claude-{sonnet,opus}-*"], [_SONNET, _OPUS, _DATED_OPUS]),
        (["anthropic/claude-@(sonnet|opus)-*"], [_SONNET, _OPUS, _DATED_OPUS]),
        (["anthropic/claude-{sonnet,opus}-*:high"], [_SONNET, _OPUS, _DATED_OPUS]),
        (["openai/gpt-5.2", "*sonnet*", "gpt-5.2"], [_GPT, _SONNET]),
        (["mistral/devstral-large"], []),
    ],
)
def test_installed_resolver_contract(patterns: list[str], expected: list[ScopableModel]) -> None:
    assert scope_models(patterns, _CATALOG) == expected
