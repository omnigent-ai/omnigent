"""
``sys_terminal_*`` tool builtins for the AP-side ToolManager.

Five tools backing the spec's ``terminals:`` block:

- ``sys_terminal_launch`` — start a configured tmux session.
- ``sys_terminal_send`` — type text + key chords.
- ``sys_terminal_read`` — capture pane state (with optional scrollback).
- ``sys_terminal_list`` — enumerate the conversation's terminals.
- ``sys_terminal_close`` — kill a session and remove it.

All five are thin wrappers around
:class:`omnigent.terminals.TerminalRegistry`. The registry owns
the per-conversation map; tools translate JSON arguments and
forward.

Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.3, these tools register
on AP-side :class:`omnigent.tools.manager.ToolManager` and flow
to the LLM through the existing action_required dispatch path
(same plumbing as ``sys_os_*``). Construction takes the spec
(for terminal-name / override-flag lookup) and the registry
singleton; both are passed in at registration time by
:meth:`ToolManager._register_terminal_tools`.

Cwd resolution at launch follows the §4.6 precedence list — see
:meth:`SysTerminalLaunchTool._resolve_anchor_cwd` for the trusted
containment anchor. The LLM ``cwd`` override is applied and
containment-checked separately by the inner guard, never folded into
the anchor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance
from omnigent.spec.types import AgentSpec
from omnigent.terminals import TerminalRegistry
from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)
_PLACEHOLDER_CWDS = (None, "", ".", "./")


def _has_meaningful_cwd(cwd: str | None) -> bool:
    """
    Return whether *cwd* is an explicit path rather than the default placeholder.

    ``None``, ``""``, ``"."``, and ``"./"`` mean "use the
    framework-provided workspace" throughout the terminal bridge.
    """
    return cwd not in _PLACEHOLDER_CWDS


class _CloseFailed(Exception):
    """
    Internal flag for ``SysTerminalCloseTool`` — surfacing a tmux
    teardown failure across the lock-acquisition boundary without
    leaking the original ``RuntimeError`` / ``OSError`` past the
    structured JSON envelope the caller expects.
    """


@dataclass(frozen=True)
class _ValidatedLaunchArgs:
    """Validated launch-tool arguments ready to drive ``TerminalRegistry.launch``.

    Returned by :meth:`SysTerminalLaunchTool._validate_launch_args`
    on the success path. Using a dataclass instead of a tuple
    keeps call-sites readable and lets future fields land without
    breaking unpacking.

    :param terminal_name: The spec terminal name from
        ``parsed["terminal"]``.
    :param session_key: The per-launch session key from
        ``parsed["session"]``.
    :param terminal_spec: The :class:`TerminalEnvSpec` for
        ``terminal_name``.
    :param cwd_override: LLM-supplied cwd, or ``None``.
    :param sandbox_override: LLM-supplied sandbox, or ``None``.
    """

    terminal_name: str
    session_key: str
    terminal_spec: TerminalEnvSpec
    cwd_override: str | None
    sandbox_override: str | None


@dataclass(frozen=True)
class _ResolvedInstance:
    """A running :class:`TerminalInstance` plus its parsed tool args + lock.

    Returned by :func:`_resolve_running_instance` on the success
    path. Using a dataclass keeps call-sites self-documenting:
    ``resolved.instance`` / ``resolved.parsed`` is clearer than
    ``resolved[0]`` / ``resolved[1]``.

    :param instance: The live :class:`TerminalInstance`.
    :param parsed: The parsed JSON arguments dict for the tool.
    :param lock: The per-instance ``threading.Lock`` from
        :meth:`TerminalRegistry.get_instance_lock`. Callers MUST
        acquire this around the ``asyncio.run(instance.X())`` call
        to serialize concurrent tmux ops on the same instance —
        without it, a ``send(text=X, keys="Enter")`` call can
        interleave its 2 tmux subprocess invocations with another
        thread's send. See ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §9.1.
    """

    instance: TerminalInstance
    parsed: dict[str, Any]
    lock: threading.Lock


def _format_launch_envelope(
    validated: _ValidatedLaunchArgs,
    instance: TerminalInstance,
    was_running: bool,
) -> str:
    """
    Render the JSON success envelope for ``sys_terminal_launch``.

    Centralizes the success-shape so :meth:`SysTerminalLaunchTool._spawn_and_format`
    stays under the 40-line ceiling. The envelope mirrors the
    legacy inner Session ``terminal_launch`` shape
    (``omnigent/inner/session.py:3242``).

    :param validated: Validated launch args used to populate the
        envelope's terminal/session fields.
    :param instance: The live :class:`TerminalInstance` whose
        ``socket_path`` and ``os_env`` are surfaced.
    :param was_running: When ``True``, return ``status:
        "already_running"`` so the LLM doesn't double-launch.
    :returns: JSON-encoded success envelope.
    """
    return json.dumps(
        {
            "terminal": validated.terminal_name,
            "session": validated.session_key,
            "tmux_socket": str(instance.socket_path),
            "has_os_env": instance.os_env is not None,
            "status": "already_running" if was_running else "launched",
        }
    )


def _materialize_terminal_spec_for_launch(
    terminal_spec: TerminalEnvSpec,
    resolved_cwd: str | None,
) -> TerminalEnvSpec:
    """
    Return a clone of ``terminal_spec`` with its ``os_env.cwd``
    populated when the spec had a placeholder.

    Without this clone, the inner ``create_terminal_instance``
    falls back to ``os.getcwd()`` when ``terminal_spec.os_env`` is
    set but its ``cwd`` is ``None``/``.``/``./`` — landing the tmux
    session in AP's process cwd, NOT the §4.6-resolved workspace.
    The synthesized parent_os_env from
    :func:`_synthesize_parent_os_env` doesn't help here: the inner
    builder prefers ``terminal_spec.os_env`` over the parent
    whenever the terminal declares its own.

    By cloning the terminal spec with a populated cwd, the
    framework-resolved §4.6 path always wins over inner defaults,
    regardless of which os_env the inner builder reaches for.

    :param terminal_spec: The :class:`TerminalEnvSpec` from the
        agent spec's ``terminals:`` block.
    :param resolved_cwd: The §4.6-resolved cwd. ``None`` means no
        tier matched (e.g. test contexts with no workspace) — fall
        back to the original spec.
    :returns: Either the input ``terminal_spec`` unchanged
        (when it already names a meaningful cwd or
        ``resolved_cwd`` is ``None``), or a fresh
        :class:`TerminalEnvSpec` clone with the cwd populated.
    """
    if resolved_cwd is None:
        return terminal_spec
    ts_os_env = terminal_spec.os_env
    if not isinstance(ts_os_env, OSEnvSpec):
        # ``inherit`` sentinel or ``None`` — the inner builder will
        # fall back to ``parent_os_env_spec``, which the caller has
        # already synthesized via :func:`_synthesize_parent_os_env`
        # to carry ``resolved_cwd``.
        return terminal_spec
    if _has_meaningful_cwd(ts_os_env.cwd):
        # Spec already names a real cwd — leave it alone.
        return terminal_spec
    # Clone the os_env with the resolved cwd, then clone the
    # terminal spec around it. Avoid mutating the original (sub-
    # agents and other tools share the same TerminalEnvSpec
    # instance). Use ``replace`` on the os_env so every field
    # (notably ``start_in_scratch``) is preserved automatically.
    from dataclasses import replace

    new_os_env = replace(ts_os_env, cwd=resolved_cwd)
    return replace(terminal_spec, os_env=new_os_env)


def _synthesize_parent_os_env(
    spec_os_env: OSEnvSpec | None,
    resolved_cwd: str | None,
) -> OSEnvSpec | None:
    """
    Build the ``parent_os_env`` for ``TerminalRegistry.launch``.

    Applies the §4.6 cwd-resolution result to the spec's os_env so
    the inner :func:`build_terminal_os_env_spec` inherits the
    correct cwd through normal channels — without tripping the
    ``allow_cwd_override`` gate. The gate is for LLM-supplied
    per-call overrides; the framework's own §4.6 resolution is
    trusted and must flow as if the spec already had it.

    The function is conservative: it returns ``spec_os_env``
    unchanged whenever the spec already has a meaningful cwd
    (anything other than ``None``, ``""``, ``"."``, or ``"./"``).
    Only when the spec lacks a meaningful cwd does it produce a *new*
    :class:`OSEnvSpec` that injects ``resolved_cwd``. This avoids
    accidentally clobbering an explicit spec setting.

    :param spec_os_env: The agent spec's ``os_env`` block, or
        ``None`` if the spec doesn't declare one.
    :param resolved_cwd: The cwd from §4.6 resolution, or ``None``
        if no tier matched (e.g. test contexts with no workspace).
        When ``None``, the function leaves the os_env alone — the
        inner builder will fall back to its own defaults.
    :returns: An :class:`OSEnvSpec` to pass as ``parent_os_env``,
        or ``None`` to leave the inner builder's default behavior
        intact.
    """
    if resolved_cwd is None:
        return spec_os_env
    if spec_os_env is None:
        # Spec has no os_env; synthesize a minimal one so the
        # workspace fallback flows through the inner builder.
        return OSEnvSpec(type="caller_process", cwd=resolved_cwd)
    if not _has_meaningful_cwd(spec_os_env.cwd):
        # Spec has the placeholder; substitute the resolved cwd.
        # Use ``replace`` so we don't mutate the original spec and
        # so every field (notably ``start_in_scratch``) is preserved
        # automatically as the dataclass evolves.
        from dataclasses import replace

        return replace(spec_os_env, cwd=resolved_cwd)
    # Spec already names an explicit cwd; leave it alone.
    return spec_os_env


# ── JSON Schemas ──────────────────────────────────────────────
# Mirror the legacy inner Session ``sys_terminal_*`` schemas so
# agents in the wild see an unchanged tool surface under Omnigent mode.


_LAUNCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terminal": {
            "type": "string",
            "description": "Terminal name from spec.terminals (e.g. 'bash').",
        },
        "session": {
            "type": "string",
            "description": (
                "Per-instance session key, free-form (e.g. 's1', 'auth-work'). "
                "Different session keys give independent tmux sessions of the "
                "same terminal."
            ),
        },
        "cwd": {
            "type": "string",
            "description": (
                "Optional cwd override. Honored only if the terminal spec sets "
                "`allow_cwd_override: true`; otherwise rejected with an error."
            ),
        },
        "sandbox": {
            "type": "string",
            "enum": ["none", "linux_bwrap"],
            "description": (
                "Optional sandbox-type override. Honored only if the terminal "
                "spec sets `allow_sandbox_override: true`; otherwise rejected."
            ),
        },
    },
    "required": ["terminal", "session"],
}


_SEND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terminal": {"type": "string"},
        "session": {"type": "string"},
        "text": {
            "type": "string",
            "description": "Literal text to type (sent via tmux send-keys -l).",
        },
        "keys": {
            "type": "string",
            "description": (
                "Tmux key names to press after the text, space-separated. "
                "Defaults to 'Enter'. Set to '' to type without pressing any "
                "key. Examples: 'Enter', 'Tab', 'C-c', 'Escape', 'C-d', 'Up'."
            ),
            "default": "Enter",
        },
    },
    "required": ["terminal", "session"],
}


_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terminal": {"type": "string"},
        "session": {"type": "string"},
        "scrollback": {
            "type": "integer",
            "default": 0,
            "description": (
                "Number of scrollback lines to include above the visible pane. "
                "Default 0 (visible pane only)."
            ),
        },
    },
    "required": ["terminal", "session"],
}


_LIST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
}


_CLOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "terminal": {"type": "string"},
        "session": {"type": "string"},
    },
    "required": ["terminal", "session"],
}


def _validate_session_required_args(args: dict[str, Any]) -> dict[str, str] | None:
    """
    Validate the ``terminal`` + ``session`` args common to most tools.

    :param args: The parsed JSON argument dict.
    :returns: An error dict ready to JSON-encode if validation
        fails (e.g. ``{"error": "..."}``); ``None`` when both
        fields are present and non-empty.
    """
    terminal = args.get("terminal")
    session = args.get("session")
    if not isinstance(terminal, str) or not terminal:
        return {"error": "requires a non-empty 'terminal' string"}
    if not isinstance(session, str) or not session:
        return {"error": "requires a non-empty 'session' string"}
    return None


def _check_overrides(
    terminal_name: str,
    terminal_spec: TerminalEnvSpec,
    cwd_override: str | None,
    sandbox_override: str | None,
) -> str | None:
    """
    Validate per-call overrides against the terminal spec's allow flags.

    Split out of :meth:`SysTerminalLaunchTool._validate_launch_args`
    per the omnigent-dev max-40-line-method rule. Returns either
    a JSON-encoded error envelope or ``None`` when all overrides
    are permitted by the spec.

    :param terminal_name: The terminal name (for error messages).
    :param terminal_spec: The :class:`TerminalEnvSpec` to consult
        for ``allow_cwd_override`` / ``allow_sandbox_override``.
    :param cwd_override: LLM-supplied cwd, or ``None``.
    :param sandbox_override: LLM-supplied sandbox, or ``None``.
    :returns: ``None`` when valid; a JSON-encoded error envelope
        (already serialized by :func:`json.dumps`) when not.
    """
    if cwd_override is not None and not terminal_spec.allow_cwd_override:
        return json.dumps(
            {
                "error": (
                    f"cwd override is not allowed for terminal "
                    f"{terminal_name!r} (set allow_cwd_override: true "
                    f"on the terminal spec to permit)"
                )
            }
        )
    if sandbox_override is not None and not terminal_spec.allow_sandbox_override:
        return json.dumps(
            {
                "error": (
                    f"sandbox override is not allowed for terminal "
                    f"{terminal_name!r} (set allow_sandbox_override: "
                    f"true on the terminal spec to permit)"
                )
            }
        )
    if sandbox_override is not None and sandbox_override not in (
        "none",
        "linux_bwrap",
    ):
        return json.dumps(
            {
                "error": (
                    f"invalid sandbox override {sandbox_override!r}; "
                    f"must be 'none' or 'linux_bwrap'"
                )
            }
        )
    return None


def _resolve_running_instance(
    registry: TerminalRegistry,
    arguments: str,
    ctx: ToolContext,
    tool_name: str,
) -> _ResolvedInstance | str:
    """
    Shared validation for tools that operate on an existing instance.

    Used by ``sys_terminal_send`` / ``read`` / ``close`` (everything
    that doesn't *launch*). Returns either a :class:`_ResolvedInstance`
    holding the live :class:`TerminalInstance` plus the parsed args,
    or a JSON-encoded error envelope ready for the caller to return.

    :param registry: The shared :class:`TerminalRegistry`.
    :param arguments: Raw JSON argument string from the LLM.
    :param ctx: Tool execution context (must provide
        ``conversation_id``).
    :param tool_name: The tool name for use in the
        missing-conversation_id error envelope (e.g.
        ``"sys_terminal_send"``).
    :returns: A :class:`_ResolvedInstance` on success, or a
        JSON-encoded error envelope (string) on failure.
    """
    if ctx.conversation_id is None:
        return json.dumps({"error": f"{tool_name} requires a conversation_id"})
    parsed = _parse_arguments(arguments)
    if "error" in parsed:
        return json.dumps(parsed)
    invalid = _validate_session_required_args(parsed)
    if invalid is not None:
        return json.dumps(invalid)
    instance = registry.get(ctx.conversation_id, parsed["terminal"], parsed["session"])
    lock = registry.get_instance_lock(ctx.conversation_id, parsed["terminal"], parsed["session"])
    # Either the entry is missing from both (never launched / already
    # closed) or both are present (launched and registered atomically).
    # If only one is present, the registry's invariants are broken.
    if instance is None or not instance.running or lock is None:
        return json.dumps(
            {
                "error": (
                    f"terminal '{parsed['terminal']}:{parsed['session']}' not found or not running"
                )
            }
        )
    return _ResolvedInstance(instance=instance, parsed=parsed, lock=lock)


def _parse_arguments(arguments: str) -> dict[str, Any] | dict[str, str]:
    """
    Parse the LLM's JSON argument string into a dict.

    :param arguments: JSON-encoded argument string from the LLM,
        e.g. ``'{"terminal": "bash", "session": "s1"}'``. May be
        empty, in which case an empty dict is returned.
    :returns: Either a parsed argument dict or, on parse failure,
        an error dict ready to JSON-encode.
    """
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        return {"error": f"malformed arguments JSON: {exc}"}
    if not isinstance(parsed, dict):
        return {"error": "arguments must be a JSON object"}
    return parsed


class SysTerminalLaunchTool(Tool):
    """
    ``sys_terminal_launch`` — start a configured tmux session.

    Looks up the terminal spec by name, applies the §4.6 cwd
    resolution precedence, enforces the spec's
    ``allow_cwd_override`` / ``allow_sandbox_override`` flags
    against any per-call overrides, and asks the registry to
    launch.

    :param spec: The agent spec — used to look up the terminal's
        :class:`TerminalEnvSpec` by name and to read the
        agent-level ``os_env`` for cwd-resolution fallback.
    :param registry: The shared :class:`TerminalRegistry` to
        register the new instance in.
    """

    def __init__(
        self,
        spec: AgentSpec,
        registry: TerminalRegistry,
    ) -> None:
        """
        :param spec: The agent spec — used to look up the terminal's
            :class:`TerminalEnvSpec` by name and to read the
            agent-level ``os_env`` for the §4.6 cwd-resolution
            fallback.
        :param registry: The shared :class:`TerminalRegistry`
            singleton that owns per-conversation tmux instances.
        """
        self._spec = spec
        self._registry = registry

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_terminal_launch"``."""
        return "sys_terminal_launch"

    @classmethod
    def description(cls) -> str:
        """:returns: LLM-facing description."""
        return (
            "Launch a named terminal (tmux session). The terminal must be "
            "declared in the agent spec's `terminals:` block. After launch, "
            "use sys_terminal_send to type into it and sys_terminal_read to "
            "capture output. Call sys_terminal_close when done. You MUST "
            "call sys_terminal_launch before sys_terminal_send / "
            "sys_terminal_read for a given (terminal, session) pair."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI Chat-Completions tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _LAUNCH_SCHEMA,
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Launch a terminal and return a JSON status envelope.

        :param arguments: JSON args; ``terminal`` and ``session``
            required, ``cwd`` and ``sandbox`` optional.
        :param ctx: Server-side context. ``conversation_id`` and
            ``workspace`` are required for terminal tools (the
            workspace anchors the §4.6 cwd default).
        :returns: JSON-encoded result dict. Success shape:
            ``{"terminal", "session", "tmux_socket", "has_os_env",
            "status": "launched" | "already_running"}``. Error
            shape: ``{"error": "..."}``.
        """
        if ctx.conversation_id is None:
            return json.dumps({"error": "sys_terminal_launch requires a conversation_id"})

        validated = self._validate_launch_args(arguments)
        if isinstance(validated, str):
            # Validation hit an error envelope — already JSON-encoded.
            return validated

        # §4.6 cwd resolution. Two synthesis points feed the trusted
        # ANCHOR cwd into the inner builder, because the inner code's
        # ``effective_os_env_spec`` selection prefers
        # ``terminal_spec.os_env`` over ``parent_os_env`` whenever
        # the terminal declares its own — so we can't just patch
        # the parent. We patch BOTH:
        #
        # - ``parent_os_env``: covers the case where the terminal
        #   inherits (no own os_env, or ``"inherit"`` sentinel).
        # - ``materialized_terminal_spec``: covers the case where
        #   the terminal has its own os_env but the cwd inside it
        #   is the placeholder. Without this clone, the inner
        #   ``create_terminal_instance`` falls back to
        #   ``os.getcwd()`` — AP's process cwd — instead of the
        #   §4.6-resolved workspace.
        #
        # SECURITY: the anchor is resolved WITHOUT the LLM ``cwd``
        # override. The override must never become its own
        # containment anchor — it flows ONLY through the inner
        # builder's ``cwd_override`` parameter (see
        # :meth:`_perform_launch`), where the guard validates it
        # against this anchor. Feeding the override in here as the
        # effective cwd (as a prior version did for placeholder-cwd
        # specs) makes the inner guard compare the override against
        # itself, so it accepts ANY path — a sandbox escape for the
        # idiomatic ``os_env.cwd: "."`` + ``allow_cwd_override:
        # true`` terminal.
        anchor_cwd = self._resolve_anchor_cwd(validated.terminal_spec, ctx)
        parent_os_env = _synthesize_parent_os_env(self._spec.os_env, anchor_cwd)
        materialized_terminal_spec = _materialize_terminal_spec_for_launch(
            validated.terminal_spec, anchor_cwd
        )

        # Snapshot whether the registry already has a live instance
        # so we can report ``status: "already_running"`` accurately
        # without racing against the launch.
        existing = self._registry.get(
            ctx.conversation_id, validated.terminal_name, validated.session_key
        )
        was_running = existing is not None and existing.running

        return self._spawn_and_format(
            ctx,
            validated,
            materialized_terminal_spec,
            parent_os_env,
            was_running,
        )

    def _spawn_and_format(
        self,
        ctx: ToolContext,
        validated: _ValidatedLaunchArgs,
        terminal_spec: TerminalEnvSpec,
        parent_os_env: OSEnvSpec | None,
        was_running: bool,
    ) -> str:
        """
        Drive ``TerminalRegistry.launch`` and format the JSON result.

        Orchestrates :meth:`_perform_launch` and
        :func:`_format_launch_envelope`. All arguments are
        pre-validated by :meth:`_validate_launch_args` /
        :meth:`_resolve_anchor_cwd` / :func:`_synthesize_parent_os_env`.

        :param ctx: Tool execution context — supplies
            ``conversation_id`` for the resource-publish event.
        :param validated: Validated launch args bundling
            ``terminal_name`` / ``session_key`` / overrides.
        :param terminal_spec: The :class:`TerminalEnvSpec` for
            ``validated.terminal_name``, possibly cwd-materialized
            by :func:`_materialize_terminal_spec_for_launch`.
        :param parent_os_env: §4.6-resolved parent os_env, or
            ``None`` to use the inner builder's defaults.
        :param was_running: Snapshot of whether the registry
            already had a live instance for this triple before the
            launch attempt — drives the ``status`` field.
        :returns: JSON-encoded success envelope or
            ``{"error": "..."}`` envelope on failure.
        """
        # ``ctx.conversation_id`` is asserted non-None by the caller
        # (:meth:`invoke` returns early when missing); narrow for mypy.
        assert ctx.conversation_id is not None
        result = self._perform_launch(ctx.conversation_id, validated, terminal_spec, parent_os_env)
        if isinstance(result, str):
            return result
        instance = result
        # The live ``session.resource.created`` event is emitted by the
        # runner's tool dispatcher (not here) — ``session_stream`` has no
        # subscribers in the runner process.
        return _format_launch_envelope(validated, instance, was_running)

    def _perform_launch(
        self,
        conversation_id: str,
        validated: _ValidatedLaunchArgs,
        terminal_spec: TerminalEnvSpec,
        parent_os_env: OSEnvSpec | None,
    ) -> TerminalInstance | str:
        """
        Drive :meth:`TerminalRegistry.launch` synchronously, mapping
        recoverable subprocess / validation errors to a JSON-encoded
        ``{"error": "..."}`` envelope.

        :param conversation_id: Caller's (validated) conversation id.
        :param validated: Validated launch args.
        :param terminal_spec: The (possibly cwd-materialized)
            :class:`TerminalEnvSpec`.
        :param parent_os_env: §4.6-resolved parent os_env or
            ``None``.
        :returns: The live :class:`TerminalInstance` on success, or
            a JSON-encoded error envelope (``str``) on a known
            recoverable failure.
        """
        try:
            return asyncio.run(
                self._registry.launch(
                    conversation_id,
                    validated.terminal_name,
                    validated.session_key,
                    terminal_spec,
                    parent_os_env=parent_os_env,
                    # cwd_override is reserved for the LLM-supplied
                    # per-call override (already validated against
                    # allow_cwd_override above). The trusted anchor
                    # flows separately through parent_os_env / the
                    # materialized terminal spec; the inner guard
                    # contains this override within that anchor.
                    cwd_override=validated.cwd_override,
                    sandbox_override=validated.sandbox_override,
                )
            )
        except (RuntimeError, OSError, ValueError) as exc:
            # tmux spawn / sandbox setup / inner OSEnvSpec validation
            # surface as these. Wrap in the LLM-facing JSON envelope
            # so the LLM can recover (e.g. by adjusting cwd / sandbox).
            _logger.exception("sys_terminal_launch failed")
            return json.dumps({"error": f"launch failed: {exc}"})

    def _validate_launch_args(self, arguments: str) -> str | _ValidatedLaunchArgs:
        """
        Parse + validate the launch tool's JSON arguments.

        :param arguments: Raw JSON argument string from the LLM.
        :returns: Either a JSON-encoded error envelope (string) for
            the caller to return as-is, or a :class:`_ValidatedLaunchArgs`
            holding the validated fields.
        """
        parsed = _parse_arguments(arguments)
        if "error" in parsed:
            return json.dumps(parsed)
        invalid = _validate_session_required_args(parsed)
        if invalid is not None:
            return json.dumps(invalid)

        terminal_name = parsed["terminal"]
        session_key = parsed["session"]
        cwd_override = parsed.get("cwd")
        sandbox_override = parsed.get("sandbox")

        if self._spec.terminals is None or terminal_name not in self._spec.terminals:
            return json.dumps(
                {
                    "error": (
                        f"unknown terminal {terminal_name!r}; "
                        f"declared: {sorted(self._spec.terminals or [])}"
                    )
                }
            )
        terminal_spec = self._spec.terminals[terminal_name]

        override_error = _check_overrides(
            terminal_name, terminal_spec, cwd_override, sandbox_override
        )
        if override_error is not None:
            return override_error

        return _ValidatedLaunchArgs(
            terminal_name=terminal_name,
            session_key=session_key,
            terminal_spec=terminal_spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
        )

    def _resolve_anchor_cwd(
        self,
        terminal_spec: TerminalEnvSpec,
        ctx: ToolContext,
    ) -> str | None:
        """
        Resolve the trusted containment *anchor* cwd for a launch.

        This is the directory the terminal is anchored to and the
        root that an LLM-supplied ``cwd`` override is contained
        within. It is resolved **without** the override on purpose:
        the override must never become its own anchor, or the inner
        containment guard
        (:func:`omnigent.inner.terminal.build_terminal_os_env_spec`)
        would compare it against itself — ``relative_to`` always
        passes — and accept ANY path (``/``, ``~/.ssh``, ``/etc``).
        That is a sandbox escape for placeholder-cwd terminals
        (``os_env.cwd: "."`` + ``allow_cwd_override: true``). The
        override instead flows separately as the registry's
        ``cwd_override``, where the guard validates it against this
        anchor.

        Precedence (first match wins):
        1. The terminal's own ``os_env.cwd`` if the terminal spec
           declares a meaningful cwd (not ``None``, ``""``,
           ``"."``, or ``"./"``).
        2. ``spec.os_env.cwd`` if it's set to a meaningful path
           (not ``None``, ``""``, ``"."``, or ``"./"``).
        3. ``ctx.workspace`` — the per-task workspace Omnigent creates
           in ``runtime/workflow.py`` (the trusted root).

        :param terminal_spec: The :class:`TerminalEnvSpec`.
        :param ctx: Tool context with the per-task workspace.
        :returns: The resolved anchor cwd as a string, or ``None`` if
            no tier matched (caller falls back to host cwd, mirroring
            legacy behavior).
        """
        terminal_os_env = getattr(terminal_spec, "os_env", None)
        # ``os_env`` on a TerminalEnvSpec may be the literal string
        # ``"inherit"`` (legacy sentinel for "use parent's"). That
        # means "no per-terminal cwd"; fall through.
        if terminal_os_env is not None and not isinstance(terminal_os_env, str):
            terminal_cwd = getattr(terminal_os_env, "cwd", None)
            if _has_meaningful_cwd(terminal_cwd):
                return terminal_cwd
        if self._spec.os_env is not None:
            spec_cwd = self._spec.os_env.cwd
            if _has_meaningful_cwd(spec_cwd):
                return spec_cwd
        if ctx.workspace is not None:
            return str(ctx.workspace)
        return None


class SysTerminalSendTool(Tool):
    """
    ``sys_terminal_send`` — send text and key chords to a running terminal.

    :param registry: The shared :class:`TerminalRegistry`.
    """

    def __init__(self, registry: TerminalRegistry) -> None:
        """
        :param registry: The shared :class:`TerminalRegistry`
            singleton used to look up the live
            :class:`TerminalInstance` keyed by ``(conversation_id,
            terminal_name, session_key)``.
        """
        self._registry = registry

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_terminal_send"``."""
        return "sys_terminal_send"

    @classmethod
    def description(cls) -> str:
        """:returns: LLM-facing description."""
        return "Send text and/or key strokes to a running terminal."

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI Chat-Completions tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _SEND_SCHEMA,
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Send keys to a registered terminal.

        :param arguments: JSON args; ``terminal`` + ``session``
            required; ``text`` and ``keys`` optional.
        :param ctx: Tool context. ``conversation_id`` is required.
        :returns: JSON-encoded result. Success: ``{"status":
            "sent"}``. Error: ``{"error": "..."}``.
        """
        resolved = _resolve_running_instance(self._registry, arguments, ctx, "sys_terminal_send")
        if isinstance(resolved, str):
            return resolved

        text = resolved.parsed.get("text")
        if text is not None and not isinstance(text, str):
            return json.dumps({"error": "'text' must be a string if provided"})
        keys = resolved.parsed.get("keys", "Enter")
        if not isinstance(keys, str):
            return json.dumps({"error": "'keys' must be a string if provided"})
        # Hold the per-instance lock across the full ``send`` op.
        # ``send(text=X, keys="Enter")`` issues ~2 tmux subprocess
        # calls with a 50ms ``asyncio.sleep`` between them; without
        # the lock, two concurrent sends interleave their commands
        # and corrupt the shell input.
        with resolved.lock:
            try:
                result = asyncio.run(resolved.instance.send(text, keys=keys))
            except (RuntimeError, OSError) as exc:
                # tmux send-keys subprocess failures and stale-socket
                # errors land here. Wrap so the LLM sees a structured
                # error and can decide whether to relaunch the terminal.
                _logger.exception("sys_terminal_send failed")
                return json.dumps({"error": f"send failed: {exc}"})
        return json.dumps(result)


class SysTerminalReadTool(Tool):
    """
    ``sys_terminal_read`` — capture the visible pane and scrollback.

    :param registry: The shared :class:`TerminalRegistry`.
    """

    def __init__(self, registry: TerminalRegistry) -> None:
        """
        :param registry: The shared :class:`TerminalRegistry`
            singleton used to look up the live
            :class:`TerminalInstance` keyed by ``(conversation_id,
            terminal_name, session_key)``.
        """
        self._registry = registry

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_terminal_read"``."""
        return "sys_terminal_read"

    @classmethod
    def description(cls) -> str:
        """:returns: LLM-facing description."""
        return "Capture the visible pane plus optional scrollback."

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI Chat-Completions tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _READ_SCHEMA,
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Read pane state from a registered terminal.

        :param arguments: JSON args; ``terminal`` + ``session``
            required, ``scrollback`` optional.
        :param ctx: Tool context. ``conversation_id`` is required.
        :returns: JSON-encoded result. Success: the pane capture
            dict from :meth:`TerminalInstance.read`. Error:
            ``{"error": "..."}``.
        """
        resolved = _resolve_running_instance(self._registry, arguments, ctx, "sys_terminal_read")
        if isinstance(resolved, str):
            return resolved

        scrollback_raw = resolved.parsed.get("scrollback", 0)
        if not isinstance(scrollback_raw, int) or scrollback_raw < 0:
            return json.dumps({"error": "'scrollback' must be a non-negative integer"})

        # Hold the per-instance lock so a concurrent ``send`` can't
        # mutate the pane mid-capture. ``capture-pane`` is one tmux
        # call and probably atomic from tmux's view, but a send can
        # land between two interleaved reads or between a read and
        # whatever the LLM does next. Cheap insurance.
        with resolved.lock:
            try:
                result = asyncio.run(resolved.instance.read(scrollback=scrollback_raw))
            except (RuntimeError, OSError) as exc:
                # tmux capture-pane subprocess failures land here.
                _logger.exception("sys_terminal_read failed")
                return json.dumps({"error": f"read failed: {exc}"})
        return json.dumps(result)


class SysTerminalListTool(Tool):
    """
    ``sys_terminal_list`` — enumerate the conversation's terminals.

    :param registry: The shared :class:`TerminalRegistry`.
    """

    def __init__(self, registry: TerminalRegistry) -> None:
        """
        :param registry: The shared :class:`TerminalRegistry`
            singleton used to look up the live
            :class:`TerminalInstance` keyed by ``(conversation_id,
            terminal_name, session_key)``.
        """
        self._registry = registry

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_terminal_list"``."""
        return "sys_terminal_list"

    @classmethod
    def description(cls) -> str:
        """:returns: LLM-facing description."""
        return (
            "List the conversation's active terminal sessions, with their "
            "running state and tmux socket paths."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI Chat-Completions tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _LIST_SCHEMA,
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Return a list of registered terminals for this conversation.

        :param arguments: Ignored (no parameters).
        :param ctx: Tool context. ``conversation_id`` is required.
        :returns: JSON-encoded list of terminal entries.
        """
        del arguments
        if ctx.conversation_id is None:
            return json.dumps({"error": "sys_terminal_list requires a conversation_id"})

        entries = self._registry.list_for_conversation(ctx.conversation_id)
        result = [_describe_entry(e.terminal_name, e.session_key, e.instance) for e in entries]
        return json.dumps(result)


class SysTerminalCloseTool(Tool):
    """
    ``sys_terminal_close`` — kill a session and remove it from the registry.

    Idempotent: closing an already-closed or unknown session
    returns a "not_found" status instead of raising.

    :param registry: The shared :class:`TerminalRegistry`.
    """

    def __init__(self, registry: TerminalRegistry) -> None:
        """
        :param registry: The shared :class:`TerminalRegistry`
            singleton used to look up the live
            :class:`TerminalInstance` keyed by ``(conversation_id,
            terminal_name, session_key)``.
        """
        self._registry = registry

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_terminal_close"``."""
        return "sys_terminal_close"

    @classmethod
    def description(cls) -> str:
        """:returns: LLM-facing description."""
        return "Close a running terminal session."

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI Chat-Completions tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": _CLOSE_SCHEMA,
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Close a registered terminal.

        :param arguments: JSON args; ``terminal`` + ``session`` required.
        :param ctx: Tool context. ``conversation_id`` is required.
        :returns: JSON-encoded result. Success: ``{"terminal",
            "session", "status": "closed" | "not_found"}``.
        """
        if ctx.conversation_id is None:
            return json.dumps({"error": "sys_terminal_close requires a conversation_id"})

        parsed = _parse_arguments(arguments)
        if "error" in parsed:
            return json.dumps(parsed)
        invalid = _validate_session_required_args(parsed)
        if invalid is not None:
            return json.dumps(invalid)

        terminal_name = parsed["terminal"]
        session_key = parsed["session"]

        # Acquire the per-instance lock BEFORE asking the registry
        # to close. This serializes against any in-flight send/read
        # holding the same lock, so the actual ``instance.close()``
        # never races with a tmux op already in progress. ``None``
        # means the instance was already closed (or never launched);
        # registry.close() returns False in that case anyway.
        lock = self._registry.get_instance_lock(ctx.conversation_id, terminal_name, session_key)

        def _do_close() -> bool:
            try:
                return asyncio.run(
                    self._registry.close(ctx.conversation_id, terminal_name, session_key)
                )
            except (RuntimeError, OSError) as exc:
                # tmux teardown can raise when the server is already
                # gone — surface but don't propagate.
                _logger.exception("sys_terminal_close failed")
                raise _CloseFailed(str(exc)) from exc

        try:
            if lock is not None:
                with lock:
                    closed = _do_close()
            else:
                closed = _do_close()
        except _CloseFailed as exc:
            return json.dumps({"error": f"close failed: {exc}"})

        return json.dumps(
            {
                "terminal": terminal_name,
                "session": session_key,
                "status": "closed" if closed else "not_found",
            }
        )


def _describe_entry(
    terminal_name: str,
    session_key: str,
    instance: TerminalInstance,
) -> dict[str, Any]:
    """
    Render a :class:`TerminalListEntry` as the ``sys_terminal_list``
    output dict.

    :param terminal_name: The terminal's spec name.
    :param session_key: The session key.
    :param instance: The live or defunct :class:`TerminalInstance`.
    :returns: A dict with keys ``terminal``, ``session``, ``command``,
        ``running``, ``has_os_env``, ``tmux_socket``.
    """
    return {
        "terminal": terminal_name,
        "session": session_key,
        "command": instance.command,
        "running": instance.running,
        "has_os_env": instance.os_env is not None,
        "tmux_socket": str(instance.socket_path),
    }
