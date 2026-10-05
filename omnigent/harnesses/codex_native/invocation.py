"""Resolved Codex command invocations shared by native launch paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CodexInvocation:
    """One executable plus the immutable arguments prepended to Codex args."""

    executable: str
    argv_prefix: tuple[str, ...] = ()
    configured: bool = False

    def argv(self, *args: str) -> tuple[str, ...]:
        """Return the complete argv for one Codex subcommand."""
        return (self.executable, *self.argv_prefix, *args)


def resolve_codex_invocation(
    *,
    explicit: str | None = None,
    cfg: Mapping[str, object] | None = None,
) -> CodexInvocation:
    """Resolve the native Codex executable and configured prefix once.

    Native Codex gives a configured command precedence over its path env var;
    an explicit command only inherits configured args when it names that same
    command.
    """
    if cfg is None:
        from omnigent.config import load_effective_config

        cfg = load_effective_config()
    from omnigent.harness_startup_config import resolve_harness_command, resolve_harness_config

    _, overrides = resolve_harness_config(cfg)
    entry = overrides.get("codex-native") or {}
    configured_command = entry.get("command")
    configured_command = (
        configured_command.strip()
        if isinstance(configured_command, str) and configured_command.strip()
        else None
    )
    configured_args = entry.get("args")
    configured_prefix = tuple(configured_args) if isinstance(configured_args, list) else ()
    explicit_command = explicit.strip() if isinstance(explicit, str) and explicit.strip() else None
    if explicit_command is not None:
        command = explicit_command
        prefix = configured_prefix if configured_command in {None, explicit_command} else ()
    elif configured_command is not None:
        command = configured_command
        prefix = configured_prefix
    else:
        command = resolve_harness_command(
            "codex-native",
            default="codex",
            explicit=None,
            cfg=cfg,
        )
        prefix = configured_prefix
    configured = bool(
        prefix
        or (configured_command is not None and command == configured_command)
        or (explicit_command is None and command != "codex")
    )
    return CodexInvocation(command, prefix, configured)


__all__ = ["CodexInvocation", "resolve_codex_invocation"]
