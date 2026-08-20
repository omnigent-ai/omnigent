"""
Tests for server-level LLM configuration for policy functions.

Covers:

- :class:`PolicyLLMClient` construction and ``create()`` delegation.
- :class:`EvaluationContext` carries ``llm_client`` field.
- :class:`PolicyEngine` injects ``llm_client`` into the context.
- :func:`_build_event` exposes ``llm_client`` in the event dict.
- :func:`build_policy_engine` constructs a :class:`PolicyLLMClient`
  from ``server_llm``.
- :class:`RuntimeCaps` accepts ``llm`` field.
- :func:`parse_server_llm` delegates to ``_parse_llm``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from omnigent.policies.function import FunctionPolicy, _build_event
from omnigent.policies.types import EvaluationContext, PolicyLLMClient
from omnigent.runtime.caps import RuntimeCaps
from omnigent.runtime.policies.builder import (
    _build_policy_llm_client,
    _resolve_server_llm_connection,
    build_policy_engine,
)
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec import parse_server_llm
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    LLMConfig,
    Phase,
    PhaseSelector,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_server_llm() -> LLMConfig:
    """
    Build a realistic server-level LLM config for tests.

    :returns: A :class:`LLMConfig` with model, connection, and
        timeout fields populated.
    """
    return LLMConfig(
        model="openai/gpt-4o-mini",
        connection={"api_key": "test-key", "base_url": "https://example.com"},
        request_timeout=60,
    )


class _FakeResponsesNamespace:
    """
    Stub for ``Client.responses`` that records calls.

    :param response: The value to return from ``create()``.
    """

    def __init__(self, response: Any) -> None:
        self._response = response
        self.create = AsyncMock(return_value=response)


class _FakeClient:
    """
    Stub LLM client that records ``responses.create()`` calls.

    Does not use MagicMock — attributes are explicit so any
    unexpected access raises ``AttributeError`` loudly.

    :param response: The fixed value ``responses.create()``
        returns.
    """

    def __init__(self, response: Any = "fake-response") -> None:
        self.responses = _FakeResponsesNamespace(response)


def _llm_capturing_policy(bucket: dict[str, Any]) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` that records ``event["llm_client"]``
    into *bucket* for assertion.

    :param bucket: Dict to write the captured client into under
        key ``"llm_client"``.
    :returns: A capturing :class:`FunctionPolicy`.
    """

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        bucket["llm_client"] = event["llm_client"]
        return {"result": "ALLOW"}

    spec = FunctionPolicySpec(
        name="capture_llm",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test.not.used"),
    )
    return FunctionPolicy(spec, _evaluate)


# ── RuntimeCaps.llm field ────────────────────────────────────────────────────


def test_runtime_caps_llm_defaults_to_none() -> None:
    """
    RuntimeCaps with no args has ``llm=None``.

    What breaks if this fails: the caps dataclass changed its
    default, which would cause a ``PolicyLLMClient`` to be built
    even when the server config has no ``llm:`` block.
    """
    caps = RuntimeCaps()
    assert caps.llm is None


def test_runtime_caps_accepts_llm_config() -> None:
    """
    RuntimeCaps stores the provided LLMConfig on the ``llm``
    field.

    What breaks if this fails: the ``llm`` field is not wired
    into the dataclass, so the CLI can't pass it through.
    """
    llm = _make_server_llm()
    caps = RuntimeCaps(llm=llm)
    # Verify the exact object is stored, not a copy or None.
    assert caps.llm is llm
    assert caps.llm.model == "openai/gpt-4o-mini"


# ── parse_server_llm ────────────────────────────────────────────────────────


def test_parse_server_llm_none_returns_none() -> None:
    """
    ``parse_server_llm(None)`` returns ``None`` — the server
    config has no ``llm:`` block.

    What breaks if this fails: absent ``llm:`` key is
    misinterpreted as a present-but-empty block.
    """
    result = parse_server_llm(None)
    assert result is None


def test_parse_server_llm_parses_valid_block() -> None:
    """
    ``parse_server_llm`` delegates to ``_parse_llm`` and returns
    a populated :class:`LLMConfig`.

    What breaks if this fails: the public wrapper doesn't
    actually call the parser, so model/connection are lost.
    """
    raw = {
        "model": "openai/gpt-4o-mini",
        "connection": {"api_key": "sk-test"},
        "request_timeout": 45,
    }
    result = parse_server_llm(raw, expand_env=False)
    assert result is not None
    assert result.model == "openai/gpt-4o-mini"
    assert result.connection == {"api_key": "sk-test"}
    assert result.request_timeout == 45


