"""Tests for :mod:`omnigent.runner.cost_advisor` — the v3 cost advisor.

Covers:

- Mode OFF (no ``executor.config.cost_optimize``) = zero behavior
  change: ``None`` returned and ZERO HTTP traffic (raising client).
- The verdict label is persisted via PATCH (captured with a real
  ``httpx.MockTransport``) and round-trips back, with ``applied``
  matching what the advisor actually did.
- The APPLICATION / PRECEDENCE / MODE matrix: optimize on a claude-sdk
  brain with no user pin applies the verdict model + injects the note;
  advise mode shadows (records, no apply, no note); a user model pin
  beats the advisor; a non-claude-sdk brain records but never applies.
- None verdict (conversational): skips label and apply.
- A failed label persist applies nothing.
- Config-parsing fail-loud paths.

Real types throughout: real ``AgentSpec`` / ``ExecutorSpec``, real
``httpx.MockTransport``, and a scripted ``Judge`` stub returning real
``AdvisorVerdict`` objects.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.cost_plan import AdvisorVerdict, parse_verdict
from omnigent.entities.conversation import NON_CONTENT_ITEM_TYPES, parse_item_data
from omnigent.runner.cost_advisor import (
    AdvisorConfig,
    maybe_run_advisor,
    parse_advisor_config,
    routing_decision_event,
)
from omnigent.runner.identity import (
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_TOKEN_HEADER,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec

_TIERS_YAML: dict[str, Any] = {  # type: ignore[explicit-any]  # YAML-shaped config payload
    "mode": "optimize",
    "tiers": {
        "cheap": ["databricks-claude-haiku-4-5"],
        "medium": ["databricks-claude-sonnet-4-6"],
        "expensive": ["databricks-claude-opus-4-8"],
    },
}
_ANCHOR = "2026-06-10T00:00:00+00:00"
_TURN_CONTENT = [{"type": "input_text", "text": "refactor the auth flow"}]


def _orchestrator_spec(*, cost_optimize: Any = None) -> AgentSpec:  # type: ignore[explicit-any]
    """Build a claude-sdk orchestrator spec.

    :param cost_optimize: Value for the advisor marker; ``None`` omits it
        (advisor off).
    :returns: An :class:`AgentSpec` with a claude-sdk brain.
    """
    config: dict[str, Any] = {"harness": "claude-sdk"}  # type: ignore[explicit-any]
    if cost_optimize is not None:
        config["cost_optimize"] = cost_optimize
    return AgentSpec(
        spec_version=1,
        name="orchestrator",
        executor=ExecutorSpec(type="omnigent", config=config),
    )


def _verdict(
    *, tier: str = "expensive", model: str = "databricks-claude-opus-4-8"
) -> AdvisorVerdict:
    """Build an unapplied verdict (as the judge produces it).

    :param tier: Difficulty tier, default ``"expensive"``.
    :param model: Brain model id, default the opus tier model.
    :returns: A verdict with ``applied=False``.
    """
    return AdvisorVerdict(
        tier=tier, model=model, applied=False, rationale="hard refactor", turn_anchor=_ANCHOR
    )


class _ScriptedJudge:
    """Judge stub returning a fixed verdict (or None) and counting calls.

    Real stub class (not MagicMock) so an unexpected call is visible and
    a short-circuited path fails loud.

    :param verdict: The verdict to return, or ``None`` (conversational).
    """

    def __init__(self, verdict: AdvisorVerdict | None) -> None:
        self._verdict = verdict
        self.call_count = 0

    async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict | None:
        """Return the scripted verdict, re-anchored to *turn_anchor*."""
        self.call_count += 1
        if self._verdict is None:
            return None
        # Re-anchor so the verdict carries the turn's anchor like the real judge.
        return AdvisorVerdict(
            tier=self._verdict.tier,
            model=self._verdict.model,
            applied=False,
            rationale=self._verdict.rationale,
            turn_anchor=turn_anchor,
        )


def _raising_handler(request: httpx.Request) -> httpx.Response:
    """MockTransport handler that fails the test on ANY request."""
    raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")


def _raising_transport() -> httpx.MockTransport:
    """Build the zero-traffic transport for no-I/O paths."""
    return httpx.MockTransport(_raising_handler)


class _PatchCapture:
    """Captures session PATCH bodies + headers and answers with a status.

    :param status_code: Status returned to every PATCH, e.g. ``200``.
    """

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.requests: list[dict[str, Any]] = []  # type: ignore[explicit-any]  # JSON bodies
        self.headers: list[httpx.Headers] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Record the PATCH body + headers and reply."""
        assert request.method == "PATCH", f"unexpected {request.method} {request.url}"
        self.requests.append(json.loads(request.content.decode()))
        self.headers.append(request.headers)
        return httpx.Response(self.status_code, json={})


