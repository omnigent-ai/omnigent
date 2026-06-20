"""Built-in safety policies for common guardrails.

Provides ready-to-use policy callables that admins and users
can attach to sessions via the CRUD API without writing custom
Python. Each callable follows the :class:`PolicyEvent` →
:class:`PolicyResponse` contract.
"""

from __future__ import annotations

import re as _re
from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_ALLOW: PolicyResponse = {"result": "ALLOW"}

_SYS_OS_TOOLS = frozenset({"sys_os_read", "sys_os_write", "sys_os_edit", "sys_os_shell"})

# Claude Code and Codex native tool names surfaced via the PreToolUse /
# PostToolUse hook contract (see ``omnigent.native_policy_hook``).
# These bypass Omnigent' ``sys_os_*`` MCP tools and execute directly
# inside the CLI subprocess.
_NATIVE_OS_TOOLS = frozenset({"Bash", "Read", "Write", "Edit", "Glob", "Grep"})

# Cursor SDK native tool names surfaced via the preToolUse hook
# (see ``omnigent.inner.cursor_policy_hook``). Cursor uses ``Shell``
# for its terminal tool (not ``Bash``). ``Read`` / ``Write`` / ``Edit``
# are already in ``_NATIVE_OS_TOOLS`` above.
_CURSOR_NATIVE_OS_TOOLS = frozenset({"Shell"})

# Pi native tool names (lowercase), surfaced via the pi ``tool_call``
# extension hook (see ``omnigent.inner.pi_executor._gate_native_tool``).
# Pi runs these in-process and routes them through the same TOOL_CALL
# policy verdict — but under its own names, distinct from the
# Claude/Codex-cased ``_NATIVE_OS_TOOLS``. Pi uses the same argument keys
# as the Omnigent ``sys_os_*`` tools (``path`` / ``command``), so the
# previews below resolve without a Pi-specific arg branch.
_PI_NATIVE_OS_TOOLS = frozenset({"read", "bash", "write", "edit"})


# ── Rate limiting ────────────────────────────────────────────────────────────