def test_parse_server_llm_parses_fallback_models() -> None:
    """
    ``parse_server_llm`` parses a ``fallback_models:`` list into
    ``LLMConfig.fallback_models`` and keeps it out of ``extra``.

    What breaks if this fails: the fallback list is dropped or
    leaks into SDK kwargs, so hosted never picks up fallback.
    """
    raw = {
        "model": "databricks-claude-sonnet-4",
        "fallback_models": ["databricks-claude-3-5-haiku", "openai/gpt-4o-mini"],
    }
    result = parse_server_llm(raw, expand_env=False)
    assert result is not None
    assert result.fallback_models == [
        "databricks-claude-3-5-haiku",
        "openai/gpt-4o-mini",
    ]
    assert "fallback_models" not in result.extra


def test_parse_server_llm_fallback_models_defaults_empty() -> None:
    """
    Absent ``fallback_models:`` yields an empty list — the
    default preserves single-model behaviour.

    What breaks if this fails: configs without the key get a
    non-empty or ``None`` fallback list, breaking existing specs.
    """
    result = parse_server_llm({"model": "openai/gpt-4o-mini"}, expand_env=False)
    assert result is not None
    assert result.fallback_models == []


def test_parse_server_llm_fallback_models_non_list_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A non-list ``fallback_models:`` (e.g. a bare string typo) is
    rejected with a warning and yields an empty list.

    What breaks if this fails: a bare string is dropped silently
    (or iterated per-character), so the operator's intended
    fallback is lost with no hint.
    """
    with caplog.at_level("WARNING"):
        result = parse_server_llm(
            {"model": "openai/gpt-4o-mini", "fallback_models": "openai/gpt-4o"},
            expand_env=False,
        )

    assert result is not None
    assert result.fallback_models == []
    assert any("fallback_models must be a list" in r.message for r in caplog.records)


# ── PolicyLLMClient ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_llm_client_create_delegates_to_inner_client() -> None:
    """
    ``PolicyLLMClient.create()`` forwards to
    ``client.responses.create()`` with pre-bound model,
    connection, and timeout.

    What breaks if this fails: the wrapper doesn't call the
    underlying client, so policy LLM calls silently return
    nothing or raise.
    """
    fake_client = _FakeClient(response="test-llm-response")
    policy_client = PolicyLLMClient(
        _client=fake_client,
        _model="openai/gpt-4o-mini",
        _connection={"api_key": "sk-test"},
        _request_timeout=60,
    )

    result = await policy_client.create(
        input=[{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        instructions="Be helpful.",
    )

    # The wrapper returned what the inner client returned.
    assert result == "test-llm-response"

    # Verify the inner client was called with pre-bound params.
    fake_client.responses.create.assert_awaited_once()
    call_kwargs = fake_client.responses.create.call_args.kwargs
    assert call_kwargs["model"] == "openai/gpt-4o-mini"
    assert call_kwargs["connection_params"] == {"api_key": "sk-test"}
    assert call_kwargs["timeout"] == 60
    assert call_kwargs["instructions"] == "Be helpful."
    # Input was forwarded as-is.
    assert call_kwargs["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


@pytest.mark.asyncio
async def test_policy_llm_client_create_allows_overrides() -> None:
    """
    Callers can override ``model``, ``connection_params``, and
    ``timeout`` via kwargs.

    What breaks if this fails: policy callables cannot use a
    different model than the server default for specific calls.
    """
    fake_client = _FakeClient(response="overridden")
    policy_client = PolicyLLMClient(
        _client=fake_client,
        _model="openai/gpt-4o-mini",
        _connection={"api_key": "sk-test"},
        _request_timeout=60,
    )

    await policy_client.create(
        input=[{"role": "user", "content": "hello"}],
        model="openai/gpt-4o",
        connection_params={"api_key": "sk-override"},
        timeout=120,
    )

    call_kwargs = fake_client.responses.create.call_args.kwargs
    # Overrides take precedence over pre-bound values.
    assert call_kwargs["model"] == "openai/gpt-4o"
    assert call_kwargs["connection_params"] == {"api_key": "sk-override"}
    assert call_kwargs["timeout"] == 120


@pytest.mark.asyncio
async def test_policy_llm_client_no_fallback_propagates_error() -> None:
    """
    With no fallbacks configured, a primary-model failure
    propagates unchanged after a single attempt.

    What breaks if this fails: the fallback loop swallows or
    reshapes the primary error even when no fallback exists,
    changing today's fail-closed behaviour in ``prompt_policy``.
    """
    fake_client = _FakeClient()
    boom = RuntimeError("primary down")
    fake_client.responses.create = AsyncMock(side_effect=boom)
    policy_client = PolicyLLMClient(
        _client=fake_client,
        _model="openai/gpt-4o-mini",
        _connection=None,
        _request_timeout=60,
    )

    with pytest.raises(RuntimeError, match="primary down"):
        await policy_client.create(input=[{"role": "user", "content": "hi"}])

    # Exactly one attempt — the primary model, no retry.
    assert fake_client.responses.create.await_count == 1


@pytest.mark.asyncio
async def test_policy_llm_client_falls_back_on_primary_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    When the primary model fails, ``create()`` advances to the
    first fallback, logs the recovery, and returns its response.

    What breaks if this fails: a transient primary-model failure
    denies the request even though a healthy fallback was
    configured.
    """
    fake_client = _FakeClient()
    good = "fallback-response"
    fake_client.responses.create = AsyncMock(side_effect=[RuntimeError("primary down"), good])
    policy_client = PolicyLLMClient(
        _client=fake_client,
        _model="databricks/primary",
        _connection={"api_key": "k"},
        _request_timeout=60,
        _fallback_models=["databricks/backup"],
    )

    with caplog.at_level("WARNING"):
        result = await policy_client.create(input=[{"role": "user", "content": "hi"}])

    assert result == good
    assert fake_client.responses.create.await_count == 2
    # The recovery is logged so the fallback path is visible in ops logs.
    assert any("recovered on fallback model" in r.message for r in caplog.records)
    # Primary tried first, backup second — in order.
    models = [c.kwargs["model"] for c in fake_client.responses.create.await_args_list]
    assert models == ["databricks/primary", "databricks/backup"]
    # The shared connection/timeout are reused across candidates.
    for call in fake_client.responses.create.await_args_list:
        assert call.kwargs["connection_params"] == {"api_key": "k"}
        assert call.kwargs["timeout"] == 60