def _client(transport: httpx.BaseTransport) -> httpx.AsyncClient:
    """Build a server client over a test transport."""
    return httpx.AsyncClient(transport=transport, base_url="http://omnigent.test")


async def _run(
    *,
    spec: AgentSpec | None,
    judge: _ScriptedJudge,
    transport: httpx.BaseTransport,
    harness: str | None = "claude-sdk",
    user_model_override: str | None = None,
    cost_control_mode_override: str | None = None,
) -> Any:  # type: ignore[explicit-any]  # AdvisorTurnResult | None
    """Drive :func:`maybe_run_advisor` with the test wiring.

    :returns: The advisor turn result, or ``None``.
    """
    async with _client(transport) as client:
        return await maybe_run_advisor(
            spec=spec,
            conversation_id="conv_x",
            turn_content=_TURN_CONTENT,
            server_client=client,
            turn_anchor=_ANCHOR,
            harness=harness,
            user_model_override=user_model_override,
            cost_control_mode_override=cost_control_mode_override,
            judge=judge,
        )


# ── Mode off / inert paths (zero HTTP traffic) ─────────────────────────────────


@pytest.mark.asyncio
async def test_mode_off_is_inert() -> None:
    """No marker → ``None`` and zero HTTP traffic.

    The raising transport makes any PATCH fail the test, proving the dark
    path is byte-identical to pre-advisor turns. The judge must also not
    be called (no marker, no judge).
    """
    judge = _ScriptedJudge(_verdict())
    result = await _run(spec=_orchestrator_spec(), judge=judge, transport=_raising_transport())
    assert result is None
    # Zero judge calls: a no-marker spec must short-circuit before judging.
    assert judge.call_count == 0


@pytest.mark.asyncio
async def test_spec_none_is_inert() -> None:
    """An unresolved spec (None) cannot opt in — no I/O, no judge."""
    judge = _ScriptedJudge(_verdict())
    result = await _run(spec=None, judge=judge, transport=_raising_transport())
    assert result is None
    assert judge.call_count == 0


@pytest.mark.asyncio
async def test_override_off_disables_advisor() -> None:
    """The session toggle ``off`` disables the advisor: ``None``, no I/O."""
    judge = _ScriptedJudge(_verdict())
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=_raising_transport(),
        cost_control_mode_override="off",
    )
    assert result is None
    # off short-circuits before the judge call (no cost incurred).
    assert judge.call_count == 0


@pytest.mark.asyncio
async def test_conversational_verdict_skips_label_and_apply() -> None:
    """A None (conversational) verdict skips label persist and apply."""
    judge = _ScriptedJudge(None)
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=_raising_transport(),
    )
    assert result is None
    assert judge.call_count == 1


# ── Optimize mode: apply + persist + note ──────────────────────────────────────


