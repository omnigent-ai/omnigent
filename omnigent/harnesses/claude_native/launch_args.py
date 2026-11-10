"""Claude Code launch-arg helpers shared by the server and the runner."""

from __future__ import annotations

from collections.abc import Sequence

# Permission modes that never approve a tool call on their own, so keeping one
# while other stored args (an ``--disallowedTools`` restriction, say) are left
# out cannot let a tool run unasked. ``acceptEdits``, ``auto`` and
# ``bypassPermissions`` each approve some calls themselves.
CLAUDE_PERMISSION_MODES_THAT_ASK = frozenset({"default", "plan", "dontAsk"})


def split_claude_permission_launch_args(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """
    Split Claude launch args into permission-mode selectors and everything else.

    The selectors are ``--permission-mode`` (space- or ``=``-joined) and the
    standalone ``--dangerously-skip-permissions``, which Claude treats as
    ``--permission-mode bypassPermissions``. Tokens after an ``--`` option
    terminator are positional text, never selectors.

    :param args: Launch args, e.g. ``["--model", "opus", "--permission-mode", "plan"]``.
    :returns: ``(other_args, permission_args)``, each in its original order, e.g.
        ``(["--model", "opus"], ["--permission-mode", "plan"])``.
    """
    other: list[str] = []
    permission: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            other.extend(args[index - 1 :])
            break
        if arg == "--dangerously-skip-permissions" or arg.startswith("--permission-mode="):
            permission.append(arg)
        elif arg == "--permission-mode":
            permission.append(arg)
            if index < len(args) and not args[index].startswith("-"):
                permission.append(args[index])
                index += 1
        else:
            other.append(arg)
    return other, permission


def claude_permission_mode(permission_args: Sequence[str]) -> str | None:
    """
    The permission mode a set of Claude permission selectors launches into.

    :param permission_args: Selectors from :func:`split_claude_permission_launch_args`,
        e.g. ``["--permission-mode", "plan"]``.
    :returns: The last selector's mode, e.g. ``"plan"``; ``"bypassPermissions"``
        for ``--dangerously-skip-permissions``; ``None`` when there is none or
        it has no value.
    """
    mode: str | None = None
    index = 0
    while index < len(permission_args):
        arg = permission_args[index]
        index += 1
        if arg == "--dangerously-skip-permissions":
            mode = "bypassPermissions"
        elif arg.startswith("--permission-mode="):
            mode = arg.partition("=")[2] or None
        elif arg == "--permission-mode":
            mode = None
            if index < len(permission_args) and not permission_args[index].startswith("-"):
                mode = permission_args[index]
                index += 1
    return mode
