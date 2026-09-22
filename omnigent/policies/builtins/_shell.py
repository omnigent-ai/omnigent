"""Generic shell-command parsing shared by built-in shell-surface policies.

Built-in policies that gate the OS shell tool (``github`` for git/gh remote
operations, ``working_dir`` for directory / worktree switches) all face the
same problem: a single ``sys_os_shell`` ``command`` string can chain several
commands (``a && b ; c``), prefix them with env-assignments or wrappers
(``sudo``, ``env``, ``VAR=x``), and hide the real command inside a shell
interpreter (``bash -c "<cmd>"``, ``env -S "<cmd>"``) or ``eval``. A policy that only looked at
the first token would be trivially bypassable.

This module factors out the *generic* primitives for breaking a command into
its individual real invocations. It is deliberately policy-agnostic — it does
not know about git, directories, or any domain; each policy composes these
primitives with its own classification and decision logic (including its own
handling of un-tokenizable segments, which differs per policy).
"""

from __future__ import annotations

import re

# Every harness's shell / terminal tool, all of which surface the command as a
# string ``command`` argument. This is the default gated surface for every
# shell-surface policy — a policy that defaults to a narrower set is silently
# inert on the harnesses it omits, which is the least-safe default for a
# security gate. Keep in sync with the per-harness tool-name families in
# ``safety.py`` (which maps the same seven for ``ask_on_os_tools``).
SHELL_TOOLS: frozenset[str] = frozenset(
    {
        "sys_os_shell",  # Omnigent built-in (SDK harnesses)
        "Bash",  # Claude Code / Codex native
        "bash",  # pi / opencode native
        "Shell",  # Cursor
        "terminal",  # Hermes
        "developer__shell",  # Goose
        "shell",  # codex in-process harness (commandExecution observed tool)
    }
)

# Leading tokens to skip when finding the real command in a segment — command
# wrappers that take the real command as their trailing arguments and accept no
# options of their own, so the wrapper word is simply skipped
# (``nohup git push`` → ``git push``). A wrapper with options belongs in
# :data:`_FLAG_WRAPPERS` instead: skipping only its word would leave a flag as
# the apparent command and let the real invocation slip past the gate.
CMD_WRAPPERS: frozenset[str] = frozenset({"nohup"})

# Wrappers that carry their OWN option flags (and, for ``timeout``, a leading
# duration positional) before the real command. Skipping only the wrapper word
# (as for ``CMD_WRAPPERS``) would leave a flag or the duration as the apparent
# command and let the real ``git push`` slip past the gate
# (GHSA-7mqg-cx4g-x2rf). Each entry maps the wrapper to the set of its option
# flags that consume a SEPARATE following value token (``nice -n 10`` /
# ``stdbuf -o L`` / ``timeout -s KILL`` / ``sudo -u root`` / ``env -u VAR``);
# combined forms (``-n10`` / ``-oL`` / ``--signal=KILL`` / ``--unset=VAR``) are
# a single token and need no entry. An end-of-options ``--`` needs no entry
# either — it is consumed as a valueless flag, leaving the command next.
_FLAG_WRAPPERS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "setsid": frozenset(),
    # ``sudo -u root git push`` / ``env -i git push`` / ``command -p git push``
    # / ``time -p git push`` / ``exec -a name git push`` all put a flag where a
    # bare-word skip expects the command.
    "sudo": frozenset(
        {
            "-a",
            "--auth-type",  # BSD: authentication type (takes a value)
            "-c",
            "--login-class",  # BSD: login class (takes a value)
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-R",
            "--chroot",
            "-r",
            "--role",
            "-T",
            "--command-timeout",
            "-t",
            "--type",
            "-U",
            "--other-user",
            "-u",
            "--user",
        }
    ),
    # ``-S`` takes a value like the rest, but that value is a command string —
    # see :data:`_ENV_SPLIT_STRING_FLAGS`, which captures it instead of skipping.
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "command": frozenset(),
    "time": frozenset({"-f", "--format", "-o", "--output"}),
    "exec": frozenset({"-a"}),
}

# ``env -S '<cmd>'`` / ``env --split-string='<cmd>'`` do not merely pass a value
# to ``env`` — coreutils splits the string into words and RUNS it, making ``env``
# a command interpreter like ``sh -c``. Treating ``-S`` as an ordinary value flag
# would swallow the whole command as the flag's value and leave NO tokens to
# classify, so the segment would abstain → ALLOW (and with no tokens, not even
# :func:`is_unresolved_invocation` could catch it). Its value is therefore
# *captured* rather than skipped, and re-parsed via :func:`unwrap_shell_command`.
_ENV_SPLIT_STRING_FLAGS: frozenset[str] = frozenset({"-S", "--split-string"})

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


