"""Claude Code launch-arg helpers shared by the server and the runner."""

from __future__ import annotations

from collections.abc import Sequence


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