@pytest.mark.asyncio
async def test_optimize_applies_model_persists_applied_verdict_and_note() -> None:
    """Optimize on a claude-sdk brain, no user pin: the verdict model is
    applied, the label persists with applied=True, and the note is injected."""
    capture = _PatchCapture()
    judge = _ScriptedJudge(_verdict(tier="expensive", model="databricks-claude-opus-4-8"))
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
    )
    assert result is not None
    # apply_model is the verdict model — this is what the runner stamps on
    # the harness body; None here would mean the brain never switched.
    assert result.apply_model == "databricks-claude-opus-4-8"
    assert result.verdict.applied is True
    # The note announces the applied model + tier.
    assert result.note_item is not None
    note_text = result.note_item["content"][0]["text"]
    assert note_text == "[Cost advisor: this turn runs on databricks-claude-opus-4-8 (expensive)]"

    # Exactly one label PATCH, carrying a v3 verdict that round-trips with
    # applied=True (proves persisted state matches the applied decision).
    assert len(capture.requests) == 1
    labels = capture.requests[0]["labels"]
    parsed = parse_verdict(labels)
    assert parsed is not None
    assert parsed.model == "databricks-claude-opus-4-8"
    assert parsed.tier == "expensive"
    assert parsed.applied is True


@pytest.mark.asyncio
async def test_optimize_user_pin_beats_advisor() -> None:
    """A user model pin wins: the verdict is recorded (applied=False) but
    NOT applied, and no note is injected."""
    capture = _PatchCapture()
    judge = _ScriptedJudge(_verdict())
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
        user_model_override="databricks-claude-sonnet-4-6",  # the user's /model pin
    )
    assert result is not None
    # Application is suppressed — the brain runs on the user's pin (the
    # harness honors it), not the advisor's.
    assert result.apply_model is None
    assert result.note_item is None
    # Still recorded for telemetry, but applied=False (shadow under a pin).
    parsed = parse_verdict(capture.requests[0]["labels"])
    assert parsed is not None
    assert parsed.applied is False


@pytest.mark.asyncio
async def test_optimize_non_claude_sdk_records_but_does_not_apply() -> None:
    """Owner scope pin: a non-claude-sdk brain records the verdict but never
    applies it (no apply_model, no note)."""
    capture = _PatchCapture()
    judge = _ScriptedJudge(_verdict())
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
        harness="codex",  # not claude-sdk → advise-style labeling only
    )
    assert result is not None
    assert result.apply_model is None
    assert result.note_item is None
    parsed = parse_verdict(capture.requests[0]["labels"])
    assert parsed is not None
    # Recorded but unapplied — the verdict exists for telemetry, the codex
    # brain model is untouched.
    assert parsed.applied is False


# ── Advise mode: shadow ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_advise_mode_shadows_no_apply_no_note() -> None:
    """advise mode shadows, records but never applies."""
    capture = _PatchCapture()
    advise_yaml = {**_TIERS_YAML, "mode": "advise"}
    judge = _ScriptedJudge(_verdict())
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=advise_yaml),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
    )
    assert result is not None
    assert result.apply_model is None
    assert result.note_item is None
    parsed = parse_verdict(capture.requests[0]["labels"])
    assert parsed is not None and parsed.applied is False


@pytest.mark.asyncio
async def test_override_on_escalates_advise_to_optimize() -> None:
    """The session toggle ``on`` with an advise spec escalates to optimize:
    the verdict is now applied."""
    capture = _PatchCapture()
    advise_yaml = {**_TIERS_YAML, "mode": "advise"}
    judge = _ScriptedJudge(_verdict())
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=advise_yaml),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
        cost_control_mode_override="on",
    )
    assert result is not None
    # on → optimize: an advise spec now applies. apply_model proves the
    # override flipped the behavior, not just the label.
    assert result.apply_model == "databricks-claude-opus-4-8"
    parsed = parse_verdict(capture.requests[0]["labels"])
    assert parsed is not None
    assert parsed.applied is True


# ── Degradation: failed persist is telemetry-only ────────────────────────────────


@pytest.mark.asyncio
async def test_failed_persist_applies_nothing() -> None:
    """A failed label PATCH must NOT kill the turn — assert result is None."""
    capture = _PatchCapture(status_code=403)  # multi-user reject
    judge = _ScriptedJudge(_verdict())
    result = await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
    )
    # The PATCH was attempted (and failed).
    assert len(capture.requests) == 1
    assert result is None