# Utilities that RUN another command as a child process (``find … -exec git
# push``, ``xargs git push``, ``perl -e 'exec("git",…)'``, ``python3 -c
# "os.execvp('git',…)"``, ``awk 'system("git push …")'``, ``make``). Their
# ``argv[0]`` is the utility, not the gated command, and the command they spawn
# is buried in an ``-exec`` clause / trailing args / a ``-e`` / ``-c`` code
# string / an ``awk`` program this module does not parse. Left unmodelled, the
# head is not ``git``/``gh`` so the segment produces no op and the policy
# abstains → ALLOW — an incomplete-fix bypass of the shell-parser fail-open
# (CVE-2026-62676). Treating it as an unresolved invocation routes it through
# each policy's keyword-gated fail-safe (ASK / the configured action) instead of
# silently allowing it. Matched on the basename so an absolute path counts too.
PROCESS_SPAWNING_UTILITIES: frozenset[str] = frozenset(
    {
        "find",
        "xargs",
        "perl",
        "python",
        "python2",
        "python3",
        "ruby",
        "awk",
        "gawk",
        "mawk",
        "make",
    }
)


def spawns_gated_command_as_child(tokens: list[str]) -> bool:
    """
    Whether the real command is a process-spawning utility (:data:`PROCESS_SPAWNING_UTILITIES`).

    Such a utility runs another command as a child process whose argv this
    module does not parse, so its head is never ``git`` / ``gh`` / ``cd`` and a
    gated child would slip past as "not a gated command". A policy should route
    a ``True`` here through the same fail-safe it uses for an unresolved
    invocation (keyword-gated ASK / configured action), not abstain.

    :param tokens: The output of :func:`real_invocation_tokens`.
    :returns: ``True`` when the head is a process-spawning utility.
    """
    return bool(tokens) and tokens[0].rsplit("/", 1)[-1] in PROCESS_SPAWNING_UTILITIES


