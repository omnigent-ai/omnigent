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
# wrappers that take the real command as their trailing arguments. These take
# no options of their own in the forms we gate, so the wrapper word is simply
# skipped (``sudo git push`` → ``git push``).
CMD_WRAPPERS: frozenset[str] = frozenset({"sudo", "env", "command", "time", "nohup", "exec"})

# Wrappers that carry their OWN option flags (and, for ``timeout``, a leading
# duration positional) before the real command. Skipping only the wrapper word
# (as for ``CMD_WRAPPERS``) would leave a flag or the duration as the apparent
# command and let the real ``git push`` slip past the gate
# (GHSA-7mqg-cx4g-x2rf). Each entry maps the wrapper to the set of its option
# flags that consume a SEPARATE following value token (``nice -n 10`` /
# ``stdbuf -o L`` / ``timeout -s KILL``); combined forms (``-n10`` / ``-oL`` /
# ``--signal=KILL``) are a single token and need no entry.
_FLAG_WRAPPERS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "setsid": frozenset(),
}

# Flag-wrappers that ALSO consume a leading positional (a duration) after their
# own flags: ``timeout 5m git push`` / ``timeout -s KILL 5m git push``.
_DURATION_WRAPPERS: frozenset[str] = frozenset({"timeout"})

# Shell interpreters that run a command string passed via ``-c`` (or, for
# ``eval``, as positional words). Their inner command is parsed recursively so
# ``bash -c "git push …"`` is gated like a bare ``git push …`` rather than
# slipping past detection. Matched on the basename so ``/bin/bash`` counts too.
SHELL_INTERPRETERS: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

# Matches the ``-c`` command-string flag of a shell interpreter, whether bare
# (``-c``) or bundled with other single-char flags (``-lc`` login, ``-ic``
# interactive, ``-xc`` trace). bash/sh still read the command from the next
# operand in every such form, so ``bash -lc "git push …"`` must unwrap like
# ``bash -c "git push …"`` rather than slip past as unrecognized.
_INTERPRETER_C_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*$")

# Guard against pathological nesting (``bash -c "bash -c …"``).
MAX_SHELL_NESTING = 4


def _extract_command_substitutions(command: str) -> tuple[str, list[str]]:
    """
    Pull ``$(...)`` and backtick command-substitution bodies out of a command.

    A substitution body is itself a command the shell *runs* — its output is
    interpolated — so ``x=$(git push <url>)`` executes the push even though the
    outer token looks like a plain env-assignment that
    :func:`real_invocation_tokens` would skip. To gate it, the body must be
    parsed as a command in its own right (GHSA-7mqg-cx4g-x2rf).

    :param command: The raw shell command string.
    :returns: ``(outer, bodies)`` — *outer* is *command* with each substitution
        replaced by a space (so the residue, e.g. ``x=``, parses harmlessly),
        and *bodies* is the list of inner command strings to parse separately.
        ``$(...)`` is matched with balanced-paren scanning so nested
        substitutions are captured whole; backticks are treated as non-nesting.
    """
    bodies: list[str] = []
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            depth, j = 1, i + 2
            while j < n and depth > 0:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            bodies.append(command[i + 2 : j])
            out.append(" ")
            i = j + 1
            continue
        if ch == "`":
            j = command.find("`", i + 1)
            if j == -1:
                out.append(ch)
                i += 1
                continue
            bodies.append(command[i + 1 : j])
            out.append(" ")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), bodies


def split_command_segments(command: str) -> list[str]:
    """
    Split a shell command on chaining operators into individual segments.

    Splits on ``&&``, ``||``, ``;``, ``|``, a single ``&`` (the background
    operator, also a command separator), and newlines so that
    ``git add . && git push`` is evaluated as two segments. The ``&&``
    alternative is matched before the single-``&`` character class, so a
    ``&&`` is consumed whole rather than split into two empty halves.
    Command substitutions (``$(...)`` / backticks) are pulled out first and
    their bodies appended as their own segments, so a command hidden inside one
    (``x=$(git push <url>)``) is still gated. This is a naive split that does
    not honor operators appearing inside quotes — acceptable because the
    commands these policies gate do not embed these operators in quoted args in
    practice, and a mis-split only ever produces an extra ignored segment.

    Splitting on a lone ``&`` matters for the gate: without it, a benign
    leading command could hide a gated one behind a background operator
    (``echo hi & git push`` would be one un-split segment whose head is
    ``echo``, slipping the ``git push`` past detection).

    :param command: The raw shell command string, e.g.
        ``"cd /repo && npm test"``.
    :returns: List of trimmed, non-empty segments, e.g.
        ``["cd /repo", "npm test"]``.
    """
    outer, bodies = _extract_command_substitutions(command)
    parts = re.split(r"&&|\|\||[;|\n&]", outer)
    segments = [seg.strip() for seg in parts if seg.strip()]
    for body in bodies:
        segments.extend(split_command_segments(body))
    return segments


def real_invocation_tokens(tokens: list[str]) -> list[str]:
    """
    Drop leading env-assignments and command wrappers to reach the real argv.

    :param tokens: shlex-split tokens of one segment, e.g.
        ``["sudo", "GIT_SSH=x", "git", "push"]`` or
        ``["timeout", "-s", "KILL", "5m", "git", "push"]``.
    :returns: Tokens starting at the real command (``["git", "push"]``), or
        empty when nothing remains.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in CMD_WRAPPERS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            index += 1
            continue
        if token in _FLAG_WRAPPERS:
            index = _skip_flag_wrapper_args(
                tokens,
                index + 1,
                value_flags=_FLAG_WRAPPERS[token],
                has_duration=token in _DURATION_WRAPPERS,
            )
            continue
        break
    return tokens[index:]


def _skip_flag_wrapper_args(
    tokens: list[str],
    index: int,
    *,
    value_flags: frozenset[str],
    has_duration: bool,
) -> int:
    """
    Skip a flag-wrapper's own option flags (and its duration positional).

    Given the index just past a :data:`_FLAG_WRAPPERS` word, advance over any
    leading option flags — consuming a following value token for a separate-token
    *value_flag* (``-s KILL``) — and then, for a duration wrapper, the single
    required duration positional, leaving *index* at the real command.

    :param tokens: The full token list of the segment.
    :param index: Index of the first token after the wrapper word.
    :param value_flags: The wrapper's flags that consume a separate value token.
    :param has_duration: Whether the wrapper takes a leading duration positional
        (``timeout``).
    :returns: Index of the wrapped command's first token.
    """
    while index < len(tokens) and tokens[index].startswith("-"):
        flag = tokens[index]
        index += 1
        if flag in value_flags and index < len(tokens):
            index += 1
    if has_duration and index < len(tokens):
        index += 1
    return index


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
            if _INTERPRETER_C_FLAG.fullmatch(tok) and i + 1 < len(tokens):
                return tokens[i + 1]
        return None
    if head == "eval":
        # ``eval`` runs its remaining words as a command (often a single quoted
        # string after shlex-splitting); rejoin them to re-parse.
        return " ".join(tokens[1:]) if len(tokens) > 1 else None
    return None