# ── Reserved-label authority header ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_carries_runner_tunnel_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the runner process has its tunnel binding token, the label PATCH
    carries it so multi-user servers authorize the advisor's reserved-label
    write."""
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "tok-123")
    capture = _PatchCapture()
    judge = _ScriptedJudge(_verdict())
    await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
    )
    # The token rides the reserved-namespace gate; without it,
    # a multi-user server would 403 the write.
    assert capture.headers[0].get(RUNNER_TUNNEL_TOKEN_HEADER) == "tok-123"


@pytest.mark.asyncio
async def test_persist_omits_token_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a tunnel token, the PATCH omits the header — single-user
    servers accept the write without it."""
    monkeypatch.delenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, raising=False)
    capture = _PatchCapture()
    judge = _ScriptedJudge(_verdict())
    await _run(
        spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
        judge=judge,
        transport=httpx.MockTransport(capture.handler),
    )
    assert RUNNER_TUNNEL_TOKEN_HEADER not in capture.headers[0]


# ── Config parsing ──────────────────────────────────────────────────────────────


def test_parse_advisor_config_absent_is_none() -> None:
    """No marker => None (advisor off)."""
    assert parse_advisor_config({"harness": "claude-sdk"}) is None


def test_parse_advisor_config_false_opt_out_is_none() -> None:
    """An explicit ``false`` is an opt-out, not a malformed config."""
    assert parse_advisor_config({"cost_optimize": False}) is None


def test_parse_advisor_config_happy_path() -> None:
    """A well-formed marker parses into mode + tier catalog tuples."""
    config = parse_advisor_config({"cost_optimize": _TIERS_YAML})
    assert config == AdvisorConfig(
        tiers={
            "cheap": ("databricks-claude-haiku-4-5",),
            "medium": ("databricks-claude-sonnet-4-6",),
            "expensive": ("databricks-claude-opus-4-8",),
        },
        mode="optimize",
    )


def test_parse_advisor_config_defaults_mode_optimize() -> None:
    """Omitting ``mode`` defaults to optimize (apply)."""
    config = parse_advisor_config(
        {"cost_optimize": {"tiers": {"cheap": ["databricks-claude-haiku-4-5"]}}}
    )
    assert config is not None
    assert config.mode == "optimize"


@pytest.mark.parametrize(
    "marker,match",
    [
        ({}, "present but empty"),
        ("not-a-mapping", "must be a mapping"),
        ({"tiers": {}}, "non-empty"),
        ({"mode": "bogus", "tiers": {"cheap": ["m"]}}, "mode must be one of"),
        ({"tiers": {"platinum": ["m"]}}, "unknown tier"),
        ({"tiers": {"cheap": "m"}}, "must be a list"),
        ({"tiers": {"cheap": [""]}}, "non-empty model-id"),
    ],
)
def test_parse_advisor_config_fail_loud(marker: Any, match: str) -> None:  # type: ignore[explicit-any]
    """A present-but-broken marker fails loud rather than silently off."""
    with pytest.raises(ValueError, match=match):
        parse_advisor_config({"cost_optimize": marker})