@pytest.mark.asyncio
async def test_policy_llm_client_all_models_fail_raises_last() -> None:
    """
    When the primary and every fallback fail, the last error is
    re-raised after all candidates are exhausted.

    What breaks if this fails: an exhausted fallback chain hides
    the failure or raises the wrong (stale) error, breaking the
    fail-closed contract in ``prompt_policy``.
    """
    fake_client = _FakeClient()
    fake_client.responses.create = AsyncMock(
        side_effect=[
            RuntimeError("primary"),
            RuntimeError("backup-1"),
            RuntimeError("backup-2"),
        ]
    )
    policy_client = PolicyLLMClient(
        _client=fake_client,
        _model="m0",
        _connection=None,
        _request_timeout=60,
        _fallback_models=["m1", "m2"],
    )

    with pytest.raises(RuntimeError, match="backup-2"):
        await policy_client.create(input=[{"role": "user", "content": "hi"}])

    assert fake_client.responses.create.await_count == 3


@pytest.mark.asyncio
async def test_policy_llm_client_explicit_model_override_skips_fallback() -> None:
    """
    An explicit ``model=`` override opts out of the fallback
    chain — only the requested model is tried, and its failure
    propagates.

    What breaks if this fails: a caller that deliberately targets
    one model silently gets the fallback chain instead.
    """
    fake_client = _FakeClient()
    fake_client.responses.create = AsyncMock(side_effect=RuntimeError("override down"))
    policy_client = PolicyLLMClient(
        _client=fake_client,
        _model="m0",
        _connection=None,
        _request_timeout=60,
        _fallback_models=["m1", "m2"],
    )

    with pytest.raises(RuntimeError, match="override down"):
        await policy_client.create(
            input=[{"role": "user", "content": "hi"}],
            model="explicit",
        )

    # Only the explicit model was tried — fallbacks were skipped.
    assert fake_client.responses.create.await_count == 1
    assert fake_client.responses.create.await_args.kwargs["model"] == "explicit"


