"""Claude Code ``PreToolUse`` hook that routes native subagent spawns.

Registered by ``build_hook_settings`` on the ``Task|Agent`` matcher and
run as a subprocess per spawn. Reads the hook payload from stdin, asks
the runner's ``route-subagent`` endpoint what to do, and writes the
decision to stdout.

Always exits ``0``: routing must never be the reason a spawn fails. When
the endpoint is unadvertised, unreachable, or answers ``allow``, the hook
emits nothing and Claude proceeds unchanged.
"""

from __future__ import annotations

import sys

from omnigent.inner.hook_scripts.subagent_router import run_route_subagent_main

_HARNESS = "claude-native"
_LABEL = "omnigent claude router hook"
_PROG = "omnigent-claude-router-hook"


def main(argv: list[str] | None = None) -> int:
    """
    Run the hook.

    :param argv: Command-line arguments, excluding the program name.
        ``None`` uses :data:`sys.argv`.
    :returns: Always ``0`` so a routing failure never blocks a spawn.
    """
    return run_route_subagent_main(
        list(sys.argv[1:] if argv is None else argv),
        prog=_PROG,
        harness=_HARNESS,
        label=_LABEL,
    )


if __name__ == "__main__":
    sys.exit(main())
