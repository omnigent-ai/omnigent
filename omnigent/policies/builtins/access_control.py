"""Built-in access-control policies based on formal security models.

:func:`bell_lapadula` implements the Bell-LaPadula model (BLP), the
classical mandatory-access-control model for **confidentiality**:

- **No read up** (Simple Security Property): an agent may not invoke a tool
  whose classification level exceeds the agent's clearance.  A "DENY" fires
  on ``tool_call`` before the tool runs.

- **No write down** (Star Property / ``*``-property): once the agent has received
  a result from a tool at level *L*, it may not subsequently invoke a tool
  classified below *L* whose purpose is to write or exfiltrate data.  The
  policy tracks the agent's "contamination high-water mark" in
  ``session_state`` (updated on ``tool_result``) and gates outbound write-tool
  calls on ``tool_call``.

Levels are operator-defined strings ordered by position in the ``levels``
list (index 0 = least sensitive).  Only tools explicitly listed in
``tool_levels`` are subject to BLP enforcement; unclassified tools are
allowed freely.

YAML usage::

    policies:
      blp:
        type: function
        function:
          path: omnigent.policies.builtins.access_control.bell_lapadula
          arguments:
            levels: [public, internal, confidential, secret]
            clearance: internal
            tool_levels:
              # read sources
              sys_read_internal_docs: internal
              sys_read_hr_data: confidential
              # write / exfil sinks
              sys_os_shell: public
              sys_os_write: public
              external_api_call: internal
            write_tools:
              - sys_os_shell
              - sys_os_write
              - external_api_call

The factory must be referenced via ``function: {path, arguments}`` with a
non-empty ``arguments`` block.
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

# session_state key storing the agent's current read high-water mark (the
# classification level of the highest-classified tool result seen so far).
_BLP_READ_MARK_KEY = "_blp_read_mark"


def _tool_matches(raw_tool: str, name: str) -> bool:
    """Check whether a raw event tool name matches a configured canonical name.

    MCP-agnostic: matches on exact equality or on a ``"__"``-delimited suffix,
    so ``"sys_read_hr_data"`` matches ``"mcp__omnigent__sys_read_hr_data"`` and
    the bare name alike, without requiring server prefixes in the config.

    :param raw_tool: Tool name from the event, e.g.
        ``"mcp__omnigent__sys_read_hr_data"``.
    :param name: Configured canonical tool name, e.g. ``"sys_read_hr_data"``.
    :returns: ``True`` when *raw_tool* refers to *name*.
    """
    return raw_tool == name or raw_tool.endswith(f"__{name}")


def bell_lapadula(
    levels: list[str],
    clearance: str,
    tool_levels: dict[str, str],
    write_tools: list[str] | None = None,
) -> PolicyCallable:
    """Factory: enforce Bell-LaPadula mandatory access control.

    Implements the two core BLP properties:

    - **No read up** — a ``tool_call`` whose tool is classified above the
      agent's *clearance* is DENYed before it runs.
    - **No write down** — once the agent has received a ``tool_result`` from
      a tool at level *L*, any subsequent ``tool_call`` to a *write_tool*
      classified below *L* is DENYed.

    Both properties operate only on tools explicitly listed in *tool_levels*;
    unclassified tools are allowed freely under both properties.

    :param levels: Ordered list of classification level names, least sensitive
        first, e.g. ``["public", "internal", "confidential", "secret"]``.
        The position in this list determines the numeric level used for
        comparisons.  Every name in *tool_levels*, *clearance*, and
        *write_tools* must appear here.
    :param clearance: The agent's clearance level, e.g. ``"internal"``.  The
        agent may read (call) tools at or below this level.
    :param tool_levels: Mapping of tool name → classification level, e.g.
        ``{"sys_read_hr_data": "confidential", "sys_os_shell": "public"}``.
        Tools absent from this mapping are unclassified and unrestricted.
    :param write_tools: Tool names considered "write / exfiltration" sinks for
        the no-write-down check, e.g. ``["sys_os_shell", "sys_os_write"]``.
        Only write-classified tools participate in the ``*``-property check.
        ``None`` or ``[]`` disables no-write-down enforcement (only no-read-up
        applies).
    :returns: A policy callable implementing BLP on ``tool_call`` and
        ``tool_result`` phases.
    :raises ValueError: If *clearance* or any value in *tool_levels* is not in
        *levels*, or if any entry in *write_tools* names a tool not in
        *tool_levels*.
    """
    if not levels:
        raise ValueError("bell_lapadula: levels must be a non-empty list")
    level_index: dict[str, int] = {name: i for i, name in enumerate(levels)}

    if clearance not in level_index:
        raise ValueError(f"bell_lapadula: clearance {clearance!r} not in levels {levels}")
    clearance_idx = level_index[clearance]

    # Validate and resolve tool levels.
    resolved: dict[str, int] = {}
    for tool, lvl in tool_levels.items():
        if lvl not in level_index:
            raise ValueError(
                f"bell_lapadula: tool_levels[{tool!r}] = {lvl!r} not in levels {levels}"
            )
        resolved[tool] = level_index[lvl]

    write_set: frozenset[str] = frozenset(write_tools or [])
    for wt in write_set:
        if wt not in resolved:
            raise ValueError(f"bell_lapadula: write_tools entry {wt!r} is not in tool_levels")

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate BLP properties for one tool_call or tool_result event.

        - ``tool_call``: check no-read-up, then (for write tools) no-write-down.
        - ``tool_result``: advance the contamination high-water mark when the
          tool's level exceeds the current mark.
        - All other phases abstain (ALLOW).

        :param event: Policy event dict.
        :returns: DENY when a BLP property is violated; ALLOW with optional
            ``state_updates`` when the read mark advances; ALLOW otherwise.
        """
        phase = event.get("type")
        tool_name: str = event.get("target") or ""

        # ── resolve tool name (MCP-prefix-agnostic) ──────────────────────
        # ``resolved`` keys are canonical names (e.g. "sys_read_hr_data").
        # The raw event target may carry an MCP server prefix such as
        # "mcp__omnigent__sys_read_hr_data".  _tool_matches handles both.
        matched_canonical: str | None = next(
            (name for name in resolved if _tool_matches(tool_name, name)), None
        )

        # ── tool_result: advance the read high-water mark ─────────────────
        if phase == "tool_result":
            if matched_canonical is None:
                return _ALLOW  # unclassified tool — no mark change
            tool_level_idx = resolved[matched_canonical]
            state = event.get("session_state") or {}
            current_mark: int = int(state.get(_BLP_READ_MARK_KEY) or 0)
            if tool_level_idx <= current_mark:
                return _ALLOW  # mark already at or above this level
            return {
                "result": "ALLOW",
                "state_updates": [
                    {
                        "key": _BLP_READ_MARK_KEY,
                        "action": "set",
                        "value": tool_level_idx,
                    }
                ],
            }

        # ── tool_call: enforce no-read-up and no-write-down ───────────────
        if phase != "tool_call":
            return _ALLOW

        if matched_canonical is None:
            return _ALLOW  # unclassified tool — no restriction

        tool_level_idx = resolved[matched_canonical]

        # No read up: agent clearance must be >= tool's level.
        if tool_level_idx > clearance_idx:
            tool_level_name = levels[tool_level_idx]
            return {
                "result": "DENY",
                "reason": (
                    f"Bell-LaPadula no-read-up violation: tool {tool_name!r} is "
                    f"classified {tool_level_name!r} but agent clearance is "
                    f"{clearance!r}. The agent may not access data above its "
                    f"clearance level."
                ),
            }

        # No write down: if this is a write sink, its level must be >= read mark.
        if any(_tool_matches(tool_name, wt) for wt in write_set):
            state = event.get("session_state") or {}
            read_mark: int = int(state.get(_BLP_READ_MARK_KEY) or 0)
            if tool_level_idx < read_mark:
                read_mark_name = levels[read_mark]
                tool_level_name = levels[tool_level_idx]
                return {
                    "result": "DENY",
                    "reason": (
                        f"Bell-LaPadula no-write-down violation: the agent has "
                        f"read data classified {read_mark_name!r} but is attempting "
                        f"to write to {tool_name!r} which is classified "
                        f"{tool_level_name!r}. Writing to a lower-classified "
                        f"sink would leak confidential data."
                    ),
                }

        return _ALLOW

    return evaluate  # type: ignore[return-value]


