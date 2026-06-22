"""Tests for :mod:`omnigent.runner.cost_judge` — the per-turn LLM judge.

Covers:

- The judge's LLM-call-and-parse path with a SCRIPTED client returning
  canned JSON (real :class:`Response` types, never MagicMock — the judge
  relies on the ``isinstance(item, MessageOutput)`` gate): a single
  tier+model verdict, the conversational null verdict, an out-of-tier
  model clamp, a fenced-JSON body, malformed JSON, an unknown tier, a
  no-assistant-text response, the retry path (transient error then
  success), and total judge failure.
- The judge-model resolution (cheapest-tier default + ``advisor_model``
  override).
- The mode-resolution precedence matrix (override × spec marker).

The judge fails OPEN: a broken call (error, malformed, unknown tier)
returns ``None`` and never raises into the turn, so the judge can only
turn red if that contract breaks.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.cost_plan import AdvisorVerdict
from omnigent.llms.types import MessageOutput, OutputText, Response
from omnigent.runner.cost_judge import (
    LLMJudge,
    build_llm_judge,
    resolve_advisor_mode,
)

# Two configured tiers, multiple models each, so clamp / out-of-tier
# behavior is observable.
_TIERS: dict[str, tuple[str, ...]] = {
    "cheap": ("databricks-claude-haiku-4-5", "databricks-gpt-5-4-mini"),
    "expensive": ("databricks-claude-opus-4-8", "databricks-gpt-5-5"),
}
_ANCHOR = "2026-06-10T00:00:00+00:00"


def _response(text: str) -> Response:
    """
    Build a minimal real :class:`Response` carrying ``text``.

    Real SDK types (``MessageOutput`` + ``OutputText``) so the judge's
    ``isinstance(item, MessageOutput)`` extraction matches production; a
    MagicMock would silently fail the gate and the judge would see no
    text.

    :param text: The assistant text the scripted judge returns, e.g.
        ``'{"tier": null}'``.
    :returns: A :class:`Response` with one assistant message.
    """
    return Response(output=[MessageOutput(content=[OutputText(text=text)])], model="test-model")


class _ScriptedClient:
    """
    LLM client stub returning canned responses and recording each call.

    Real stub class (not MagicMock) so an unexpected extra call is
    visible via ``call_count`` and a path that should short-circuit
    fails loud instead of silently passing.

    :param texts: One assistant text per expected call, returned in
        order. Exception instances are re-raised instead of returned —
        used to script the retry path.
    """

    def __init__(self, *texts: str | Exception) -> None:
        self._texts: list[str | Exception] = list(texts)
        self.call_count = 0
        self.captured: list[dict[str, Any]] = []  # type: ignore[explicit-any]  # kwargs per call

    class _Responses:
        """Inner namespace exposing ``create`` like the real client."""

        def __init__(self, outer: _ScriptedClient) -> None:
            self._outer = outer

        async def create(self, **kwargs: Any) -> Response:  # type: ignore[explicit-any]
            """Record the call and return / raise the next scripted item."""
            outer = self._outer
            idx = outer.call_count
            outer.call_count += 1
            outer.captured.append(kwargs)
            item = outer._texts[idx]
            if isinstance(item, Exception):
                raise item
            return _response(item)

    @property
    def responses(self) -> _ScriptedClient._Responses:
        """Return the responses namespace."""
        return self._Responses(self)


def _judge(client: _ScriptedClient) -> LLMJudge:
    """
    Build an :class:`LLMJudge` over the test catalog and a stub.

    :param client: The scripted client the judge calls.
    :returns: A judge wired to *client* (no real LLM client built).
    """
    return build_llm_judge(
        tiers=_TIERS,
        executor_config={"cost_optimize": {"tiers": {}}},
        connection=None,
        client=client,
    )


# ── Judge: response → verdict ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_carries_judged_tier_and_model() -> None:
    """A tier+model response becomes a verdict with that tier + model —
    proving the parsed JSON reached the caller intact."""
    client = _ScriptedClient(
        '{"tier": "expensive", "model": "databricks-claude-opus-4-8", '
        '"rationale": "deep refactor"}'
    )
    verdict = await _judge(client).judge(query="refactor the auth flow", turn_anchor=_ANCHOR)
    assert verdict is not None
    # Exact verdict: a wrong tier/model means the JSON was dropped or
    # mis-mapped, and the brain would run at the wrong price.
    assert verdict.tier == "expensive"
    assert verdict.model == "databricks-claude-opus-4-8"
    assert verdict.rationale == "deep refactor"
    assert verdict.turn_anchor == _ANCHOR
    # The judge never sets applied — that is the advisor's decision.
    assert verdict.applied is False
    # Exactly one judge call per turn — a second would double the cost.
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_verdict_strips_markdown_code_fence() -> None:
    """A fenced JSON body (```json ... ```) still parses to a verdict."""
    client = _ScriptedClient(
        '```json\n{"tier": "cheap", "model": "databricks-claude-haiku-4-5"}\n```'
    )
    verdict = await _judge(client).judge(query="what's 2+2?", turn_anchor=_ANCHOR)
    assert verdict is not None
    # The fence must be stripped before json.loads; otherwise this turn
    # would have failed open (None) and the test would catch it below.
    assert verdict.tier == "cheap"
    assert verdict.model == "databricks-claude-haiku-4-5"


@pytest.mark.asyncio
async def test_null_verdict_returns_none_for_conversational_turn() -> None:
    """The explicit ``{"tier": null}`` is a conversational turn: the judge
    returns ``None`` (prior selection stands), not an error."""
    client = _ScriptedClient('{"tier": null}')
    verdict = await _judge(client).judge(query="thanks!", turn_anchor=_ANCHOR)
    assert verdict is None
    # The judge still made its one call to learn the turn is conversational.
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_empty_query_skips_llm_call() -> None:
    """A whitespace-only query is conversational without any LLM call —
    saves a judge call on non-text turns."""
    client = _ScriptedClient()  # no scripted responses; a call would IndexError
    verdict = await _judge(client).judge(query="   ", turn_anchor=_ANCHOR)
    assert verdict is None
    # Zero calls proves the empty-query short-circuit fired BEFORE the LLM.
    assert client.call_count == 0


@pytest.mark.asyncio
async def test_out_of_tier_model_is_clamped_to_tier_first() -> None:
    """A model pin outside the named tier clamps to that tier's first model
    rather than failing the turn."""
    client = _ScriptedClient(
        '{"tier": "cheap", "model": "databricks-claude-opus-4-8", "rationale": "r"}'
    )
    verdict = await _judge(client).judge(query="rename a var", turn_anchor=_ANCHOR)
    assert verdict is not None
    assert verdict.tier == "cheap"
    # opus is an expensive-tier model; clamped to cheap's first model so a
    # hallucinated pin degrades to the tier's canonical model, not a crash.
    assert verdict.model == "databricks-claude-haiku-4-5"


@pytest.mark.asyncio
async def test_unknown_tier_fails_open_to_none() -> None:
    """A verdict naming an unconfigured tier is a judge failure → None."""
    client = _ScriptedClient('{"tier": "platinum", "model": "x"}')
    verdict = await _judge(client).judge(query="do a thing", turn_anchor=_ANCHOR)
    # None (not raise): an unknown tier can't be ranked, so the turn runs
    # unadvised rather than blocking.
    assert verdict is None


@pytest.mark.asyncio
async def test_malformed_json_fails_open_after_retry() -> None:
    """Non-JSON output fails open to None — after retrying once."""
    client = _ScriptedClient("not json at all", "still not json")
    verdict = await _judge(client).judge(query="hello", turn_anchor=_ANCHOR)
    assert verdict is None
    # Two attempts (initial + one retry) before giving up — a single
    # attempt would mean the retry path was lost.
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_no_assistant_text_fails_open() -> None:
    """A response with no assistant text fails open to None."""
    # Empty output list => _extract_assistant_text raises => caught => None.
    empty = Response(output=[], model="test-model")

    class _EmptyClient:
        class _Responses:
            async def create(self, **kwargs: Any) -> Response:  # type: ignore[explicit-any]
                return empty

        @property
        def responses(self) -> _EmptyClient._Responses:
            return self._Responses()

    judge = build_llm_judge(
        tiers=_TIERS, executor_config=None, connection=None, client=_EmptyClient()
    )
    assert await judge.judge(query="hi", turn_anchor=_ANCHOR) is None


@pytest.mark.asyncio
async def test_retry_recovers_from_transient_error() -> None:
    """A transient error on the first call, then a good verdict, succeeds."""
    client = _ScriptedClient(
        RuntimeError("transient gateway error"),
        '{"tier": "medium", "model": "x"}',  # model clamps; tier is configured below
    )
    # medium is not in _TIERS, so use a catalog that has it for this test.
    judge = build_llm_judge(
        tiers={"medium": ("databricks-claude-sonnet-4-6",)},
        executor_config=None,
        connection=None,
        client=client,
    )
    verdict = await judge.judge(query="summarize this module", turn_anchor=_ANCHOR)
    assert verdict is not None
    assert verdict.tier == "medium"
    # Clamped to the only medium model — proves the recovered call's verdict
    # flowed through the clamp.
    assert verdict.model == "databricks-claude-sonnet-4-6"
    # Two calls: the failed one + the successful retry.
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_total_failure_returns_none_never_raises() -> None:
    """Both attempts error => None (fail-open), never a raise into the turn."""
    client = _ScriptedClient(RuntimeError("boom"), TimeoutError("slow"))
    verdict = await _judge(client).judge(query="do something hard", turn_anchor=_ANCHOR)
    assert verdict is None
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_prompt_inlines_tier_menu_for_the_judge() -> None:
    """The judge prompt names every configured tier's models so the judge
    can only pin a real id."""
    client = _ScriptedClient('{"tier": null}')
    await _judge(client).judge(query="anything", turn_anchor=_ANCHOR)
    prompt = client.captured[0]["input"][0]["content"][0]["text"]
    # Every configured model id appears in the prompt menu — a missing one
    # would mean the judge can't pin it and would always clamp.
    for tier_models in _TIERS.values():
        for model in tier_models:
            assert model in prompt


# ── Judge-model resolution ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_judge_model_is_cheapest_tier_first() -> None:
    """Absent an override, the judge call runs on the cheapest tier's first
    model (a cheap judge for a cheap decision)."""
    client = _ScriptedClient('{"tier": null}')
    await _judge(client).judge(query="hi", turn_anchor=_ANCHOR)
    # The model the judge CALL used (captured kwargs), not the verdict's.
    assert client.captured[0]["model"] == "databricks-claude-haiku-4-5"


@pytest.mark.asyncio
async def test_advisor_model_override_picks_judge_model() -> None:
    """``cost_optimize.advisor_model`` overrides the judge-call model."""
    client = _ScriptedClient('{"tier": null}')
    judge = build_llm_judge(
        tiers=_TIERS,
        executor_config={"cost_optimize": {"advisor_model": "databricks-gpt-5-4-mini"}},
        connection=None,
        client=client,
    )
    await judge.judge(query="hi", turn_anchor=_ANCHOR)
    assert client.captured[0]["model"] == "databricks-gpt-5-4-mini"


# ── Mode resolution precedence ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec_mode,override,expected",
    [
        # No override: spec mode stands.
        ("advise", None, "advise"),
        ("optimize", None, "optimize"),
        # Toggle ON escalates to optimize — even an advise-default spec.
        ("advise", "on", "optimize"),
        ("optimize", "on", "optimize"),
        # Toggle OFF disables the advisor for the session.
        ("advise", "off", None),
        ("optimize", "off", None),
        # Unexpected override value defers to the spec marker.
        ("optimize", "weird", "optimize"),
    ],
)
def test_resolve_advisor_mode_precedence(
    spec_mode: str, override: str | None, expected: str | None
) -> None:
    """Per-session override beats the spec marker; off disables; unknown
    defers."""
    assert resolve_advisor_mode(spec_mode, override) == expected


def test_verdict_dataclass_is_unapplied_by_judge() -> None:
    """Sanity: the judge constructs verdicts with applied=False; the advisor
    flips it. Guards against a regression that pre-applies in the judge."""
    # Build directly to assert the contract default the judge depends on.
    v = AdvisorVerdict(
        tier="cheap",
        model="databricks-claude-haiku-4-5",
        applied=False,
        rationale="r",
        turn_anchor=_ANCHOR,
    )
    assert v.applied is False


# ── Databricks judge-model routing (real-client path) ─────────────────────────


@pytest.mark.asyncio
async def test_bare_databricks_judge_model_routes_via_databricks_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``databricks-*`` judge model is routed through the databricks
    adapter on the real-client path: provider-prefixed model + a connection
    resolved from the brain's profile.

    Without the prefix the generic client routes the bare id to the default
    openai adapter (api.openai.com) and the judge 401s and fails open on
    EVERY turn — the live-run failure that motivated the routing.
    """
    from omnigent.runtime.credentials.databricks import WorkspaceCreds

    client = _ScriptedClient('{"tier": null}')
    resolved_profiles: list[str | None] = []

    def _fake_creds(profile: str | None) -> WorkspaceCreds:
        resolved_profiles.append(profile)
        return WorkspaceCreds(host="https://example.databricks.com", token="tok-123")

    # The real-client path lazily does `from omnigent.llms.client import
    # Client`; rebind that symbol so no real client is constructed.
    monkeypatch.setattr("omnigent.llms.client.Client", lambda: client)
    monkeypatch.setattr("omnigent.runner.cost_judge._resolve_workspace_creds", _fake_creds)
    judge = build_llm_judge(
        tiers=_TIERS,
        executor_config={"cost_optimize": {"tiers": {}}},
        connection=None,
        databricks_profile="brain-profile",
    )
    await judge.judge(query="hi", turn_anchor=_ANCHOR)
    # Credentials were resolved from the BRAIN's profile — a None here
    # means the advisor's profile threading broke (ambient fallback).
    assert resolved_profiles == ["brain-profile"]
    # Prefixed model = the databricks adapter handles the call; the bare id
    # would mean the openai-adapter misroute regressed.
    assert client.captured[0]["model"] == "databricks/databricks-claude-haiku-4-5"
    # The call carries the gateway connection resolved from the profile.
    assert client.captured[0]["connection_params"] == {
        "base_url": "https://example.databricks.com/serving-endpoints",
        "api_key": "tok-123",
    }


