"""Generic shell-command parsing shared by built-in shell-surface policies.

Built-in policies that gate the OS shell tool (``github`` for git/gh remote
operations, ``working_dir`` for directory / worktree switches) all face the
same problem: a single ``sys_os_shell`` ``command`` string can chain several
commands (``a && b ; c``), prefix them with env-assignments or wrappers
(``sudo``, ``env``, ``VAR=x``), and hide the real command inside a shell
interpreter (``bash -c "<cmd>"``) or ``eval``. A policy that only looked at
the first token would be trivially bypassable.

This module factors out the *generic* primitives for breaking a command into
its individual real invocations. It is deliberately policy-agnostic — it does
not know about git, directories, or any domain; each policy composes these
primitives with its own classification and decision logic (including its own
handling of un-tokenizable segments, which differs per policy).
"""

from __future__ import annotations

import re

# Leading tokens to skip when finding the real command in a segment — command
# wrappers that take the real command as their trailing arguments.
CMD_WRAPPERS: frozenset[str] = frozenset({"sudo", "env", "command", "time", "nohup", "exec"})

# Shell interpreters that run a command string passed via ``-c`` (or, for
# ``eval``, as positional words). Their inner command is parsed recursively so
# ``bash -c "git push …"`` is gated like a bare ``git push …`` rather than
# slipping past detection. Matched on the basename so ``/bin/bash`` counts too.
SHELL_INTERPRETERS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

# Guard against pathological nesting (``bash -c "bash -c …"``).
MAX_SHELL_NESTING = 4


def split_command_segments(command: str) -> list[str]:
    """
    Split a shell command on chaining operators into individual segments.

    Splits on ``&&``, ``||``, ``;``, ``|``, a single ``&`` (the background
    operator, also a command separator), and newlines so that
    ``git add . && git push`` is evaluated as two segments. The ``&&``
    alternative is matched before the single-``&`` character class, so a
    ``&&`` is consumed whole rather than split into two empty halves. This is
    a naive split that does not honor operators appearing inside quotes —
    acceptable because the commands these policies gate do not embed these
    operators in quoted args in practice, and a mis-split only ever produces
    an extra ignored segment.

    Splitting on a lone ``&`` matters for the gate: without it, a benign
    leading command could hide a gated one behind a background operator
    (``echo hi & git push`` would be one un-split segment whose head is
    ``echo``, slipping the ``git push`` past detection).

    :param command: The raw shell command string, e.g.
        ``"cd /repo && npm test"``.
    :returns: List of trimmed, non-empty segments, e.g.
        ``["cd /repo", "npm test"]``.
    """
    parts = re.split(r"&&|\|\||[;|\n&]", command)
    return [seg.strip() for seg in parts if seg.strip()]


def real_invocation_tokens(tokens: list[str]) -> list[str]:
    """
    Drop leading env-assignments and command wrappers to reach the real argv.

    :param tokens: shlex-split tokens of one segment, e.g.
        ``["sudo", "GIT_SSH=x", "git", "push"]``.
    :returns: Tokens starting at the real command (``["git", "push"]``), or
        empty when nothing remains.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in CMD_WRAPPERS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            index += 1
            continue
        break
    return tokens[index:]


def unwrap_shell_command(tokens: list[str]) -> str | None:
    """
    Return the inner command string of a shell-interpreter / ``eval`` wrapper.

    :param tokens: Real invocation tokens (env-prefixes / wrappers already
        stripped), e.g. ``["bash", "-c", "git push origin main"]`` or
        ``["eval", "git", "push"]``.
    :returns: The wrapped command string to re-parse, or ``None`` when *tokens*
        is not a shell-interpreter / ``eval`` invocation.
    """
    head = tokens[0].rsplit("/", 1)[-1]
    if head in SHELL_INTERPRETERS:
        for i, tok in enumerate(tokens):
            if tok == "-c" and i + 1 < len(tokens):
                return tokens[i + 1]
        return None
    if head == "eval":
        # ``eval`` runs its remaining words as a command (often a single quoted
        # string after shlex-splitting); rejoin them to re-parse.
        return " ".join(tokens[1:]) if len(tokens) > 1 else None
    return None