def is_shell_interpreter(tokens: list[str]) -> bool:
    """
    Whether the real command is a shell interpreter (:data:`SHELL_INTERPRETERS`).

    A shell interpreter invoked without a ``-c`` command string reads its
    program from stdin or a script file (``printf '…' | sh``, ``sh script.sh``,
    a bare ``sh``). That program is opaque to this parser —
    :func:`unwrap_shell_command` returns ``None`` — so a gated ``git push``
    hidden in it would produce no op and the policy would abstain → ALLOW. A
    policy should surface such an unclassifiable interpreter path for approval
    rather than allow it. Matched on the basename so ``/bin/sh`` counts too.

    This tests the head only, not the arguments, so it also returns ``True`` for
    an interpreter that *does* carry a ``-c`` string. Call it after
    :func:`unwrap_shell_command` has already returned ``None`` for the tokens
    (as the github policy does): a readable ``-c`` form is unwrapped and
    classified there first, so what reaches this check is the stdin/script form.

    :param tokens: The output of :func:`real_invocation_tokens`.
    :returns: ``True`` when the head is a shell interpreter.
    """
    return bool(tokens) and tokens[0].rsplit("/", 1)[-1] in SHELL_INTERPRETERS


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
    (``x=$(git push <url>)``) is still gated.

    The grouping metacharacters ``(``, ``)``, ``{`` and ``}`` are also split
    points. A gated command wrapped in a subshell group ``( git push … )``, a
    brace group ``{ git push … ; }``, or a process substitution ``<( git push …
    )`` runs, but without splitting on these the segment head is ``(`` / ``{``
    (or the fused ``(git``) rather than ``git``, so the parser never classifies
    the inner invocation and the policy silently abstains → ALLOW. Splitting on
    them isolates the inner command as its own segment so it is gated like the
    bare form. ``$(…)`` command substitutions are already pulled out above
    before this split, so their parens never reach here.

    The split is quote-aware: an operator (including a grouping character) is a
    split point only when it is NOT inside single or double quotes. Grouping
    characters appear literally inside quoted code far more often than the
    ``;|&`` operators do — an ``awk`` program ``'BEGIN{system("git push …")}'``,
    a ``perl -e 'exec("git",…)'`` snippet, a ``python3 -c "…os.execvp('git',…)"``
    body — and splitting on those would fragment the command so a
    process-spawning utility's head and its gated keyword could land in
    different segments, silently reopening the shell-parser fail-open. Honoring
    quotes keeps such a command whole so the utility head is still classified,
    while a real ``( … )`` / ``{ …; }`` group (whose operators are unquoted)
    still splits.

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
    segments = [seg.strip() for seg in _split_unquoted_operators(outer) if seg.strip()]
    for body in bodies:
        segments.extend(split_command_segments(body))
    return segments


# Single-character command separators / grouping operators (``&&`` and ``||``
# are handled as two-character operators before these). The grouping characters
# ``(){}`` isolate a command wrapped in a subshell / brace group / process
# substitution so it is still classified; see split_command_segments.
_SEGMENT_OPERATOR_CHARS: frozenset[str] = frozenset(";|&\n(){}")


def _split_unquoted_operators(command: str) -> list[str]:
    """
    Split *command* on shell separators, ignoring separators inside quotes.

    Walks the string tracking single-/double-quote state and backslash escapes,
    and cuts on ``&&``, ``||`` and each :data:`_SEGMENT_OPERATOR_CHARS`
    character encountered outside quotes. A backslash outside single quotes
    escapes the next character, so neither the backslash nor that character is
    treated as a quote toggle or an operator — matching how the shell tokenizes,
    where ``echo \\" ; git push …`` still runs ``git push`` as its own command
    (the escaped ``"`` does not open a quoted region, so the ``;`` still splits).
    Missing this reopened a guardrail bypass. Single quotes take no escapes.
    An unbalanced quote leaves the remainder quoted; the downstream ``shlex``
    parse then fails that segment closed via each policy's un-tokenizable
    fail-safe.

    :param command: The command string, substitutions already stripped.
    :returns: The raw (un-trimmed) segments between operators.
    """
    segments: list[str] = []
    buf: list[str] = []
    in_single = in_double = False
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if in_single:
            # Single quotes are literal: no escapes, only ``'`` ends the region.
            if ch == "'":
                in_single = False
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            # Outside single quotes a backslash escapes the next character; keep
            # both verbatim so an escaped quote/operator is neither a toggle nor
            # a split point (also correct inside double quotes, e.g. ``\"``).
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if in_double:
            if ch == '"':
                in_double = False
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
        elif ch == '"':
            in_double = True
            buf.append(ch)
        elif command[i : i + 2] in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        elif ch in _SEGMENT_OPERATOR_CHARS:
            segments.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return segments


def real_invocation_tokens(tokens: list[str]) -> list[str]:
    """
    Drop leading env-assignments and command wrappers to reach the real argv.

    Wrappers are matched on the basename, so an absolute path (``/usr/bin/sudo
    -u root git push``) is stripped like the bare word — otherwise the path
    token would be left as the apparent command and the push would slip past
    the gate exactly as an unmodelled wrapper flag would.

    An ``env -S '<cmd>'`` wrapper is left in place instead of stripped: its
    string is a command to re-parse, which is :func:`unwrap_shell_command`'s
    job, not a value to skip over.

    :param tokens: shlex-split tokens of one segment, e.g.
        ``["sudo", "GIT_SSH=x", "git", "push"]`` or
        ``["timeout", "-s", "KILL", "5m", "git", "push"]``.
    :returns: Tokens starting at the real command (``["git", "push"]``), or
        empty when nothing remains.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        word = token.rsplit("/", 1)[-1]
        if word in CMD_WRAPPERS or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            index += 1
            continue
        if word in _FLAG_WRAPPERS:
            next_index, split_string = _skip_flag_wrapper_args(
                tokens,
                index + 1,
                value_flags=_FLAG_WRAPPERS[word],
                has_duration=word in _DURATION_WRAPPERS,
                capture_flags=_ENV_SPLIT_STRING_FLAGS if word == "env" else frozenset(),
            )
            if split_string is not None:
                # ``env -S`` RUNS the captured string — stop at the wrapper so
                # the caller unwraps and re-parses it rather than losing the
                # command into a consumed flag value.
                break
            index = next_index
            continue
        break
    return tokens[index:]


def is_unresolved_invocation(tokens: list[str]) -> bool:
    """
    Whether :func:`real_invocation_tokens` failed to reach a real command.

    A leading ``-`` means the head is still an option belonging to some wrapper
    whose flags this module does not model, so the segment's real command was
    never reached. Treating that as "not a gated command" is what let
    ``sudo -u root git push`` slip past the gate, so each policy must instead
    route it through its own un-parseable handling (ASK / the configured
    action) — the same fail-safe it applies to a segment ``shlex`` cannot
    tokenize. This is the backstop for :data:`_FLAG_WRAPPERS` being an
    enumeration that can fall behind a new wrapper.

    :param tokens: The output of :func:`real_invocation_tokens`, e.g.
        ``["-u", "root", "git", "push"]``.
    :returns: ``True`` when the head is an option rather than a command.
    """
    return bool(tokens) and tokens[0].startswith("-")


def _skip_flag_wrapper_args(
    tokens: list[str],
    index: int,
    *,
    value_flags: frozenset[str],
    has_duration: bool,
    capture_flags: frozenset[str] = frozenset(),
) -> tuple[int, str | None]:
    """
    Skip a flag-wrapper's own option flags (and its duration positional).

    Given the index just past a :data:`_FLAG_WRAPPERS` word, advance over any
    leading option flags — consuming a following value token for a separate-token
    *value_flag* (``-s KILL``) — and then, for a duration wrapper, the single
    required duration positional, leaving *index* at the real command.

    Short options bundle (``sudo -nu root``, ``env -iu FOO``), so a value-taking
    option is matched against each character of a single-dash token rather than
    only against the whole token. Its value is a separate token only when the
    option is the bundle's LAST character; otherwise the rest of the token is
    the attached value (``nice -n10`` / ``stdbuf -oL``) and nothing further is
    consumed. Only the first value-taking option of a bundle is modelled, which
    is the only form ``getopt`` itself can honor — a value option that is not
    last ends the bundle by taking the remainder as its value.

    :param tokens: The full token list of the segment.
    :param index: Index of the first token after the wrapper word.
    :param value_flags: The wrapper's flags that consume a separate value token.
    :param has_duration: Whether the wrapper takes a leading duration positional
        (``timeout``).
    :param capture_flags: Flags whose value is a *command string* to re-parse
        rather than an opaque value to skip (:data:`_ENV_SPLIT_STRING_FLAGS`).
    :returns: ``(index, captured)`` — the index of the wrapped command's first
        token, and the value of a *capture_flags* option when one was present
        (else ``None``).
    """
    captured: str | None = None
    while index < len(tokens) and tokens[index].startswith("-"):
        flag = tokens[index]
        index += 1
        if flag.startswith("--"):
            # Long options take a separate value only in the ``--flag value``
            # form; ``--flag=value`` is a single token needing no lookahead.
            name, equals, attached = flag.partition("=")
            if name in capture_flags:
                captured = attached if equals else _value_at(tokens, index)
            if flag in value_flags and index < len(tokens):
                index += 1
            continue
        bundle = flag[1:]
        position = next(
            (pos for pos, opt in enumerate(bundle) if f"-{opt}" in value_flags),
            None,
        )
        if position is None:
            continue
        is_last = position == len(bundle) - 1
        if f"-{bundle[position]}" in capture_flags:
            captured = _value_at(tokens, index) if is_last else bundle[position + 1 :]
        if is_last and index < len(tokens):
            index += 1
    if has_duration and index < len(tokens):
        index += 1
    return index, captured


def _value_at(tokens: list[str], index: int) -> str | None:
    """
    Return ``tokens[index]``, or ``None`` when the option's value is missing.

    :param tokens: The full token list of the segment.
    :param index: Index of the token holding an option's separate value.
    :returns: The value token, or ``None`` past the end (``env -S`` with no
        string runs nothing, so there is nothing to re-parse).
    """
    return tokens[index] if index < len(tokens) else None


def unwrap_shell_command(tokens: list[str]) -> str | None:
    """
    Return the inner command string of a shell-interpreter / ``eval`` wrapper.

    ``env -S '<cmd>'`` counts as one: coreutils splits the string into words and
    runs it, so it is a command interpreter wearing an option flag. Without
    unwrapping it, the string is consumed as an ordinary flag value and the
    segment yields no command at all — a silent ALLOW of whatever it hides.

    :param tokens: Real invocation tokens (env-prefixes / wrappers already
        stripped), e.g. ``["bash", "-c", "git push origin main"]``,
        ``["eval", "git", "push"]`` or ``["env", "-S", "git push origin main"]``.
    :returns: The wrapped command string to re-parse, or ``None`` when *tokens*
        is not a shell-interpreter / ``eval`` / ``env -S`` invocation.
    """
    head = tokens[0].rsplit("/", 1)[-1]
    if head == "env":
        _, split_string = _skip_flag_wrapper_args(
            tokens,
            1,
            value_flags=_FLAG_WRAPPERS["env"],
            has_duration=False,
            capture_flags=_ENV_SPLIT_STRING_FLAGS,
        )
        return split_string
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
