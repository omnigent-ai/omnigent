"""Interpreter invocation for native-harness hook and MCP children.

A hook or MCP child must import the *same* ``omnigent`` the process that wrote
its command is running, not whichever copy its interpreter happens to find
first. Two things can break that, and they pull in opposite directions:

- the session's working directory. A worktree with its own ``omnigent/``
  precedes everything on ``sys.path`` for ``-m``, so a child launched from
  there imports the checkout instead of the install.
- the environment. When the parent's own omnigent is reachable only through
  ``PYTHONPATH`` or user site-packages, a child that discards those resolves a
  different copy — or an older one that lacks the module being run, which
  fails at import with no output the harness can act on.

``-I`` (isolated mode) fixes the first by discarding the second, so it trades
one failure for the other. ``-P`` drops the working directory from
``sys.path`` and does only that, and pinning ``PYTHONPATH`` to the parent's own
package root makes the child's resolution match the parent's however omnigent
was installed. That pin is no more restrictive than the ``-I`` it replaces,
which ignored ``PYTHONPATH`` outright.
"""

from __future__ import annotations

from pathlib import Path

# Directory holding the ``omnigent`` package this process imported.
OMNIGENT_IMPORT_ROOT = str(Path(__file__).resolve().parents[2])

# Keeps the session's working directory off ``sys.path`` without discarding the
# environment that makes omnigent importable.
PYTHON_NO_CWD_FLAG = "-P"


def omnigent_child_env() -> dict[str, str]:
    """Environment entries pinning a child to the running omnigent.

    For children a harness spawns directly, where the command and its
    environment are configured separately rather than as one shell string.

    :returns: Environment mapping to merge into the child's ``env``.
    """
    return {"PYTHONPATH": OMNIGENT_IMPORT_ROOT}


def omnigent_module_argv(python: str, module: str, *args: str) -> list[str]:
    """Build argv running an omnigent *module* against the running install.

    :param python: Interpreter to run, e.g. ``"/usr/bin/python3"``.
    :param module: Dotted module to run with ``-m``, e.g.
        ``"omnigent.harnesses.claude_native.hook"``.
    :param args: Arguments passed through to the module.
    :returns: argv suitable for :func:`shlex.join` into a shell hook command.
    """
    return [
        "env",
        f"PYTHONPATH={OMNIGENT_IMPORT_ROOT}",
        python,
        PYTHON_NO_CWD_FLAG,
        "-m",
        module,
        *args,
    ]