# ── EvaluationContext.llm_client ────────────────────────────────────────────


def test_evaluation_context_llm_client_defaults_to_none() -> None:
    """
    ``EvaluationContext`` has ``llm_client=None`` by default.

    What breaks if this fails: the field doesn't exist or has a
    non-None default, which would inject a phantom client into
    test contexts.
    """
    ctx = EvaluationContext(phase=Phase.REQUEST, content="hello")
    assert ctx.llm_client is None


def test_evaluation_context_accepts_llm_client() -> None:
    """
    ``EvaluationContext`` accepts a ``llm_client`` value.

    What breaks if this fails: the field is not on the frozen
    dataclass, so ``replace(ctx, llm_client=...)`` would raise.
    """
    sentinel = object()
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content="hello",
        llm_client=sentinel,
    )
    assert ctx.llm_client is sentinel


# ── _build_event includes llm_client ────────────────────────────────────────


def test_build_event_includes_llm_client_none() -> None:
    """
    ``_build_event`` includes ``llm_client: None`` when the
    context has no LLM client.

    What breaks if this fails: the key is missing from the event
    dict, causing ``KeyError`` in policy callables that check
    ``event["llm_client"]``.
    """
    ctx = EvaluationContext(phase=Phase.REQUEST, content="hello")
    event = _build_event(ctx)
    assert "llm_client" in event
    assert event["llm_client"] is None


def test_build_event_includes_llm_client_object() -> None:
    """
    ``_build_event`` passes through the ``llm_client`` object
    from the context.

    What breaks if this fails: the LLM client is dropped or
    copied instead of passed through, so the policy callable
    gets a different object than the engine injected.
    """
    sentinel = object()
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content="hello",
        llm_client=sentinel,
    )
    event = _build_event(ctx)
    # Same object — not copied, since it's a shared client.
    assert event["llm_client"] is sentinel


# ── PolicyEngine injects llm_client ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_engine_injects_llm_client_into_policy(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The engine injects the ``llm_client`` from its constructor
    into ``event["llm_client"]`` for every policy evaluation.

    What breaks if this fails: function policies that need an
    LLM client (e.g. prompt-difficulty classifiers) receive
    ``None`` even when the server has ``llm:`` configured.
    """
    bucket: dict[str, Any] = {}
    sentinel = object()
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[_llm_capturing_policy(bucket)],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
        llm_client=sentinel,
    )

    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))

    # The policy callable received the exact llm_client the engine
    # was constructed with.
    assert bucket["llm_client"] is sentinel


@pytest.mark.asyncio
async def test_engine_injects_none_when_no_llm_client(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    When the engine has no ``llm_client`` (server has no ``llm:``
    config), ``event["llm_client"]`` is ``None``.

    What breaks if this fails: policy callables get a stale or
    garbage value instead of a clean ``None``.
    """
    bucket: dict[str, Any] = {}
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[_llm_capturing_policy(bucket)],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
        # llm_client defaults to None
    )

    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))

    assert bucket["llm_client"] is None


# ── _build_policy_llm_client ────────────────────────────────────────────────


def test_build_policy_llm_client_none_returns_none() -> None:
    """
    ``_build_policy_llm_client(None, None)`` returns ``None``.

    What breaks if this fails: a ``PolicyLLMClient`` is
    constructed from ``None`` config, which would crash.
    """
    result = _build_policy_llm_client(None, None)
    assert result is None


def test_build_policy_llm_client_constructs_from_config() -> None:
    """
    ``_build_policy_llm_client`` builds a :class:`PolicyLLMClient`
    with model, connection, and timeout from the config. The
    connection is resolved separately (by
    :func:`_resolve_server_llm_connection`) and passed in.

    What breaks if this fails: the builder doesn't construct the
    client or drops config fields, so policies can't call the LLM.
    """
    llm_config = _make_server_llm()
    connection = _resolve_server_llm_connection(llm_config)
    result = _build_policy_llm_client(llm_config, connection)

    assert result is not None
    assert isinstance(result, PolicyLLMClient)
    assert result._model == "openai/gpt-4o-mini"
    assert result._connection == {"api_key": "test-key", "base_url": "https://example.com"}
    assert result._request_timeout == 60


