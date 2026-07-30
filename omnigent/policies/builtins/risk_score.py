"""Built-in *global risk score* policy (MCP-agnostic).

A single configurable policy-callable factory, :func:`risk_score_policy`, that
implements the "session risk score" pattern from the sample-policies wishlist:

- **Accrue risk** as the agent takes risky actions. Two sources:
  - *Tool calls* — each call to a configured tool adds points
    (``tool_points``), e.g. a ``web_search`` adds 10. This source needs no
    special support from any tool, so it works out of the box.
  - *Tool results* — a result carrying a sensitive data-classification label
    adds points (``sensitive_labels``), e.g. a result tagged
    ``"Highly Confidential"`` adds 30. The label is read out of the tool's
    result payload (scanning the keys in ``label_keys``), so this works with
    *any* MCP server that annotates its results with a classification field —
    configure ``sensitive_labels`` to match whatever values that server emits.
    (Not every MCP labels its output; when none do, leave ``sensitive_labels``
    empty and rely on ``tool_points`` alone.)
- **Gate sensitive actions** once the accrued score crosses ``threshold``:
  configured ``guarded_tools`` (e.g. ``gmail_message_send``) escalate from
  ALLOW to **ASK** (default) or **DENY**, forcing human oversight on a session
  that has touched enough risky material.

The running score lives in the engine's persisted ``session_state`` under
``state_key`` (default ``"risk_score"``) and is bumped via ``increment``
state-updates, so it accumulates across turns. A per-actor starting offset
(``initial_scores_by_actor``, keyed on ``context.actor.run_as``) lets you
"start a session at higher risk" for specific users without writing Python —
something a single global ``threshold`` can't express. (A flat, all-users
offset is intentionally omitted: it would be mathematically identical to
lowering ``threshold`` by the same amount.)

**Tool matching is MCP-agnostic.** A configured tool name matches the raw event
tool when they are equal *or* the raw name ends with ``"__" + name`` — so
``"gmail_message_send"`` matches ``"mcp__google__gmail_message_send"``,
``"google__gmail_message_send"``, and the bare ``"gmail_message_send"`` alike,
with no server-prefix configuration.

This factory must be referenced via ``function: {path, arguments}`` with a
non-empty ``arguments`` block (the registry declares it ``kind: "factory"``).

YAML usage::

    policies:
      session_risk:
        type: function
        function:
          path: omnigent.policies.builtins.risk_score.risk_score_policy
          arguments:
            threshold: 50
            tool_points: {web_search: 10, fetch: 5}
            # Only if your MCP annotates results with these exact label values.
            sensitive_labels: {"Highly Confidential": 30, RESTRICTED: 30}
            guarded_tools: [gmail_message_send, drive_permission_create]
            escalate_action: ASK
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from omnigent.policies.schema import PolicyEvent, PolicyResponse, StateUpdateEntry

# ── Constants ─────────────────────────────────────────────────────────────────

# Default session-state key holding the running score. Public: it surfaces in
# the conversation's persisted ``session_state`` and callers may inspect it.
DEFAULT_RISK_STATE_KEY = "risk_score"

# Result-payload keys whose string value is treated as a data-classification /
# DLP sensitivity label. Drive results use ``label_classification``; the others
# cover common variants so the policy works across MCP servers.
_DEFAULT_LABEL_KEYS: tuple[str, ...] = (
    "label_classification",
    "classification",
    "sensitivity",
    "sensitivity_label",
    "dlp_label",
)

# Max recursion depth when scanning a tool-result payload for labels. Bounds the
# walk so a crafted, deeply-nested MCP response can't exhaust the Python stack;
# real results are only a few levels deep. Truncating is fail-safe here — an
# unscanned label simply adds no risk (it never *lowers* the score).
_MAX_RESULT_SCAN_DEPTH = 20

# The two verdicts a guarded tool may escalate to. Validated at factory build.
_VALID_ESCALATIONS = frozenset({"ASK", "DENY"})

_ALLOW: PolicyResponse = {"result": "ALLOW"}


# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RiskCfg:
    """
    Resolved risk-score configuration shared by the evaluator's phase handlers.

    :param threshold: Score at/above which ``guarded_tools`` escalate.
    :param tool_points: Canonical tool name → points added on each call, e.g.
        ``{"web_search": 10}``.
    :param sensitive_labels: Lower-cased classification label → points added
        when a ``tool_result`` carries that label, e.g.
        ``{"highly confidential": 30}``.
    :param guarded_tools: Canonical tool names that escalate once the score is
        at/above ``threshold``, e.g. ``{"gmail_message_send"}``.
    :param escalate_action: Verdict for a guarded tool over threshold —
        ``"ASK"`` or ``"DENY"``.
    :param initial_scores_by_actor: ``context.actor.run_as`` email → starting
        offset for that user, e.g. ``{"intern@example.com": 40}``.
    :param state_key: ``session_state`` key holding the running score.
    :param label_keys: Result-payload keys inspected for a classification label.
    :param reason: Human-readable prefix on ASK / DENY escalations.
    """

    threshold: int
    tool_points: dict[str, int]
    sensitive_labels: dict[str, int]
    guarded_tools: frozenset[str]
    escalate_action: str
    initial_scores_by_actor: dict[str, int]
    state_key: str
    label_keys: tuple[str, ...]
    reason: str


# ── Shared helpers ────────────────────────────────────────────────────────────


def _tool_matches(raw_tool: str, name: str) -> bool:
    """
    Check whether a raw event tool name matches a configured (canonical) name.

    MCP-agnostic: matches on exact equality or on a ``"__"``-delimited suffix,
    so ``"gmail_message_send"`` matches ``"mcp__google__gmail_message_send"``
    and the bare name alike, without configuring server prefixes.

    :param raw_tool: Tool name from the event, e.g.
        ``"mcp__google__gmail_message_send"``.
    :param name: Configured canonical tool name, e.g. ``"gmail_message_send"``.
    :returns: ``True`` when *raw_tool* refers to *name*.
    """
    return raw_tool == name or raw_tool.endswith(f"__{name}")


def _points_for_tool(raw_tool: str, tool_points: dict[str, int]) -> int:
    """
    Sum the configured points for a tool call.

    :param raw_tool: Tool name from the event, e.g. ``"mcp__google__fetch"``.
    :param tool_points: Canonical tool name → points mapping.
    :returns: Total points (0 when no configured name matches). When several
        configured names match the same raw tool, their points sum — configure
        distinct names to avoid surprise.
    """
    return sum(points for name, points in tool_points.items() if _tool_matches(raw_tool, name))


def _is_guarded(raw_tool: str, guarded_tools: frozenset[str]) -> bool:
    """
    Check whether a raw event tool is one of the guarded (gated) tools.

    :param raw_tool: Tool name from the event, e.g.
        ``"mcp__google__gmail_message_send"``.
    :param guarded_tools: Configured canonical guarded-tool names.
    :returns: ``True`` when *raw_tool* matches any guarded name.
    """
    return any(_tool_matches(raw_tool, name) for name in guarded_tools)


def _parse_result_payload(data: Any) -> Any:  # type: ignore[explicit-any]
    """
    Extract and parse the tool output from a ``tool_result`` event.

    Handles the server-side shape (``{"result": "<json-or-text>"}``, value
    stringified) and the runner-side shape (raw string / already-structured).

    :param data: The raw ``event["data"]`` on a ``tool_result`` event, e.g.
        ``{"result": '{"label_classification": "internal"}'}``.
    :returns: The parsed structure when the payload was JSON, else the raw value.
    """
    inner = data
    if isinstance(data, dict) and "result" in data:
        inner = data["result"]
    if isinstance(inner, str):
        with contextlib.suppress(ValueError, TypeError):
            return json.loads(inner)
    return inner


def _collect_labels(payload: Any, label_keys: tuple[str, ...], max_depth: int) -> set[str]:  # type: ignore[explicit-any]
    """
    Recursively collect data-classification labels from a result payload.

    Walks dicts/lists up to *max_depth* and gathers the lower-cased string value
    of any key in *label_keys*. Depth-bounded so a crafted, deeply-nested MCP
    response can't exhaust the recursion stack; unscanned labels simply add no
    risk (fail-safe — the score never drops).

    :param payload: Parsed ``tool_result`` payload — dict, list, or scalar, e.g.
        ``{"label_classification": "Highly Confidential"}``.
    :param label_keys: Keys whose string value is a classification label.
    :param max_depth: Remaining levels to descend; decremented per recursion. At
        ``0`` the walk stops.
    :returns: Set of lower-cased label strings found under any *label_keys* key.
    """
    if max_depth <= 0:
        return set()
    labels: set[str] = set()
    if isinstance(payload, dict):
        for key, nested in payload.items():
            if key in label_keys and isinstance(nested, str) and nested.strip():
                labels.add(nested.strip().lower())
            labels.update(_collect_labels(nested, label_keys, max_depth - 1))
    elif isinstance(payload, list):
        for item in payload:
            labels.update(_collect_labels(item, label_keys, max_depth - 1))
    return labels


def _current_score(event: PolicyEvent, cfg: _RiskCfg) -> int:
    """
    Compute the session's effective risk score for this event.

    Effective score = this actor's starting offset + the accumulated value
    persisted in ``session_state[state_key]``.

    :param event: The policy event (read for ``session_state`` and the actor).
    :param cfg: Resolved risk configuration.
    :returns: The effective integer score (≥ the actor's offset).
    """
    state = event.get("session_state") or {}
    accumulated = int(state.get(cfg.state_key, 0))
    actor = (event.get("context") or {}).get("actor") or {}
    run_as = actor.get("run_as") or ""
    actor_offset = cfg.initial_scores_by_actor.get(run_as, 0)
    return actor_offset + accumulated


def _increment(state_key: str, points: int) -> PolicyResponse:
    """
    Build an ALLOW response that adds *points* to the running score.

    :param state_key: ``session_state`` key to increment.
    :param points: Points to add (must be > 0; callers gate on this).
    :returns: An ALLOW :class:`PolicyResponse` with one ``increment``
        state-update.
    """
    update: StateUpdateEntry = {"key": state_key, "action": "increment", "value": points}
    return {"result": "ALLOW", "state_updates": [update]}


# ── Phase handlers ────────────────────────────────────────────────────────────


def _decide_tool_call(event: PolicyEvent, cfg: _RiskCfg) -> PolicyResponse | None:
    """
    Gate (when over threshold) or score a ``tool_call`` event.

    Order of precedence: a guarded tool whose session score is already at/above
    ``threshold`` escalates (ASK/DENY) and does **not** score — the escalation
    is the response, and on ASK the engine withholds state-updates anyway.
    Otherwise, if the tool has configured points, accrue them; else abstain.

    :param event: The ``tool_call`` policy event.
    :param cfg: Resolved risk configuration.
    :returns: An escalation, an ALLOW-with-increment, or ``None`` to abstain.
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    raw_tool = data.get("name", "")
    if not raw_tool:
        return None

    if _is_guarded(raw_tool, cfg.guarded_tools):
        score = _current_score(event, cfg)
        if score >= cfg.threshold:
            # ``escalate_action`` is validated to "ASK"/"DENY" at factory build,
            # so the cast to the PolicyResponse ``result`` Literal is sound.
            return cast(
                PolicyResponse,
                {
                    "result": cfg.escalate_action,
                    "reason": (
                        f"{cfg.reason} Session risk score {score} ≥ threshold "
                        f"{cfg.threshold}; {raw_tool} requires review."
                    ),
                },
            )

    points = _points_for_tool(raw_tool, cfg.tool_points)
    if points > 0:
        return _increment(cfg.state_key, points)
    return None


def _decide_tool_result(event: PolicyEvent, cfg: _RiskCfg) -> PolicyResponse | None:
    """
    Accrue risk when a ``tool_result`` carries a sensitive classification label.

    Parses the result payload, collects any classification labels, and adds the
    **maximum** matching label's points (a single result usually carries one
    classification; taking the max avoids double-counting nested copies of the
    same doc).

    :param event: The ``tool_result`` policy event.
    :param cfg: Resolved risk configuration.
    :returns: An ALLOW-with-increment when a sensitive label matched, else
        ``None`` to abstain.
    """
    if not cfg.sensitive_labels:
        return None
    payload = _parse_result_payload(event.get("data"))
    labels = _collect_labels(payload, cfg.label_keys, _MAX_RESULT_SCAN_DEPTH)
    matched = [cfg.sensitive_labels[label] for label in labels if label in cfg.sensitive_labels]
    if not matched:
        return None
    return _increment(cfg.state_key, max(matched))


# ── Factory ───────────────────────────────────────────────────────────────────


def risk_score_policy(
    *,
    threshold: int = 50,
    tool_points: dict[str, int] | None = None,
    sensitive_labels: dict[str, int] | None = None,
    guarded_tools: list[str] | None = None,
    escalate_action: str = "ASK",
    initial_scores_by_actor: dict[str, int] | None = None,
    state_key: str = DEFAULT_RISK_STATE_KEY,
    label_keys: list[str] | None = None,
    reason: str = "Elevated session risk.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """
    Build a global session-risk-score policy callable.

    The returned callable accrues a risk score from tool calls and sensitive
    tool results, and escalates guarded tools to ASK/DENY once the score crosses
    ``threshold``. State persists across turns via ``session_state``.

    :param threshold: Score at/above which ``guarded_tools`` escalate. Defaults
        to ``50``.
    :param tool_points: Canonical tool name → points added on each call, e.g.
        ``{"web_search": 10, "fetch": 5}``. ``None`` means no per-call scoring.
    :param sensitive_labels: Data-classification label → points added when a
        ``tool_result`` carries that label, e.g.
        ``{"Highly Confidential": 30, "RESTRICTED": 30}``. Matching is
        case-insensitive. ``None`` means no label-based scoring.
    :param guarded_tools: Canonical tool names gated once the score reaches
        ``threshold``, e.g. ``["gmail_message_send"]``. ``None`` means nothing
        is gated (pure scorer).
    :param escalate_action: Verdict for a guarded tool over threshold —
        ``"ASK"`` (default, human approval) or ``"DENY"`` (hard block).
    :param initial_scores_by_actor: ``context.actor.run_as`` email → starting
        offset, e.g. ``{"contractor@example.com": 40}`` to start that user
        closer to the threshold. ``None`` means no per-actor offset. (A flat,
        all-users offset is intentionally omitted — it would be identical to
        lowering ``threshold``.)
    :param state_key: ``session_state`` key holding the running score. Defaults
        to ``"risk_score"``.
    :param label_keys: Result-payload keys inspected for a classification label.
        ``None`` uses the defaults (``label_classification``, ``classification``,
        ``sensitivity``, ``sensitivity_label``, ``dlp_label``).
    :param reason: Human-readable prefix on ASK / DENY escalations.
    :returns: A one-argument policy callable.
    :raises ValueError: If ``escalate_action`` is not ``"ASK"`` or ``"DENY"``.
    """
    normalized_escalation = escalate_action.strip().upper()
    if normalized_escalation not in _VALID_ESCALATIONS:
        raise ValueError(
            f"risk_score_policy: escalate_action must be one of {sorted(_VALID_ESCALATIONS)}, "
            f"got {escalate_action!r}"
        )
    cfg = _RiskCfg(
        threshold=threshold,
        tool_points=dict(tool_points or {}),
        # Lower-case label keys once so matching against collected labels is
        # case-insensitive without per-event normalization.
        sensitive_labels={k.strip().lower(): v for k, v in (sensitive_labels or {}).items()},
        guarded_tools=frozenset(guarded_tools or []),
        escalate_action=normalized_escalation,
        initial_scores_by_actor=dict(initial_scores_by_actor or {}),
        state_key=state_key,
        label_keys=tuple(label_keys) if label_keys is not None else _DEFAULT_LABEL_KEYS,
        reason=reason,
    )

    def _evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Route a risk event: score/gate tool calls, score sensitive results.

        :param event: The policy event.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain.
        """
        phase = event.get("type")
        if phase == "tool_call":
            return _decide_tool_call(event, cfg)
        if phase == "tool_result":
            return _decide_tool_result(event, cfg)
        return None

    return _evaluate


# ── Registry ──────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [  # type: ignore[explicit-any]
    {
        "handler": "omnigent.policies.builtins.risk_score.risk_score_policy",
        "kind": "factory",
        "name": "Session Risk Score",
        "description": (
            "Accrues a per-session risk score from risky tool calls and from "
            "tool results carrying a sensitive data-classification label, then "
            "escalates configured sensitive tools to ASK (or DENY) once the score "
            "crosses a threshold. MCP-agnostic; score persists across turns via "
            "session_state."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "threshold": {
                    "type": "integer",
                    "description": "Score at/above which guarded_tools escalate.",
                    "default": 50,
                },
                "tool_points": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                    "description": "Tool name -> points added on each call "
                    '(e.g. {"web_search": 10}).',
                },
                "sensitive_labels": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                    "description": "Data-classification label -> points added when a "
                    "tool result carries it (case-insensitive, e.g. "
                    '{"Highly Confidential": 30}).',
                },
                "guarded_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tool names gated once the score reaches threshold "
                    '(e.g. ["gmail_message_send"]).',
                },
                "escalate_action": {
                    "type": "string",
                    "enum": ["ASK", "DENY"],
                    "description": "Verdict for a guarded tool over threshold.",
                    "default": "ASK",
                },
                "initial_scores_by_actor": {
                    "type": "object",
                    "additionalProperties": {"type": "integer"},
                    "description": "Actor run_as email -> starting offset "
                    '(e.g. {"contractor@example.com": 40}).',
                },
                "state_key": {
                    "type": "string",
                    "description": "session_state key holding the running score.",
                    "default": DEFAULT_RISK_STATE_KEY,
                },
                "label_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Result-payload keys inspected for a classification "
                    "label (default: label_classification, classification, "
                    "sensitivity, sensitivity_label, dlp_label).",
                },
                "reason": {
                    "type": "string",
                    "description": "Human-readable prefix on ASK / DENY escalations.",
                },
            },
        },
    },
]
