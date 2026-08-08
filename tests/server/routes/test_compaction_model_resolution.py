"""Unit tests for compaction LLM-model resolution.

Regression: server-side ``/compact`` used to resolve its summarization model
only from the agent spec (``spec.llm`` / ``spec.executor.model``) and ignore
``conv.model_override``. For agents that pin no model in their spec (``polly``,
``debby``, provider-default subscription agents) that made ``/compact`` — the
only recovery from a context-window overflow ("prompt is too long") —
permanently unavailable, bricking the session. ``model_override`` (set by a
``/model`` switch) must be honored, matching the turn and pricing paths.
"""

from __future__ import annotations

from types import SimpleNamespace

from omnigent.server.routes.sessions import _resolve_compaction_llm_config
from omnigent.spec.types import LLMConfig


def _spec(llm: LLMConfig | None, executor_model: str | None, executor_connection=None):
    return SimpleNamespace(
        llm=llm,
        executor=SimpleNamespace(model=executor_model, connection=executor_connection),
    )


def _conv(model_override: str | None):
    return SimpleNamespace(model_override=model_override)


def test_override_used_when_spec_pins_no_model() -> None:
    """A ``/model`` override drives compaction for a spec-less-model agent
    (e.g. polly), reusing the executor connection."""
    spec = _spec(llm=None, executor_model=None, executor_connection={"api_key": "k"})
    cfg = _resolve_compaction_llm_config(spec, _conv("claude-sonnet-5[1m]"))
    assert cfg is not None
    assert cfg.model == "claude-sonnet-5[1m]"
    assert cfg.connection == {"api_key": "k"}


def test_override_wins_over_spec_llm() -> None:
    """The session-level override takes precedence over the spec's llm.model,
    reusing the spec llm's connection."""
    spec = _spec(
        llm=LLMConfig(model="anthropic/claude-sonnet-4", connection={"base_url": "u"}),
        executor_model=None,
    )
    cfg = _resolve_compaction_llm_config(spec, _conv("claude-opus-4-8[1m]"))
    assert cfg.model == "claude-opus-4-8[1m]"
    assert cfg.connection == {"base_url": "u"}


def test_spec_llm_used_when_no_override() -> None:
    """With no override, the agent's declared llm block is returned as-is."""
    llm = LLMConfig(model="anthropic/claude-sonnet-4", connection={"base_url": "u"})
    cfg = _resolve_compaction_llm_config(_spec(llm=llm, executor_model=None), _conv(None))
    assert cfg is llm


def test_executor_model_fallback() -> None:
    """No override and no spec.llm → executor.model + executor.connection."""
    spec = _spec(llm=None, executor_model="openai/gpt-5.4", executor_connection={"k": "v"})
    cfg = _resolve_compaction_llm_config(spec, _conv(None))
    assert cfg.model == "openai/gpt-5.4"
    assert cfg.connection == {"k": "v"}


def test_none_when_nothing_configured() -> None:
    """No override, no spec.llm, no executor.model → None (caller raises)."""
    assert _resolve_compaction_llm_config(_spec(None, None), _conv(None)) is None


def test_empty_override_is_ignored() -> None:
    """An empty-string override is falsy and must not shadow the spec."""
    llm = LLMConfig(model="anthropic/claude-sonnet-4")
    assert _resolve_compaction_llm_config(_spec(llm=llm, executor_model=None), _conv("")) is llm