def test_build_policy_llm_client_normalizes_databricks_prefix() -> None:
    """
    Bare ``databricks-`` model names get the ``databricks/``
    provider prefix, on both the primary model and every fallback.

    What breaks if this fails: hosted fallback calls route to
    ``/responses`` on the Databricks gateway and 400 (the exact
    bug the primary-model fixup already guards against).
    """
    llm_config = LLMConfig(
        model="databricks-claude-sonnet-4",
        fallback_models=["databricks-claude-3-5-haiku", "openai/gpt-4o-mini"],
    )
    result = _build_policy_llm_client(llm_config, None)

    assert result is not None
    assert result._model == "databricks/databricks-claude-sonnet-4"
    # Fallbacks get the same fixup; already-prefixed ids pass through.
    assert result._fallback_models == [
        "databricks/databricks-claude-3-5-haiku",
        "openai/gpt-4o-mini",
    ]


def test_build_policy_llm_client_empty_fallbacks_default() -> None:
    """
    A config with no ``fallback_models`` yields an empty fallback
    list — single-model behaviour is preserved.

    What breaks if this fails: the builder injects phantom
    fallbacks, changing behaviour for every existing hosted
    deployment that sets only ``model``.
    """
    result = _build_policy_llm_client(_make_server_llm(), None)

    assert result is not None
    assert result._fallback_models == []


def test_build_policy_llm_client_warns_on_cross_provider_fallback_with_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A fallback on a different provider than the primary, combined
    with a resolved connection, warns at build time.

    What breaks if this fails: the shared connection is silently
    handed to a fallback on another provider, so the fallback
    request fails to authenticate mid-policy-evaluation with no
    hint at startup.
    """
    llm_config = LLMConfig(
        model="databricks/primary",
        fallback_models=["openai/gpt-4o-mini"],
    )
    with caplog.at_level("WARNING"):
        result = _build_policy_llm_client(llm_config, {"api_key": "k"})

    assert result is not None
    assert any(
        "fallback_models target" in r.message and "openai" in r.message for r in caplog.records
    )


def test_build_policy_llm_client_no_warn_same_provider_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Same-provider fallbacks with a connection do not warn — the
    shared connection is valid for every candidate.

    What breaks if this fails: correct configs get spurious
    warnings, training operators to ignore the real ones.
    """
    llm_config = LLMConfig(
        model="databricks/primary",
        fallback_models=["databricks/backup"],
    )
    with caplog.at_level("WARNING"):
        result = _build_policy_llm_client(llm_config, {"api_key": "k"})

    assert result is not None
    assert not any("fallback_models target" in r.message for r in caplog.records)


# ── build_policy_engine wiring ──────────────────────────────────────────────


