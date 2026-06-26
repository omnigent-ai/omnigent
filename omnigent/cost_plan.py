"""Advisor verdict contract for per-turn brain-model selection.

THE interface between the per-turn cost advisor
(:mod:`omnigent.runner.cost_advisor`) and the session label that
records what it decided: the advisor serializes an
:class:`AdvisorVerdict` into ONE conversation label
(:data:`COST_CONTROL_PLAN_LABEL`, JSON-encoded) and readers parse it
back with :func:`parse_verdict`. Anything that needs to agree on "what
did the advisor decide for this turn's brain" goes through this module
— never through ad-hoc dicts.

The advisor v3 contract (this module): a per-user-turn LLM judge picks
ONE model for the ORCHESTRATOR'S OWN BRAIN, sized to the turn's
difficulty (difficult coding → expensive tier, medium knowledge work →
medium, trivial → cheap). The verdict names a single tier + a single
concrete model drawn from that tier's configured list. The orchestrator
brain still freely decides how many sub-agents to spawn, which workers,
and which worker models — that is correctness (the model-family guard),
not cost, and the advisor never touches it.

What retired with v3: the multi-entry tier PARTITION of v2 (a turn now
runs on ONE brain model; a mixed-difficulty query takes the MAX tier
its parts need), the ``sys_session_send`` dispatch guard
(``cost_guard``), and the advise-mode divergence telemetry (nothing to
diverge from once the verdict targets the brain, not dispatches).

This module is pure: no I/O, no ambient clock (callers pass the turn
anchor in); its only project import is the shared model-spelling
canonicalizer from :mod:`omnigent.model_override`, so tier ranking and
the brain-application layer agree on which spellings name the same
model.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

# Label-key prefix of the policy-owned cost-control namespace. Labels
# under it are advisor/runner-written telemetry; the server rejects them
# in client-supplied label writes (see ``update_session`` /
# ``create_session`` in :mod:`omnigent.server.routes.sessions`).
COST_CONTROL_LABEL_NAMESPACE = "cost_control."

# Conversation label carrying the JSON-encoded advisor verdict for the
# session's most recent advised turn.
COST_CONTROL_PLAN_LABEL = "cost_control.plan"

# Schema version serialized into the label. v3 is the single-verdict
# brain-model shape; v1 (per-worker draft) and v2 (tier partition) never
# carry meaning here. parse_verdict version-gates strictly on v3 and
# tolerates a legacy v2 label in an old session by returning None rather
# than crashing the reader.
PLAN_VERSION = 3

# Tier names in ascending cost order: cheap < medium < expensive.
TIER_ORDER: tuple[str, ...] = ("cheap", "medium", "expensive")

# Advisor enforcement modes: "optimize" applies the verdict to the
# brain; "advise" runs the judge in shadow (records the verdict, leaves
# the brain model unchanged).
ADVISOR_MODES: tuple[str, ...] = ("advise", "optimize")


def reserved_cost_control_keys(labels: Mapping[str, str]) -> tuple[str, ...]:
    """
    Return the policy-owned ``cost_control.*`` keys present in *labels*.

    :param labels: A label mapping from a client request body, e.g.
        ``{"cost_control.plan": "{...}", "team": "ml"}``.
    :returns: The keys under :data:`COST_CONTROL_LABEL_NAMESPACE`, in
        mapping order, e.g. ``("cost_control.plan",)``. Empty when the
        mapping touches no reserved keys.
    """
    return tuple(key for key in labels if key.startswith(COST_CONTROL_LABEL_NAMESPACE))


def tier_rank(tier: str) -> int:
    """
    Return the cost rank of a tier name (lower = cheaper).

    :param tier: A tier name from :data:`TIER_ORDER`, e.g. ``"cheap"``.
    :returns: The tier's index in :data:`TIER_ORDER`, e.g. ``0``.
    :raises ValueError: When *tier* is not a known tier name — an
        unknown tier is a configuration error that must fail loud, not
        silently rank as cheapest or priciest.
    """
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        raise ValueError(f"unknown tier {tier!r}; expected one of {TIER_ORDER}") from None


@dataclass(frozen=True, kw_only=True)
class AdvisorVerdict:
    """
    A per-turn brain-model selection produced by the cost advisor.

    The advisor picks ONE model (drawn from one tier's configured list)
    for the orchestrator's OWN brain this turn, sized to the turn's
    difficulty. ``applied`` records whether the brain actually ran on
    that model: ``True`` in optimize mode (the override took effect),
    ``False`` in advise mode (shadow telemetry, brain unchanged) or when
    a user model pin beat the advisor.

    :param version: Serialization schema version, e.g. ``3``
        (:data:`PLAN_VERSION`).
    :param tier: The difficulty tier the judge assigned the turn, one of
        :data:`TIER_ORDER`, e.g. ``"expensive"``.
    :param model: The concrete brain model the judge chose from
        ``tier``'s configured list, e.g.
        ``"databricks-claude-opus-4-8"``.
    :param applied: ``True`` when the brain ran on :attr:`model` this
        turn (optimize mode, no user pin); ``False`` when the verdict was
        recorded but not applied (advise mode, or a user model pin won).
    :param rationale: One-sentence judge explanation, surfaced in the
        UI and (optimize mode) in the in-turn system note. The judge
        always produces a string (:mod:`omnigent.runner.cost_judge`
        substitutes a fallback when the model returns none); ``None`` is
        reserved for the serialize/parse round-trip's degenerate case,
        where even an empty rationale would not fit the labels column.
    :param turn_anchor: Caller-supplied anchor tying the verdict to the
        turn that produced it (an item id or ISO timestamp), e.g.
        ``"2026-06-10T12:00:00+00:00"``. Callers sample the clock; this
        module never does.
    """

    version: int = PLAN_VERSION
    tier: str
    model: str
    applied: bool
    rationale: str | None
    turn_anchor: str


# Conversation labels persist into a varchar(256) column; values longer
# than this are rejected wholesale by Postgres.
_LABEL_VALUE_MAX_LEN = 256

# Suffix marking a rationale trimmed to fit the labels column.
_TRIM_MARKER = "..."


def verdict_to_label_value(verdict: AdvisorVerdict) -> str:
    """
    Serialize a verdict into the :data:`COST_CONTROL_PLAN_LABEL` value.

    Long judge rationales are trimmed so the value fits the labels
    column (an oversized value fails the whole write, and the verdict
    then never surfaces). The full rationale still reaches the UI via the
    ``routing_decision`` transcript item.

    Trimming measures SERIALIZED length, not raw character count.
    :func:`json.dumps` defaults to ``ensure_ascii=True``, so a non-ASCII
    char escapes to ``\\uXXXX`` (6 chars) and a quote/backslash to 2;
    counting raw chars dropped a short non-ASCII rationale wholesale (to
    ``null``) even with column budget to spare. The trim keeps the
    longest rationale prefix that fits, then appends
    :data:`_TRIM_MARKER`; only the degenerate case (the other fields
    alone overflow the column) yields a ``null`` rationale.

    :param verdict: The verdict to serialize.
    :returns: Compact JSON, e.g. ``'{"applied":true,"model":
        "databricks-claude-opus-4-8","rationale":"...","tier":
        "expensive","turn_anchor":"...","version":3}'``, at most
        :data:`_LABEL_VALUE_MAX_LEN` characters.
    """
    payload = {
        "version": verdict.version,
        "tier": verdict.tier,
        "model": verdict.model,
        "applied": verdict.applied,
        "rationale": verdict.rationale,
        "turn_anchor": verdict.turn_anchor,
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if len(serialized) <= _LABEL_VALUE_MAX_LEN or not verdict.rationale:
        return serialized

    # Serialized chars left for the rationale's escaped CONTENT, after the
    # rest of the object and the trim marker take their share. base_len is
    # measured with an empty rationale, so it already counts every other
    # field's escaping plus the rationale value's two surrounding quotes.
    base_payload = dict(payload)
    base_payload["rationale"] = ""
    base_len = len(json.dumps(base_payload, separators=(",", ":"), sort_keys=True))
    budget = _LABEL_VALUE_MAX_LEN - base_len - len(_TRIM_MARKER)

    kept = ""
    if budget > 0:
        # Largest prefix whose escaped content fits the budget. Escaped
        # length is monotonic in prefix length, so binary-search it.
        # ``json.dumps(s)`` wraps the value in quotes, hence the ``- 2``.
        lo, hi = 0, len(verdict.rationale)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(json.dumps(verdict.rationale[:mid])) - 2 <= budget:
                lo = mid
            else:
                hi = mid - 1
        kept = verdict.rationale[:lo]

    payload["rationale"] = (kept + _TRIM_MARKER) if kept else None
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def parse_verdict(labels: Mapping[str, str]) -> AdvisorVerdict | None:
    """
    Parse an :class:`AdvisorVerdict` out of a conversation-label mapping.

    Version-gates strictly on v3. A legacy v2 label (a tier partition
    written by an older runner into a session that predates this build)
    is TOLERATED: it parses to ``None`` instead of raising, so old
    sessions keep loading — the advisor simply has no v3 verdict to
    surface for them. Any other malformed v3 label fails loud, since a
    corrupt current-version label is a real bug, not legacy data.

    :param labels: The conversation's labels, e.g.
        ``{"cost_control.plan": '{"version": 3, ...}'}``.
    :returns: The parsed v3 verdict; ``None`` when the label is absent
        (no advised turn yet) or is a tolerated legacy v2 label. A parsed
        verdict's ``rationale`` is ``None`` when the writer had to drop it
        to fit the column (see :func:`verdict_to_label_value`).
    :raises ValueError: When a v3-shaped label is malformed (bad JSON,
        wrong field types, unknown tier). A ``null`` rationale is NOT
        malformed: the writer emits it in the degenerate case, so it
        round-trips rather than raising.
    """
    raw = labels.get(COST_CONTROL_PLAN_LABEL)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{COST_CONTROL_PLAN_LABEL} label is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{COST_CONTROL_PLAN_LABEL} label must be a JSON object")
    version = payload.get("version")
    if version != PLAN_VERSION:
        # Legacy v2 (tier partition) / v1 in an old session: tolerate by
        # ignoring rather than crashing the reader. Only the current
        # schema is parsed; older shapes carry no v3 verdict.
        return None
    tier = payload.get("tier")
    if not isinstance(tier, str) or tier not in TIER_ORDER:
        raise ValueError(
            f"{COST_CONTROL_PLAN_LABEL} verdict has tier {tier!r}; expected one of {TIER_ORDER}"
        )
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise ValueError(f"{COST_CONTROL_PLAN_LABEL} verdict needs a non-empty string model")
    applied = payload.get("applied")
    if not isinstance(applied, bool):
        raise ValueError(f"{COST_CONTROL_PLAN_LABEL} verdict needs a boolean applied field")
    rationale = payload.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        raise ValueError(
            f"{COST_CONTROL_PLAN_LABEL} verdict needs a string or null rationale field"
        )
    turn_anchor = payload.get("turn_anchor")
    if not isinstance(turn_anchor, str):
        raise ValueError(f"{COST_CONTROL_PLAN_LABEL} verdict needs a string turn_anchor field")
    return AdvisorVerdict(
        version=PLAN_VERSION,
        tier=tier,
        model=model,
        applied=applied,
        rationale=rationale,
        turn_anchor=turn_anchor,
    )


def describe_verdict(verdict: AdvisorVerdict) -> str:
    """
    Render a verdict as the one-line summary used in notes and logs.

    :param verdict: The verdict to describe.
    :returns: Summary text, e.g.
        ``"databricks-claude-opus-4-8 (expensive)"``.
    """
    return f"{verdict.model} ({verdict.tier})"
