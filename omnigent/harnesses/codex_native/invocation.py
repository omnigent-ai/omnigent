"""Resolved Codex command invocations shared by native launch paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CodexInvocation:
    """One executable plus app-server and terminal argument prefixes.

    ``terminal_prefix`` defaults to ``argv_prefix`` for compatibility with
    manually constructed invocations. Configured args-only launches override
    it so only the TUI receives those pass-through arguments.
    """

    executable: str
    argv_prefix: tuple[str, ...] = ()
    configured: bool = False
    terminal_prefix: tuple[str, ...] | None = None
    app_server_configured: bool | None = None

    def __post_init__(self) -> None:
        if self.terminal_prefix is None:
            object.__setattr__(self, "terminal_prefix", self.argv_prefix)
        if self.app_server_configured is None:
            object.__setattr__(
                self, "app_server_configured", self.configured or bool(self.argv_prefix)
            )

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
    elif configured_command is not None:
        command = configured_command
    else:
        command = resolve_harness_command(
            "codex-native",
            default="codex",
            explicit=None,
            cfg=cfg,
        )
    same_configured_command = configured_command is not None and command == configured_command
    app_prefix = configured_prefix if same_configured_command else ()
    if configured_command is None or same_configured_command:
        terminal_prefix = configured_prefix
    else:
        terminal_prefix = ()
    configured = bool(
        app_prefix
        or terminal_prefix
        or (configured_command is not None and same_configured_command)
        or (explicit_command is None and command != "codex")
    )
    return CodexInvocation(
        command,
        app_prefix,
        configured,
        terminal_prefix,
        app_server_configured=same_configured_command,
    )


__all__ = ["CodexInvocation", "resolve_codex_invocation"]
