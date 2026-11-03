"""Argv allowlist for the ``bob chat`` TUI that ``omnigent bob`` launches.

Launch args reach the runner from ``omnigent bob [ARGS]`` and from the
session's persisted ``terminal_launch_args``, so they are validated in both
places. Only documented ``bob chat`` options are accepted. Options that would
widen Bob's tool gate behind the user's back (``--auto-approve``) or that
Omnigent owns (the workspace) are rejected with a pointer to the alternative.
"""

from __future__ import annotations

from collections.abc import Sequence

#: ``bob chat`` subcommand that starts the interactive TUI (Bob Shell 2.x).
BOB_CHAT_SUBCOMMAND = "chat"

# Documented ``bob chat`` options (Bob Shell 2.0.5) that take one value.
_VALUE_OPTIONS: frozenset[str] = frozenset(
    {
        "--disable-tool-groups",
        "--instance-id",
        "--log-level",
        "--max-cost",
        "--max-turns",
        "--mode",
        "--team-id",
    }
)
# Documented ``bob chat`` flags that take no value. ``--trust`` and
# ``--accept-license`` are allowed because passing them is the user's explicit
# answer to Bob's folder-trust and license prompts; Omnigent never adds them.
_FLAG_OPTIONS: frozenset[str] = frozenset(
    {"--accept-license", "--disable-mcp", "--disable-subagents", "--trust"}
)
# ``--resume`` takes an optional task id; bare ``--resume`` opens Bob's picker.
_OPTIONAL_VALUE_OPTIONS: frozenset[str] = frozenset({"--resume", "-r"})

_REJECTED_OPTIONS: dict[str, str] = {
    "--auto-approve": (
        "Omnigent does not launch Bob with --auto-approve: Bob's own approval "
        "dialog is the only tool gate for bob-native sessions. Toggle "
        "auto-approve inside Bob (Ctrl+F on macOS, Alt+F elsewhere) if you want it."
    ),
    "--workspace": "Omnigent sets Bob's workspace to the session directory; drop --workspace.",
    "-w": "Omnigent sets Bob's workspace to the session directory; drop -w.",
    "--model": "Bob Shell 2.x has no model flag; choose the model inside Bob instead.",
    "-m": "Bob Shell 2.x has no model flag; choose the model inside Bob instead.",
}


class BobLaunchArgsError(ValueError):
    """Raised when a ``bob chat`` launch arg is not on the allowlist."""


def validate_bob_chat_args(args: Sequence[str]) -> list[str]:
    """Return *args* unchanged after checking each against the allowlist.

    :param args: Raw user args destined for ``bob chat``, e.g.
        ``["--mode", "plan", "--resume", "latest"]``.
    :returns: The args as a list, in order.
    :raises BobLaunchArgsError: On an unknown, rejected, or malformed option.
    """
    validated = list(args)
    index = 0
    while index < len(validated):
        token = validated[index]
        name, has_inline_value, inline_value = token.partition("=")
        if name in _REJECTED_OPTIONS:
            raise BobLaunchArgsError(_REJECTED_OPTIONS[name])
        if (
            has_inline_value
            and not inline_value
            and name in _VALUE_OPTIONS | _OPTIONAL_VALUE_OPTIONS
        ):
            raise BobLaunchArgsError(f"Bob option {name} has an empty value.")
        if name in _VALUE_OPTIONS:
            if not has_inline_value:
                if index + 1 >= len(validated) or validated[index + 1].startswith("-"):
                    raise BobLaunchArgsError(f"Bob option {name} requires a value.")
                index += 1
        elif name in _OPTIONAL_VALUE_OPTIONS:
            following = validated[index + 1] if index + 1 < len(validated) else None
            if not has_inline_value and following is not None and not following.startswith("-"):
                index += 1
        elif name in _FLAG_OPTIONS:
            if has_inline_value:
                raise BobLaunchArgsError(f"Bob flag {name} does not take a value.")
        else:
            raise BobLaunchArgsError(
                f"Unsupported bob chat argument {token!r}. Supported options: "
                + ", ".join(sorted(_VALUE_OPTIONS | _FLAG_OPTIONS | {"--resume"}))
                + "."
            )
        index += 1
    return validated


def build_bob_chat_argv(user_args: Sequence[str]) -> list[str]:
    """Return the ``bob`` argv (minus the executable) for the TUI launch.

    :param user_args: Validated or raw user args; validated again here.
    :returns: ``["chat", *user_args]``.
    :raises BobLaunchArgsError: If *user_args* fail validation.
    """
    return [BOB_CHAT_SUBCOMMAND, *validate_bob_chat_args(user_args)]


def preflight_bob_cli_args(args: Sequence[str]) -> None:
    """CLI dispatch preflight: reject unsupported Bob args before a backend starts.

    :param args: Pass-through args the native launcher would receive.
    :raises click.ClickException: If *args* fail :func:`validate_bob_chat_args`.
    """
    import click

    try:
        validate_bob_chat_args(args)
    except BobLaunchArgsError as exc:
        raise click.ClickException(str(exc)) from exc