# ── Registry ──────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.access_control.bell_lapadula",
        "kind": "factory",
        "name": "Bell-LaPadula",
        "description": (
            "Enforces the Bell-LaPadula mandatory access-control model for "
            "confidentiality. "
            "No-read-up: blocks tool calls whose classification level exceeds "
            "the agent's clearance (prevents reading data above clearance). "
            "No-write-down: once the agent has read data at level L, blocks "
            "calls to write-sink tools classified below L (prevents leaking "
            "high-classified data to lower-classified outputs). "
            "Only tools listed in tool_levels are subject to enforcement; "
            "unclassified tools are allowed freely."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "levels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ordered classification levels, least sensitive first, "
                        'e.g. ["public", "internal", "confidential", "secret"].'
                    ),
                },
                "clearance": {
                    "type": "string",
                    "description": (
                        "The agent's clearance level. May only read tools at or below this level."
                    ),
                },
                "tool_levels": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Map of tool name → classification level. Tools absent "
                        "from this map are unclassified and unrestricted."
                    ),
                },
                "write_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tool names considered write / exfiltration sinks for "
                        "the no-write-down check. Must be a subset of tool_levels "
                        "keys. Omit or pass [] to disable no-write-down "
                        "(only no-read-up will apply)."
                    ),
                },
            },
            "required": ["levels", "clearance", "tool_levels"],
        },
    },
]