@pytest.mark.asyncio
async def test_default_judge_build_threads_brain_databricks_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no judge is injected, the production judge is built with the
    brain's resolved Databricks profile so the judge call rides the same
    gateway as the brain (a bare ``databricks-*`` judge model would
    otherwise misroute to the openai adapter and fail open every turn)."""
    captured: dict[str, Any] = {}  # type: ignore[explicit-any]  # build_llm_judge kwargs

    class _NullJudge:
        """Judge stub that always reports the turn conversational."""

        async def judge(self, *, query: str, turn_anchor: str) -> None:
            """Return None (no verdict) so no label PATCH is attempted."""
            return

    def _capture_build(**kwargs: Any) -> _NullJudge:  # type: ignore[explicit-any]
        captured.update(kwargs)
        return _NullJudge()

    monkeypatch.setattr("omnigent.runner.cost_advisor.build_llm_judge", _capture_build)
    monkeypatch.setattr(
        # The profile resolver reads the user-level provider config; stub it
        # so the test is hermetic on any box.
        "omnigent.runner.cost_advisor._databricks_profile_for_spec",
        lambda spec: "brain-profile",
    )
    async with _client(_raising_transport()) as client:
        result = await maybe_run_advisor(
            spec=_orchestrator_spec(cost_optimize=_TIERS_YAML),
            conversation_id="conv_x",
            turn_content=_TURN_CONTENT,
            server_client=client,
            turn_anchor=_ANCHOR,
            harness="claude-sdk",
        )
    assert result is None
    # The brain's profile reached the judge builder — a missing key means
    # the advisor stopped threading it and the judge falls back to ambient
    # credential resolution (the misroute regression).
    assert captured["databricks_profile"] == "brain-profile"


# ── routing_decision_event: the turn-start transcript chip ───────────────────


def test_routing_decision_event_shape_applied() -> None:
    """The event is a ``response.output_item.done`` carrying a
    ``routing_decision`` item with the verdict's model/tier/applied/rationale.

    A wrong shape here means the AP server relay never recognizes it as a
    routing item (its ``_routing_decision_item_from_sse`` gates on exactly
    this type + item type), so the chip never persists or streams."""
    verdict = AdvisorVerdict(
        tier="expensive",
        model="databricks-claude-opus-4-8",
        applied=True,
        rationale="multi-file refactor needs deep reasoning",
        turn_anchor=_ANCHOR,
    )
    event = routing_decision_event(verdict)
    assert event["type"] == "response.output_item.done"
    item = event["item"]
    assert item["type"] == "routing_decision"
    # Every render field is carried through verbatim — a dropped field
    # would render a chip missing its model, tier, or rationale.
    assert item["model"] == "databricks-claude-opus-4-8"
    assert item["tier"] == "expensive"
    assert item["applied"] is True
    assert item["rationale"] == "multi-file refactor needs deep reasoning"


def test_routing_decision_event_shadow_carries_applied_false() -> None:
    """A shadow verdict (advise mode / user pin won) carries
    ``applied=False`` so the UI renders "would have picked" instead of
    naming the active model. If this flipped to True, the chip would
    falsely claim the brain ran on the router's pick."""
    verdict = AdvisorVerdict(
        tier="cheap",
        model="databricks-claude-haiku-4-5",
        applied=False,
        rationale="trivial question",
        turn_anchor=_ANCHOR,
    )
    item = routing_decision_event(verdict)["item"]
    assert item["applied"] is False
    assert item["model"] == "databricks-claude-haiku-4-5"


def test_routing_decision_event_item_parses_as_routing_decision_data() -> None:
    """The emitted item validates against the real ``RoutingDecisionData``
    model the AP server relay parses it with — so a field-name or type
    drift between the runner emitter and the entity model fails here, not
    silently at relay time where it would just drop the frame."""
    verdict = AdvisorVerdict(
        tier="medium",
        model="databricks-claude-sonnet-4-6",
        applied=True,
        rationale="moderate knowledge work",
        turn_anchor=_ANCHOR,
    )
    item = routing_decision_event(verdict)["item"]
    data = parse_item_data("routing_decision", item)
    # Round-trips through the entity model: proves the relay's
    # parse_item_data("routing_decision", item) will succeed and persist.
    assert data.model == "databricks-claude-sonnet-4-6"
    assert data.tier == "medium"
    assert data.applied is True
    assert data.rationale == "moderate knowledge work"


def test_routing_decision_type_is_non_content() -> None:
    """The item type is in NON_CONTENT_ITEM_TYPES, so the agent loop's
    history filter never feeds it to the model. If it were removed from
    that set, the brain would start seeing (and answering) its own router
    note — the exact "must not enter conversation history" constraint."""
    assert "routing_decision" in NON_CONTENT_ITEM_TYPES