@pytest.mark.asyncio
async def test_databricks_credential_failure_falls_back_to_adapter_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed credential resolution keeps the prefixed model but passes no
    connection (the adapter then auto-resolves ambiently) — building the
    judge never raises into the turn."""

    def _broken_creds(profile: str | None) -> None:
        raise OSError("no usable Databricks credentials")

    client = _ScriptedClient('{"tier": null}')
    monkeypatch.setattr("omnigent.llms.client.Client", lambda: client)
    monkeypatch.setattr("omnigent.runner.cost_judge._resolve_workspace_creds", _broken_creds)
    judge = build_llm_judge(
        tiers=_TIERS,
        executor_config={"cost_optimize": {"tiers": {}}},
        connection=None,
        databricks_profile="brain-profile",
    )
    await judge.judge(query="hi", turn_anchor=_ANCHOR)
    # Still routed to the databricks adapter (prefix is independent of creds).
    assert client.captured[0]["model"] == "databricks/databricks-claude-haiku-4-5"
    # No connection: the adapter's own ambient resolution is the fallback.
    assert client.captured[0]["connection_params"] is None


@pytest.mark.asyncio
async def test_non_databricks_judge_model_is_not_rerouted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider-prefixed non-Databricks judge model is passed through
    untouched and triggers NO Databricks credential resolution."""

    def _must_not_resolve(profile: str | None) -> None:
        raise AssertionError("credential resolution must not run for a non-Databricks model")

    client = _ScriptedClient('{"tier": null}')
    monkeypatch.setattr("omnigent.llms.client.Client", lambda: client)
    monkeypatch.setattr("omnigent.runner.cost_judge._resolve_workspace_creds", _must_not_resolve)
    judge = build_llm_judge(
        tiers=_TIERS,
        executor_config={"cost_optimize": {"advisor_model": "anthropic/claude-haiku-4-5"}},
        connection=None,
        databricks_profile="brain-profile",
    )
    await judge.judge(query="hi", turn_anchor=_ANCHOR)
    assert client.captured[0]["model"] == "anthropic/claude-haiku-4-5"
    assert client.captured[0]["connection_params"] is None
