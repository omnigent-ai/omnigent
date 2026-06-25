"""Per-turn cost advisor (runner-side) for brain-model selection.

When an agent spec opts in (see :func:`parse_advisor_config`), the
runner's turn setup calls :func:`maybe_run_advisor` before the user
message reaches the harness. The advisor runs an LLM
:class:`~omnigent.runner.cost_judge.Judge` over the turn's user query
and produces ONE :class:`~omnigent.cost_plan.AdvisorVerdict` sizing the
turn's difficulty to a model for the orchestrator's OWN brain. It then:

- persists the verdict as the session's ``cost_control.plan`` label
  (v3 telemetry, surfaced in the UI), and
- in **optimize** mode, reports the model the caller must run the brain
  on this turn (the runner sets it on the harness request — claude-sdk
  honors a per-turn ``model_override`` via the inner executor's
  ``set_model``). In **advise** mode the verdict is SHADOW: persisted
  for telemetry, brain model unchanged.

Precedence: an explicit USER model pin (the session's persisted
``model_override``, set via ``/model`` or the web picker) BEATS the
advisor — the verdict is still recorded (``applied=False``) but the
brain runs on the user's choice, never the advisor's.

Scope pin (owner directive): model APPLICATION is CLAUDE-SDK ONLY. If
the brain harness is anything else, the advisor still judges and records
the verdict (advise-style labeling) but never applies it, and logs one
warning. This keeps the surface a single if-check, not a multi-harness
abstraction.

A judge may decide the turn is purely conversational and return
``None``: the advisor then skips the label write and applies nothing,
leaving the prior turn's selection in force. A failed label persist
degrades the same way. Mode is OFF by default: a spec without the marker
makes :func:`maybe_run_advisor` return ``None`` without any I/O.

Per-session ``cost_control_mode_override`` takes precedence
over the spec marker's mode (resolved in
:func:`~omnigent.runner.cost_judge.resolve_advisor_mode`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from omnigent.cost_plan import (
    ADVISOR_MODES,
    COST_CONTROL_PLAN_LABEL,
    AdvisorVerdict,
    describe_verdict,
    tier_rank,
    verdict_to_label_value,
)
from omnigent.runner.cost_judge import build_llm_judge, resolve_advisor_mode
from omnigent.runner.identity import (
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_TOKEN_HEADER,
)

_logger = logging.getLogger(__name__)

# Advisor-mode marker read from ``executor.config``.
ADVISOR_CONFIG_KEY = "cost_optimize"

# The only harness whose per-turn model the advisor APPLIES. The
# claude-sdk inner executor honors a per-turn ``model_override`` via
# ``set_model`` (no subprocess respawn); other harnesses get advise-style
# labeling only (owner-directed scope pin).
_APPLICABLE_HARNESS = "claude-sdk"

# Timeout for the one label-persist PATCH per advised turn.
_LABEL_PATCH_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class AdvisorConfig:
    """
    Parsed advisor configuration for one agent spec.

    :param tiers: Models-only tier catalog mapping tier name to model
        ids, e.g. ``{"cheap": ("m1",), "expensive": ("m2",)}``. The judge
        picks the brain model from this catalog; it is also the clamp
        source for a strayed pin.
    :param mode: Mode the advisor runs in: ``"optimize"`` (apply the
        verdict to the brain) or ``"advise"`` (shadow — record only).
    """

    tiers: dict[str, tuple[str, ...]]
    mode: str


def parse_advisor_config(executor_config: Mapping[str, Any] | None) -> AdvisorConfig | None:
    """
    Read the advisor marker out of ``executor.config``.

    Expected YAML shape::

        executor:
          config:
            cost_optimize:
              mode: advise
              advisor_model: databricks-claude-haiku-4-5
              tiers:
                cheap: [databricks-claude-haiku-4-5]
                medium: [databricks-claude-sonnet-4-6]
                expensive: [databricks-claude-opus-4-8]

    :param executor_config: The spec's ``executor.config`` dict, or
        ``None`` when the spec has no executor config.
    :returns: The parsed config, or ``None`` when the marker is absent,
        ``null``, or an explicit ``false`` opt-out (advisor off — the
        default).
    :raises ValueError: When the marker is present but malformed
        (non-mapping, EMPTY mapping, unknown ``mode``, empty or
        non-string ``tiers``). Opting in with a broken config — including
        ``cost_optimize: {}`` — fails loud rather than silently running
        unadvised.
    """
    raw = (executor_config or {}).get(ADVISOR_CONFIG_KEY)
    # Only absence, YAML null, and an explicit ``false`` opt-out mean OFF;
    # every other falsy value (``{}``, ``""``, ``0``) is a malformed opt-in.
    if raw is None or raw is False:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"executor.config.{ADVISOR_CONFIG_KEY} must be a mapping with "
            f"'tiers' (and optional 'mode'); got {type(raw).__name__}"
        )
    if not raw:
        raise ValueError(
            f"executor.config.{ADVISOR_CONFIG_KEY} is present but empty; "
            "set 'tiers' (or remove the key to disable the advisor)"
        )
    # "optimize" is the contract's default: omitting mode applies verdicts.
    mode = raw.get("mode", "optimize")
    if mode not in ADVISOR_MODES:
        raise ValueError(
            f"executor.config.{ADVISOR_CONFIG_KEY}.mode must be one of "
            f"{ADVISOR_MODES}; got {mode!r}"
        )
    raw_tiers = raw.get("tiers")
    if not isinstance(raw_tiers, Mapping) or not raw_tiers:
        raise ValueError(
            f"executor.config.{ADVISOR_CONFIG_KEY}.tiers must be a non-empty "
            "mapping of tier name to model-id list"
        )
    tiers: dict[str, tuple[str, ...]] = {}
    for name, models in raw_tiers.items():
        tier_rank(name)  # fail loud on unknown tier names
        if not isinstance(models, Sequence) or isinstance(models, str):
            raise ValueError(
                f"executor.config.{ADVISOR_CONFIG_KEY}.tiers[{name!r}] must be a list"
            )
        if not all(isinstance(m, str) and m for m in models):
            raise ValueError(
                f"executor.config.{ADVISOR_CONFIG_KEY}.tiers[{name!r}] must "
                "contain non-empty model-id strings"
            )
        tiers[name] = tuple(models)
    return AdvisorConfig(tiers=tiers, mode=mode)


def _databricks_profile_for_spec(spec: Any) -> str | None:  # type: ignore[explicit-any]  # structural spec stubs in tests
    """
    Resolve the Databricks profile the brain's gateway routing would use.

    Mirrors the claude-sdk spawn-env auth precedence
    (:func:`omnigent.runtime.workflow._build_claude_sdk_spawn_env`):
    provider-config default > spec auth > legacy spec profile > global
    ``auth:`` block — so the judge call rides the same Databricks gateway
    as the brain. claude-sdk is the resolution family because the advisor
    only ever applies to a claude-sdk brain (and the tier catalog is
    Claude-shaped by construction).

    :param spec: The resolved agent spec for the session.
    :returns: The profile name, e.g. ``"my-workspace"``, or ``None``
        (no Databricks routing configured, or resolution failed — the
        judge then relies on ambient credential resolution, fail-open).
    """
    try:
        from omnigent.runtime.workflow import (
            _load_global_auth,
            _resolve_provider_for_build,
        )
        from omnigent.spec.types import DatabricksAuth

        provider = _resolve_provider_for_build(spec, harness_type="claude-sdk")
        if provider is not None:
            # A non-databricks provider routes the brain elsewhere; the
            # judge then has no profile to ride (ambient resolution).
            return provider.profile if provider.kind == "databricks" else None
        executor = spec.executor
        legacy = (getattr(executor, "config", None) or {}).get("profile") or getattr(
            executor, "profile", None
        )
        auth = getattr(executor, "auth", None)
        if auth is None and not legacy:
            auth = _load_global_auth()
        if isinstance(auth, DatabricksAuth):
            return auth.profile or None
        if auth is not None:
            # Explicit non-Databricks auth (e.g. api_key) — no profile.
            return None
        return str(legacy) if legacy else None
    except Exception:  # noqa: BLE001 — advisor must never block the turn
        _logger.warning(
            "cost_advisor: Databricks profile resolution failed; "
            "judge will use ambient credentials",
            exc_info=True,
        )
        return None


class Judge(Protocol):
    """
    Pluggable per-turn judge the advisor drives.

    Implementations decide one :class:`AdvisorVerdict` for the turn — or
    ``None`` when the turn is purely conversational. The advisor owns
    persistence and application; judges only map a query to a verdict.
    The production :class:`~omnigent.runner.cost_judge.LLMJudge`
    implements this protocol.
    """

    async def judge(self, *, query: str, turn_anchor: str) -> AdvisorVerdict | None:
        """
        Produce the brain-model verdict for one user turn.

        :param query: The turn's user message text, e.g. ``"refactor the
            auth flow"``. May be empty for non-text turns.
        :param turn_anchor: Caller-sampled anchor for the verdict, e.g.
            an ISO timestamp.
        :returns: The verdict to persist (and, optimize mode, apply), or
            ``None`` for a conversational turn — the advisor then leaves
            any prior selection in force.
        """
        ...


@dataclass(frozen=True)
class AdvisorTurnResult:
    """
    What one advised turn produced.

    :param verdict: The persisted verdict; ``verdict.applied`` reflects
        whether the brain model was actually changed this turn.
    :param apply_model: The model the caller MUST run the brain on this
        turn (set on the harness request), or ``None`` to leave the
        brain model unchanged (advise mode, a user pin won, or a
        non-applicable harness).
    :param note_item: The turn-input item carrying the one-line system
        note, in the runner's history-item shape, or ``None`` (advise
        mode injects no note).
    """

    verdict: AdvisorVerdict
    apply_model: str | None
    note_item: dict[str, Any] | None


def _extract_query_text(turn_content: Sequence[Mapping[str, Any]]) -> str:
    """
    Join the text blocks of a turn's inbound content into the query.

    :param turn_content: The forwarded message content blocks, e.g.
        ``[{"type": "input_text", "text": "refactor the auth flow"}]``.
    :returns: Newline-joined text; empty string for non-text turns.
    """
    parts: list[str] = []
    for block in turn_content:
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _advisor_note_item(verdict: AdvisorVerdict) -> dict[str, Any]:  # type: ignore[explicit-any]  # JSON-shaped turn input item
    """
    Build the one-line system note announcing the applied model.

    Only optimize mode injects a note (advise injects nothing), so this
    is always called with an applied verdict.

    :param verdict: The applied verdict.
    :returns: A history-shaped message item whose text reads
        ``"[Cost advisor: this turn runs on <model> (<tier>)]"``.
    """
    text = f"[Cost advisor: this turn runs on {verdict.model} ({verdict.tier})]"
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def routing_decision_event(verdict: AdvisorVerdict) -> dict[str, Any]:  # type: ignore[explicit-any]  # JSON-shaped SSE event
    """
    Build the turn-start SSE event carrying the router's verdict.

    Shaped as a ``response.output_item.done`` carrying a
    ``routing_decision`` item so it rides the existing stream-relay
    pipeline end to end: the AP server's relay persists it as a durable,
    display-only transcript item (in arrival order, BEFORE the turn's
    assistant output) and forwards the same event live, and the web UI's
    block stream renders it as a muted chip the moment the turn begins.
    The item type is in :data:`~omnigent.entities.conversation.NON_CONTENT_ITEM_TYPES`
    and is not in the runner's harness-input allowlist, so the brain
    never sees it.

    The runner emits this at turn start, before any ``response.in_progress``,
    so the relay has no turn response_id yet — it stamps a fresh
    ``routing_*`` id, and the item renders as its own standalone line.

    :param verdict: The (already applied/shadowed) verdict for the turn.
    :returns: An SSE event dict, e.g.
        ``{"type": "response.output_item.done", "item": {"type":
        "routing_decision", "model": "...", "tier": "expensive",
        "applied": true, "rationale": "..."}}``.
    """
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "routing_decision",
            "model": verdict.model,
            "tier": verdict.tier,
            "applied": verdict.applied,
            "rationale": verdict.rationale,
        },
    }


async def maybe_run_advisor(
    *,
    spec: Any,  # type: ignore[explicit-any]  # structural spec stubs in tests
    conversation_id: str,
    turn_content: Sequence[Mapping[str, Any]],
    server_client: httpx.AsyncClient,
    turn_anchor: str,
    harness: str | None,
    user_model_override: str | None = None,
    cost_control_mode_override: str | None = None,
    judge: Judge | None = None,
) -> AdvisorTurnResult | None:
    """
    Run the cost advisor for one turn when the spec opts in.

    Judges the turn, persists the ``cost_control.plan`` label, and — in
    optimize mode on a claude-sdk brain with no user pin — reports the
    model the caller must apply. Returns ``None`` (turn runs unadvised)
    when:

    - *spec* is ``None`` or carries no :data:`ADVISOR_CONFIG_KEY` marker
      (advisor off, the default);
    - the resolved mode is off (``cost_control_mode_override`` is
      ``"off"``); no judge call is made;
    - the judge returns ``None`` (conversational turn) — BY DESIGN the
      label write and application are skipped, so the prior turn's
      selection stays;
    - the label persist fails — treated like a conversational turn (no
      application, prior selection stays), so the recorded and the
      applied model never diverge.

    A present-but-malformed marker raises (see
    :func:`parse_advisor_config`).

    :param spec: The resolved agent spec for the session.
    :param conversation_id: Session id, e.g. ``"conv_abc123"``.
    :param turn_content: This turn's inbound message content blocks.
    :param server_client: HTTP client pointed at the Omnigent server,
        used for the one label-persist PATCH.
    :param turn_anchor: Caller-sampled anchor for the verdict (item id or
        ISO timestamp) — the advisor never reads the clock itself.
    :param harness: The session's resolved brain harness, e.g.
        ``"claude-sdk"``. Application is claude-sdk only; any other value
        degrades to advise-style labeling (one warning).
    :param user_model_override: The session's persisted ``model_override``
        (a ``/model`` or web-picker pin), or ``None``. When set, it BEATS
        the advisor: the verdict is recorded but not applied.
    :param cost_control_mode_override: The session's per-session
        cost-control switch: ``"on"`` forces the spec mode
        (or ``"optimize"``), ``"off"`` disables the advisor for the
        session, ``None`` / absent defers to the spec marker. Takes
        precedence over the marker's mode.
    :param judge: Judge override; ``None`` builds the production
        :class:`~omnigent.runner.cost_judge.LLMJudge` from the spec's
        advisor config.
    :returns: The turn result (verdict + apply_model + note), or ``None``
        when the turn runs unadvised.
    :raises ValueError: When the spec's advisor marker is malformed.
    """
    if spec is None:
        return None
    config = parse_advisor_config(getattr(spec.executor, "config", None))
    if config is None:
        return None
    # Per-session override > spec marker. None => advisor off this turn.
    effective_mode = resolve_advisor_mode(config.mode, cost_control_mode_override)
    if effective_mode is None:
        _logger.info(
            "cost_advisor: session %s advisor disabled by override; skipping",
            conversation_id,
        )
        return None
    if judge is not None:
        effective_judge: Judge = judge
    else:
        effective_judge = build_llm_judge(
            tiers=config.tiers,
            executor_config=getattr(spec.executor, "config", None),
            connection=getattr(spec.executor, "connection", None),
            # Ride the same Databricks gateway as the brain — a bare
            # databricks-* judge model would otherwise route to the
            # default openai adapter and fail open every turn.
            databricks_profile=_databricks_profile_for_spec(spec),
        )
    verdict = await effective_judge.judge(
        query=_extract_query_text(turn_content),
        turn_anchor=turn_anchor,
    )
    if verdict is None:
        return None
    return await _finalize_advised_turn(
        verdict=verdict,
        mode=effective_mode,
        harness=harness,
        user_model_override=user_model_override,
        conversation_id=conversation_id,
        server_client=server_client,
    )


async def _finalize_advised_turn(
    *,
    verdict: AdvisorVerdict,
    mode: str,
    harness: str | None,
    user_model_override: str | None,
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> AdvisorTurnResult | None:
    """
    Decide application, persist the verdict label, and build the result.

    Split from :func:`maybe_run_advisor` so the judge-call half and the
    persist/apply half each stay focused. The apply decision is made BEFORE
    persisting so the label's ``applied`` flag matches what the runner does.

    :param verdict: The judge's (unapplied) verdict for the turn.
    :param mode: The effective advisor mode (``"advise"`` / ``"optimize"``).
    :param harness: The session's brain harness, e.g. ``"claude-sdk"``.
    :param user_model_override: The session's persisted user model pin, or
        ``None``.
    :param conversation_id: Session id, e.g. ``"conv_abc123"``.
    :param server_client: HTTP client for the label-persist PATCH.
    :returns: The turn result; the telemetry-label persist is best-effort
        (the chip + application carry the verdict even if it fails).
    """
    apply_model = _model_to_apply(
        verdict=verdict,
        mode=mode,
        harness=harness,
        user_model_override=user_model_override,
        conversation_id=conversation_id,
    )
    applied_verdict = AdvisorVerdict(
        tier=verdict.tier,
        model=verdict.model,
        applied=apply_model is not None,
        rationale=verdict.rationale,
        turn_anchor=verdict.turn_anchor,
    )
    persisted = await _persist_verdict_label(applied_verdict, conversation_id, server_client)
    if not persisted:
        return None
    _logger.info(
        "cost_advisor: session %s verdict %s applied=%s",
        conversation_id,
        describe_verdict(applied_verdict),
        applied_verdict.applied,
    )
    note_item = _advisor_note_item(applied_verdict) if apply_model is not None else None
    return AdvisorTurnResult(verdict=applied_verdict, apply_model=apply_model, note_item=note_item)


def _model_to_apply(
    *,
    verdict: AdvisorVerdict,
    mode: str,
    harness: str | None,
    user_model_override: str | None,
    conversation_id: str,
) -> str | None:
    """
    Decide whether (and to what) the brain model is changed this turn.

    :param verdict: The judge's verdict (unapplied).
    :param mode: The effective advisor mode (``"advise"`` /
        ``"optimize"``).
    :param harness: The session's brain harness, e.g. ``"claude-sdk"``.
    :param user_model_override: The session's persisted user model pin,
        or ``None``.
    :param conversation_id: Session id, for the scope-pin log.
    :returns: The model to run the brain on this turn, or ``None`` to
        leave it unchanged (advise mode, a user pin, or a non-applicable
        harness).
    """
    if mode != "optimize":
        return None
    if user_model_override:
        # Explicit user intent beats the advisor; verdict is shadow-recorded.
        _logger.info(
            "cost_advisor: session %s has a user model pin %r; not applying advisor verdict",
            conversation_id,
            user_model_override,
        )
        return None
    if harness != _APPLICABLE_HARNESS:
        # Owner-directed scope pin: model application is claude-sdk only.
        _logger.warning(
            "cost_advisor: session %s brain harness %r is not %r; recording the "
            "verdict but not applying it (application is claude-sdk only)",
            conversation_id,
            harness,
            _APPLICABLE_HARNESS,
        )
        return None
    return verdict.model


def _runner_identity_headers() -> dict[str, str]:
    """
    Build the headers proving runner identity for reserved-label writes.

    :returns: ``{X-Omnigent-Runner-Tunnel-Token: <token>}`` when the
        runner process carries its tunnel binding token (set by every
        CLI / host-daemon spawn path), else ``{}`` — single-user local
        servers accept the write without it, multi-user servers reject it
        and the turn degrades to unadvised.
    """
    raw_token = os.environ.get(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR)
    if raw_token is None or not raw_token.strip():
        return {}
    return {RUNNER_TUNNEL_TOKEN_HEADER: raw_token.strip()}


async def _persist_verdict_label(
    verdict: AdvisorVerdict,
    conversation_id: str,
    server_client: httpx.AsyncClient,
) -> bool:
    """
    Persist the verdict as the session's ``cost_control.plan`` label.

    The PATCH carries the runner's tunnel binding token (when the process
    has one) so multi-user servers can verify the write comes from the
    session's bound runner: ``cost_control.*`` is a reserved,
    advisor-owned label namespace that ordinary clients may not write
    (see ``update_session`` in :mod:`omnigent.server.routes.sessions`).

    :param verdict: The verdict to persist.
    :param conversation_id: Session id, e.g. ``"conv_abc123"``.
    :param server_client: HTTP client pointed at the Omnigent server.
    :returns: ``True`` on success; ``False`` (with a warning logged) when
        the PATCH failed — the caller then applies nothing so the
        recorded and the applied model never diverge.
    """
    try:
        resp = await server_client.patch(
            f"/v1/sessions/{conversation_id}",
            json={"labels": {COST_CONTROL_PLAN_LABEL: verdict_to_label_value(verdict)}},
            headers=_runner_identity_headers(),
            timeout=_LABEL_PATCH_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        _logger.warning(
            "cost_advisor: verdict label persist failed for %s (%s); running turn unadvised",
            conversation_id,
            exc,
        )
        return False
    if resp.status_code >= 400:
        # Log the response body too: the bare status code hid WHY a
        # deployed multi-user server 500'd this persist (the chip no
        # longer depends on it — it rides the routing_decision transcript
        # item — but the telemetry label and its failure mode must stay
        # diagnosable). Body is bounded so a large error page can't flood
        # the log.
        try:
            _body = resp.text[:500]
        except (UnicodeDecodeError, httpx.HTTPError):
            _body = "<unreadable response body>"
        _logger.warning(
            "cost_advisor: verdict label persist returned %d for %s; "
            "running turn unadvised. response body: %s",
            resp.status_code,
            conversation_id,
            _body,
        )
        return False
    return True