def test_build_policy_engine_without_server_llm(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``build_policy_engine`` without ``server_llm`` produces an
    engine whose ``_llm_client`` is ``None``.

    What breaks if this fails: engines get a phantom LLM client
    when none was configured.
    """
    conv = conversation_store.create_conversation()
    spec = AgentSpec(spec_version=1, name="test-agent")
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    # No server_llm → no llm_client on the engine.
    assert engine._llm_client is None


def test_build_policy_engine_with_server_llm(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``build_policy_engine`` with ``server_llm`` produces an
    engine whose ``_llm_client`` is a :class:`PolicyLLMClient`.

    What breaks if this fails: the builder ignores the
    ``server_llm`` param, so policies can't access the LLM.
    """
    conv = conversation_store.create_conversation()
    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(
            policies=[
                FunctionPolicySpec(
                    name="noop",
                    on=[PhaseSelector(phase=Phase.REQUEST)],
                    function=FunctionRef(
                        path="tests.runtime.policies.conftest._always_allow",
                    ),
                ),
            ],
        ),
    )
    llm_config = _make_server_llm()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
        server_llm=llm_config,
    )

    # Engine has a PolicyLLMClient with the server config.
    assert engine._llm_client is not None
    assert isinstance(engine._llm_client, PolicyLLMClient)
    assert engine._llm_client._model == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_build_policy_engine_llm_client_reaches_callable(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    End-to-end: ``server_llm`` on the builder produces an engine
    that injects a :class:`PolicyLLMClient` into the callable's
    ``event["llm_client"]``.

    What breaks if this fails: the full pipeline (builder →
    engine → evaluate → _build_event → callable) has a gap.
    """
    bucket: dict[str, Any] = {}
    capturing = _llm_capturing_policy(bucket)
    conv = conversation_store.create_conversation()

    # Build engine with server_llm — we can't use the real
    # build_policy_engine because the capturing policy's spec
    # has a fake function.path. Instead, build the client and
    # engine directly.
    llm_config = _make_server_llm()
    llm_client = _build_policy_llm_client(llm_config, _resolve_server_llm_connection(llm_config))
    engine = PolicyEngine(
        policies=[capturing],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
        llm_client=llm_client,
    )

    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))

    # The capturing policy saw the PolicyLLMClient.
    captured = bucket["llm_client"]
    assert captured is not None
    assert isinstance(captured, PolicyLLMClient)
    assert captured._model == "openai/gpt-4o-mini"
    assert captured._connection == {"api_key": "test-key", "base_url": "https://example.com"}
    assert captured._request_timeout == 60


# ── Databricks profile support ──────────────────────────────────────────────


def test_parse_server_llm_with_profile() -> None:
    """
    ``parse_server_llm`` parses the ``profile:`` field into
    ``LLMConfig.profile``.

    What breaks if this fails: the parser drops the profile
    field, so Databricks profile auth is silently ignored.
    """
    raw = {
        "model": "databricks-claude-sonnet-4-6",
        "profile": "my-workspace",
        "request_timeout": 30,
    }
    result = parse_server_llm(raw, expand_env=False)
    assert result is not None
    assert result.model == "databricks-claude-sonnet-4-6"
    assert result.profile == "my-workspace"
    assert result.connection is None
    # profile should NOT leak into extra
    assert "profile" not in result.extra


def test_parse_server_llm_profile_not_in_extra() -> None:
    """
    ``profile:`` is a reserved key — it must not appear in
    ``extra`` alongside model kwargs.

    What breaks if this fails: ``profile`` is passed through to
    the LLM SDK as a kwarg, which would cause an unexpected
    parameter error.
    """
    raw = {
        "model": "databricks-gpt-5-4-mini",
        "profile": "dev",
        "temperature": 0.5,
    }
    result = parse_server_llm(raw, expand_env=False)
    assert result is not None
    assert result.profile == "dev"
    assert result.extra == {"temperature": 0.5}


def test_resolve_server_llm_connection_resolves_databricks_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_resolve_server_llm_connection`` resolves a Databricks profile
    to connection params when ``connection`` is absent.

    What breaks if this fails: specifying ``profile:`` in the
    server config has no effect — the classifier and PolicyLLMClient
    get ``connection=None`` and fall back to env var / OpenAI
    defaults instead of the gateway.
    """
    from omnigent.runtime.credentials.databricks import WorkspaceCreds

    monkeypatch.setattr(
        "omnigent.runtime.policies.builder.resolve_databricks_workspace",
        lambda profile: WorkspaceCreds(
            host="https://example.cloud.databricks.com",
            token="dapi-test-token",
        ),
    )

    llm_config = LLMConfig(
        model="databricks-claude-sonnet-4-6",
        profile="my-workspace",
    )
    connection = _resolve_server_llm_connection(llm_config)

    # Profile resolved to serving-endpoints URL + bearer token.
    assert connection == {
        "base_url": "https://example.cloud.databricks.com/serving-endpoints",
        "api_key": "dapi-test-token",
    }


def test_resolve_server_llm_connection_connection_wins_over_profile() -> None:
    """
    When both ``connection`` and ``profile`` are set, ``connection``
    wins — the profile is not resolved.

    What breaks if this fails: explicit connection params are
    overwritten by the profile, causing auth to go to the wrong
    endpoint.
    """
    llm_config = LLMConfig(
        model="databricks-gpt-5-4-mini",
        connection={"api_key": "explicit-key", "base_url": "https://explicit.com"},
        profile="should-be-ignored",
    )
    connection = _resolve_server_llm_connection(llm_config)

    # Explicit connection is used as-is; profile is not resolved.
    assert connection == {
        "api_key": "explicit-key",
        "base_url": "https://explicit.com",
    }


def test_resolve_server_llm_connection_none_returns_none() -> None:
    """
    ``_resolve_server_llm_connection(None)`` returns ``None`` and a
    config with neither connection nor profile also resolves to
    ``None``.

    What breaks if this fails: an absent server LLM would yield a
    truthy connection, masking the no-config case.
    """
    assert _resolve_server_llm_connection(None) is None
    bare = LLMConfig(model="openai/gpt-4o-mini")
    assert _resolve_server_llm_connection(bare) is None