def max_tool_calls_per_session(limit: int = 100) -> PolicyCallable:
    """Factory: deny after *limit* total tool calls in the session.

    Uses ``event["session_state"]`` to persist the counter across
    turns. Returns ``state_updates`` to increment the count on
    each tool call.

    :param limit: Maximum tool calls allowed across the entire
        session. Defaults to ``100``.
    :returns: A policy callable that DENYs after the limit.
    """

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate the session-wide rate limit.

        :param event: Policy event dict.
        :returns: DENY if over limit, ALLOW otherwise.
        """
        if event.get("type") != "tool_call":
            return _ALLOW
        state = event.get("session_state") or {}
        count = int(state.get("_policy_tool_call_count", 0))
        if count >= limit:
            return {
                "result": "DENY",
                "reason": f"Exceeded {limit} tool calls this session",
            }
        return {
            "result": "ALLOW",
            "state_updates": [
                {"key": "_policy_tool_call_count", "action": "increment", "value": 1},
            ],
        }

    return evaluate  # type: ignore[return-value]


# ── OS tool approval ────────────────────────────────────────────────────────


def ask_on_os_tools(event: PolicyEvent) -> PolicyResponse:
    """ASK for user approval before any file or shell tool call.

    Covers five tool-name families:

    - **Omnigent built-in OS tools** (``sys_os_read``,
      ``sys_os_write``, ``sys_os_edit``, ``sys_os_shell``).
    - **Claude Code native tools** (``Bash``, ``Read``, ``Write``,
      ``Edit``, ``Glob``, ``Grep``) — surfaced via the
      ``PreToolUse`` hook contract.
    - **Codex native tools** — uses the same ``PreToolUse`` hook
      contract with the same tool names (e.g. ``Bash``).
    - **Cursor SDK native tools** (``Shell``) — surfaced via the
      ``preToolUse`` hook (see ``cursor_policy_hook.py``). Cursor
      uses ``Shell`` instead of ``Bash`` for its terminal tool.
    - **Pi native tools** (``read``, ``bash``, ``write``, ``edit``)
      — surfaced via the pi ``tool_call`` extension hook. Lowercase
      and distinct from the Claude/Codex casing.

    Returns ASK so the user sees an approval prompt before the tool
    executes.

    :param event: Policy event dict.
    :returns: ASK if a file/shell tool is being called, ALLOW
        otherwise.
    """
    if event.get("type") != "tool_call":
        return _ALLOW
    data = event.get("data")
    if not isinstance(data, dict):
        return _ALLOW
    tool = data.get("name", "")
    _all_os_tools = (
        _SYS_OS_TOOLS | _NATIVE_OS_TOOLS | _CURSOR_NATIVE_OS_TOOLS | _PI_NATIVE_OS_TOOLS
    )
    if tool in _all_os_tools:
        args = data.get("arguments", {})
        # Build a short preview of what the tool is doing.
        if tool in ("sys_os_shell", "Bash", "bash", "Shell"):
            preview = args.get("command", "") if isinstance(args, dict) else ""
        elif tool in ("Grep", "Glob"):
            preview = args.get("pattern", "") if isinstance(args, dict) else ""
        else:
            # Omnigent tools use ``path``; Claude native tools use ``file_path``.
            preview = (
                (args.get("path") or args.get("file_path", "")) if isinstance(args, dict) else ""
            )
        return {
            "result": "ASK",
            "reason": f"Agent wants to call {tool}({preview!r}). Approve?",
        }
    return _ALLOW


# ── Policy tool approval ───────────────────────────────────────────────────


def ask_on_add_policy(event: PolicyEvent) -> PolicyResponse:
    """ASK for user approval before ``sys_add_policy`` executes.

    Agents must not silently install new policies on a session.
    This callable is injected unconditionally by the builder so
    every ``sys_add_policy`` call parks for approval — the user
    sees what the agent wants to add and can approve or deny.

    :param event: Policy event dict.
    :returns: ASK if ``sys_add_policy`` is being called, ALLOW
        otherwise.
    """
    if event.get("type") != "tool_call":
        return _ALLOW
    data = event.get("data")
    if not isinstance(data, dict):
        return _ALLOW
    if data.get("name") != "sys_add_policy":
        return _ALLOW
    args = data.get("arguments")
    if isinstance(args, dict):
        policy_name = args.get("name", "")
        handler = args.get("handler", "")
        preview = f"{policy_name} ({handler})" if handler else policy_name
    else:
        preview = ""
    return {
        "result": "ASK",
        "reason": f"Agent wants to add policy: {preview}. Approve?",
    }


# ── Skill blocking ──────────────────────────────────────────────────────────

# Omnigent runner tools that load skills in non-native (SDK) harnesses.
_SKILL_TOOLS = frozenset({"load_skill", "read_skill_file"})

# Claude Code's native ``Skill`` tool, fired via ``PreToolUse`` hook and
# evaluated server-side at ``POST /v1/sessions/{id}/policies/evaluate``.
# The tool takes a ``skill`` argument with the skill name.
_NATIVE_SKILL_TOOL = "Skill"


def block_skills(blocked: list[str]) -> PolicyCallable:
    """Factory: deny skill loading for specific skill names.

    Intercepts three loading paths:

    1. **AP runner tools** — ``load_skill`` and ``read_skill_file`` tool
       calls dispatched by non-native (SDK) harnesses.
    2. **Native ``Skill`` tool** — Claude Code's built-in ``Skill`` tool,
       intercepted via the ``PreToolUse`` command hook which POSTs to
       the Omnigent server's ``/policies/evaluate`` endpoint. This is how
       ``block_skills`` enforces on native Claude Code and Codex
       harnesses — there is no ``load_skill`` runner tool in native
       mode.
    3. **Slash commands** — ``/skill-name`` commands submitted by the
       user (or UI). The Omnigent server evaluates these at the ``request``
       phase as synthetic ``"/<name> <args>"`` text via
       ``_build_skill_slash_command_policy_body``.

    Matching is case-insensitive.

    :param blocked: Skill names to block, e.g.
        ``["code-review", "deploy"]``.
    :returns: A policy callable that DENYs blocked skill loads.
    """
    blocked_lower = frozenset(name.lower() for name in blocked)

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate whether the skill load should be blocked.

        :param event: Policy event dict.
        :returns: DENY if the skill name is blocked, ALLOW otherwise.
        """
        event_type = event.get("type")

        # ── Path 1 & 2: tool_call interception ────────────────────────
        if event_type == "tool_call":
            data = event.get("data")
            if not isinstance(data, dict):
                return _ALLOW
            tool = data.get("name", "")
            args = data.get("arguments")
            if not isinstance(args, dict):
                return _ALLOW

            # Path 1: Omnigent runner tools (load_skill / read_skill_file).
            if tool in _SKILL_TOOLS:
                # load_skill uses "name"; read_skill_file uses "skill_name"
                skill_name = args.get("name") if tool == "load_skill" else args.get("skill_name")
                if skill_name and skill_name.lower() in blocked_lower:
                    return {
                        "result": "DENY",
                        "reason": f"Skill '{skill_name}' is blocked by policy",
                    }
                return _ALLOW

            # Path 2: Claude Code / Codex native Skill tool.
            # Fired via PreToolUse hook → Omnigent /policies/evaluate.
            if tool == _NATIVE_SKILL_TOOL:
                skill_name = args.get("skill")
                if skill_name and skill_name.lower() in blocked_lower:
                    return {
                        "result": "DENY",
                        "reason": f"Skill '{skill_name}' is blocked by policy",
                    }
                return _ALLOW

            return _ALLOW

        # ── Path 3: request phase for slash-command skill loads ───────
        # The Omnigent server converts ``/skill-name args`` into a synthetic
        # user message ``"/<name> <args>"`` and evaluates it at the
        # REQUEST phase.  Match ``/<blocked-name>`` at the start.
        if event_type == "request":
            data = event.get("data")
            text = data if isinstance(data, str) else ""
            if text.startswith("/"):
                # Extract the command name: first token after "/".
                # ``split(None, ...)`` drops empty tokens, so a bare "/"
                # or a slash followed only by whitespace ("/   ") yields
                # an empty list — guard against IndexError.
                tokens = text[1:].split(None, 1)
                command = tokens[0] if tokens else ""
                if command.lower() in blocked_lower:
                    return {
                        "result": "DENY",
                        "reason": f"Skill '{command}' is blocked by policy",
                    }
            return _ALLOW

        return _ALLOW

    return evaluate  # type: ignore[return-value]


# ── Sandbox enforcement ────────────────────────────────────────────────────

_AGENT_START_TOOL = "sys_agent_start"

# Keys from ``OSEnvSandboxSpec`` that can be overridden by the policy.
# If the admin supplies a key not in this set, the policy silently
# ignores it — prevents injection of unsupported fields.
_SANDBOX_OVERRIDE_KEYS = frozenset(
    {
        "type",
        "read_paths",
        "write_paths",
        "write_files",
        "allow_network",
        "cwd_allow_hidden",
        "env_passthrough",
        "egress_rules",
        "egress_allow_private_destinations",
        "cwd_hidden_scan_max_entries",
        "cwd_hidden_scan_overflow",
    }
)


def enforce_sandbox(
    sandbox_type: str = "linux_bwrap",
    allow_network: bool = True,
    write_paths: list[str] | None = None,
    read_paths: list[str] | None = None,
    env_passthrough: list[str] | None = None,
) -> PolicyCallable:
    """Factory: force a sandbox configuration on every agent start.

    Intercepts the synthetic ``__agent_start`` tool call emitted by the
    runner before spawning an agent subprocess.  On match, returns ALLOW
    with a ``data`` payload whose ``sandbox`` field overrides the agent's
    declared sandbox config.  Fields not specified in the policy are
    inherited from the agent's existing config (merge, not replace).

    If the agent has no sandbox config at all, one is created from scratch
    using the policy's parameters.

    :param sandbox_type: Sandbox backend to force, e.g.
        ``"linux_bwrap"``, ``"darwin_seatbelt"``, ``"none"``.
        Defaults to ``"linux_bwrap"``.
    :param allow_network: Whether to allow network access.
        Defaults to ``True``.
    :param write_paths: Writable paths to enforce, e.g. ``["."]``.
        ``None`` means inherit the agent's existing ``write_paths``.
    :param read_paths: Read-only paths to enforce.
        ``None`` means inherit the agent's existing ``read_paths``.
    :param env_passthrough: Env vars to allow through the sandbox.
        ``None`` means inherit the agent's existing ``env_passthrough``.
    :returns: A policy callable that forces sandbox config on
        ``__agent_start`` tool calls.
    """
    # Build the override dict — only include keys the admin explicitly set.
    override: dict[str, Any] = {
        "type": sandbox_type,
        "allow_network": allow_network,
    }
    if write_paths is not None:
        override["write_paths"] = write_paths
    if read_paths is not None:
        override["read_paths"] = read_paths
    if env_passthrough is not None:
        override["env_passthrough"] = env_passthrough

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate whether the agent start should have sandbox forced.

        :param event: Policy event dict.
        :returns: ALLOW with forced sandbox ``data`` on
            ``__agent_start``, plain ALLOW otherwise.
        """
        if event.get("type") != "tool_call":
            return _ALLOW
        data = event.get("data")
        if not isinstance(data, dict):
            return _ALLOW
        tool = data.get("name", "")
        if tool != _AGENT_START_TOOL:
            return _ALLOW

        args = data.get("arguments")
        if not isinstance(args, dict):
            args = {}

        # Merge: existing sandbox config as base, policy overrides on top.
        current_sandbox: dict[str, Any] = args.get("sandbox") or {}
        forced_sandbox = {
            k: v for k, v in {**current_sandbox, **override}.items() if k in _SANDBOX_OVERRIDE_KEYS
        }

        return {
            "result": "ALLOW",
            "data": {
                "name": _AGENT_START_TOOL,
                "arguments": {**args, "sandbox": forced_sandbox},
            },
        }

    return evaluate  # type: ignore[return-value]


# ── PII detection on LLM requests ────────────────────────────────────────────

# Built-in PII categories with their regex patterns. The UI shows
# these as a multi-select checklist; authors never write raw regex.
_PII_CATEGORY_PATTERNS: dict[str, _re.Pattern[str]] = {
    "ssn": _re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": _re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "email": _re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    # Phone: international (+cc ...) and common local formats
    # (US, UK, JP, DE, etc.) in a single pattern.
    "phone": _re.compile(
        r"\+\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{1,4}[-.\s]?\d{1,9}\b"
        r"|\b\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
        r"|\b0\d{1,4}[-.\s]?\d{2,4}[-.\s]?\d{3,4}\b"
    ),
}

# Labels shown in the UI checklist for each category.
_PII_CATEGORY_LABELS: dict[str, str] = {
    "ssn": "Social Security Number (US)",
    "credit_card": "Credit Card Number",
    "email": "Email Address",
    "phone": "Phone Number",
}


def deny_pii_in_llm_request(
    pii_types: list[str] | None = None,
    action: str = "DENY",
) -> PolicyCallable:
    """Factory: scan the system prompt preview in ``llm_request`` for PII.

    Selects PII categories from the built-in set and scans the
    ``system_prompt_preview`` field of the LLM request data. When
    any pattern matches, the policy returns the configured *action*
    (``DENY`` by default) with a reason naming the matched category.

    Only fires on ``llm_request`` events — all other phases pass
    through with ALLOW.

    :param pii_types: List of PII category keys to scan for, e.g.
        ``["ssn", "email"]``. Defaults to all built-in categories
        when ``None`` or empty. Unknown keys are silently ignored.
    :param action: The verdict to emit on match — ``"DENY"`` or
        ``"ASK"``. Defaults to ``"DENY"``.
    :returns: A policy callable that scans LLM request prompts
        for PII patterns.
    """
    if pii_types:
        selected = {k: v for k, v in _PII_CATEGORY_PATTERNS.items() if k in pii_types}
    else:
        # None or empty list → all categories enabled.
        selected = dict(_PII_CATEGORY_PATTERNS)

    effective_action = action if action in ("DENY", "ASK") else "DENY"

    def evaluate(event: PolicyEvent) -> PolicyResponse:
        """Evaluate user input or LLM request for PII matches.

        Fires on two phases so PII is caught regardless of harness:

        - ``request``: user message text, enforced universally on
          the Omnigent server for every harness (including supervisor,
          native).
        - ``llm_request``: full LLM call metadata (system prompt +
          last user message), enforced via the harness callback
          for executors that support it.

        :param event: Policy event dict.
        :returns: DENY/ASK if PII found, ALLOW otherwise.
        """
        event_type = event.get("type")

        if event_type == "request":
            # REQUEST phase: ``data`` is the user message string.
            text = event.get("data")
            if isinstance(text, str):
                return _scan_text(text)
            # Content-block list (multimodal input).
            if isinstance(text, list):
                for block in text:
                    if isinstance(block, dict):
                        t = block.get("text", "")
                        if isinstance(t, str):
                            result = _scan_text(t)
                            if result is not _ALLOW:
                                return result
            return _ALLOW

        if event_type == "llm_request":
            # LLM_REQUEST phase: scan system prompt + user message.
            data = event.get("data")
            if not isinstance(data, dict):
                return _ALLOW
            for field in ("system_prompt_preview", "last_user_message"):
                text = data.get(field, "")
                if isinstance(text, str) and text:
                    result = _scan_text(text)
                    if result is not _ALLOW:
                        return result
            return _ALLOW

        return _ALLOW

    def _scan_text(text: str) -> PolicyResponse:
        """Scan a text string against selected PII patterns.

        :param text: The string to scan.
        :returns: DENY/ASK if PII found, ALLOW otherwise.
        """
        for category, regex in selected.items():
            match = regex.search(text)
            if match:
                label = _PII_CATEGORY_LABELS.get(category, category)
                return {
                    "result": effective_action,
                    "reason": (f"PII detected ({label}): '{match.group()[:20]}...'"),
                }
        return _ALLOW

    return evaluate  # type: ignore[return-value]


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.safety.max_tool_calls_per_session",
        "kind": "factory",
        "name": "Limit Tool Calls Per Session",
        "description": "Limits the total number of tool calls across the entire session "
        "using session_state to persist the counter",
        "params_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum tool calls allowed across the session",
                    "default": 100,
                },
            },
            "required": ["limit"],
        },
    },
    {
        "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        "kind": "callable",
        "name": "Require Approval for File & Shell Operations",
        "description": "Asks for user approval before any file or shell tool call — "
        "covers Omnigent sys_os_* tools, Claude Code native tools "
        "(Bash, Read, Write, Edit, Glob, Grep), and Codex native tools",
        "params_schema": None,
    },
    {
        "handler": "omnigent.policies.builtins.safety.block_skills",
        "kind": "factory",
        "name": "Block Specific Skills",
        "description": "Prevents the agent from loading specific skills. "
        "Intercepts load_skill/read_skill_file (non-native harnesses) and the "
        "native Skill tool (claude-native/codex-native via PreToolUse hook)",
        "params_schema": {
            "type": "object",
            "properties": {
                "blocked": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Skill names to block (case-insensitive)",
                },
            },
            "required": ["blocked"],
        },
    },
    {
        "handler": "omnigent.policies.builtins.safety.enforce_sandbox",
        "kind": "factory",
        "name": "Enforce Sandbox on Agent Start",
        "description": "Forces a specific sandbox configuration (e.g. linux_bwrap) "
        "on every agent start. Intercepts the synthetic __agent_start tool call "
        "and overrides the agent's sandbox config.",
        "params_schema": {
            "type": "object",
            "properties": {
                "sandbox_type": {
                    "type": "string",
                    "description": "Sandbox backend to force (linux_bwrap, darwin_seatbelt, none)",
                    "default": "linux_bwrap",
                },
                "allow_network": {
                    "type": "boolean",
                    "description": "Whether to allow network access",
                    "default": True,
                },
                "write_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Writable paths to enforce (null inherits agent's config)",
                },
                "read_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Read-only paths to enforce (null inherits agent's config)",
                },
                "env_passthrough": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Env vars to allow through the sandbox "
                    "(null inherits agent's config)",
                },
            },
        },
    },
    {
        "handler": "omnigent.policies.builtins.safety.deny_pii_in_llm_request",
        "kind": "factory",
        "name": "Deny PII in LLM Requests",
        "description": "Scans user messages and LLM request prompts for PII "
        "(SSN, credit card, email, phone). Works with all harnesses.",
        "params_schema": {
            "type": "object",
            "properties": {
                "pii_types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["ssn", "credit_card", "email", "phone"],
                    },
                    "uniqueItems": True,
                    "description": "PII categories to scan for. Leave empty to enable all.",
                    "default": ["ssn", "credit_card", "email", "phone"],
                },
                "action": {
                    "type": "string",
                    "enum": ["DENY", "ASK"],
                    "description": "Action when PII is detected",
                    "default": "DENY",
                },
            },
        },
    },
]
