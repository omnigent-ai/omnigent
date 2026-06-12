"""Bounds and blast-radius policies for the coding orchestrator.

Each public function is a :class:`FunctionPolicy` *factory*: it takes the
YAML ``factory_params`` as keyword arguments and returns an evaluator
callable ``fn(event[, config]) -> {"result": ..., "reason": ...}``.
The evaluators run runner-side at tool dispatch
(``omnigent/runner/policy.py``) and add no server routes. See
``designs/NESSIE.md`` "Layer 1 — enforcement".
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from typing import Any, TypeAlias

# Heterogeneous JSON-shaped maps — the V0 policy event + decision payloads.
_Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# A ready ALLOW decision (the common case — most tool calls pass).
_ALLOW: _Json = {"result": "ALLOW"}


def _decision(result: str, reason: str) -> _Json:
    """
    Build a Service-Policies-V0 decision dict.

    :param result: One of ``"ALLOW"``, ``"DENY"``, ``"ASK"``.
    :param reason: Human-readable explanation surfaced to the user
        (shown on ASK prompts and DENY messages), e.g.
        ``"git push is gated; approve to proceed."``.
    :returns: A decision dict, e.g.
        ``{"result": "ASK", "reason": "..."}``.
    """
    return {"result": result, "reason": reason}


def _tool_call(event: _Json, tool_names: set[str]) -> _Json | None:
    """
    Return the args dict of a matching ``tool_call`` event, else ``None``.

    :param event: A V0 event dict with ``type`` and ``data`` keys. For a
        tool call, ``data`` is ``{"name": "<name>", "arguments": {...}}``.
    :param tool_names: Tool names this policy acts on, e.g.
        ``{"sys_os_write", "sys_os_edit"}``.
    :returns: The ``args`` dict when *event* is a ``tool_call`` for one
        of *tool_names*, otherwise ``None`` (caller should ALLOW).
    """
    if event.get("type") != "tool_call":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("name") not in tool_names:
        return None
    args = data.get("arguments")
    return args if isinstance(args, dict) else {}


# Catastrophic, effectively-irreversible commands — always DENY. ``rm`` and
# ``git push`` are NOT here: a single regex missed split/long flag forms
# (``rm -r -f``, ``rm --recursive --force``), root children (``rm -rf /etc``),
# and force/delete refspecs (``git push origin +main`` / ``--delete``). They are
# classified by the flag/refspec-robust helpers below instead.
_DENY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgit\b.*\breset\s+--hard\s+\w+/"),  # hard-reset to a remote ref
)

# Outward / destructive but recoverable — ASK the human first.
_ASK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgh\s+(pr\s+merge|release|repo\s+delete)\b"),
    re.compile(r"\b(kubectl|helm|terraform|databricks)\b.*\b(apply|deploy|destroy|delete)\b"),
)

# Recursive-force ``rm`` of one of these (the directory itself) is catastrophic.
_RM_CRITICAL_DIRS: frozenset[str] = frozenset(
    {
        "/",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/var",
        "/boot",
        "/root",
        "/home",
        "/opt",
        "/dev",
        "/proc",
        "/sys",
    }
)
# Recursive-force ``rm`` of a path UNDER one of these system dirs is also
# catastrophic (system files). ``/home`` / ``/opt`` / ``/root`` are excluded: a
# path under them is scoped/recoverable and is gated at the ASK tier instead.
_RM_SYSTEM_PARENTS: frozenset[str] = frozenset(
    {"/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var", "/boot", "/dev", "/proc", "/sys"}
)
# Common sudo options that consume the following argv token as their value.
_SUDO_VALUE_OPTS: frozenset[str] = frozenset(
    {
        "-C",
        "-D",
        "-g",
        "-h",
        "-p",
        "-R",
        "-r",
        "-T",
        "-t",
        "-U",
        "-u",
        "--chdir",
        "--chroot",
        "--close-from",
        "--command-timeout",
        "--group",
        "--host",
        "--other-user",
        "--prompt",
        "--role",
        "--type",
        "--user",
    }
)
_GIT_GLOBAL_VALUE_OPTS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
)
_PUSH_SHORT_VALUE_OPTS: frozenset[str] = frozenset({"o"})
_ENV_ASSIGNMENT_RE: re.Pattern[str] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def _shell_statements(command: str) -> list[list[str]]:
    """
    Best-effort split of a shell command line into per-statement token lists.

    Splits on the common statement / pipe separators (``;`` ``&&`` ``||`` ``|``
    newline) and tokenizes each piece with :func:`shlex.split` (falling back to
    a whitespace split on a quoting error). This is a heuristic for catching
    obvious destructive commands — it deliberately does NOT model subshells,
    command substitution, or ``eval``, which a determined caller could use to
    evade it. The policy is a safety net against accidental / obvious damage,
    not a security boundary (that is sandboxing).

    :param command: A shell command string, e.g. ``"cd repo && rm -rf build"``.
    :returns: One token list per statement, e.g.
        ``[["cd", "repo"], ["rm", "-rf", "build"]]``.
    """
    statements: list[list[str]] = []
    for piece in re.split(r"&&|\|\||[;|\n]", command):
        piece = piece.strip()
        if not piece:
            continue
        try:
            argv = shlex.split(piece)
        except ValueError:
            argv = piece.split()
        if argv:
            statements.append(argv)
    return statements


def _rm_target_is_catastrophic(target: str) -> bool:
    """
    Whether ``rm -rf`` of *target* would be catastrophic / irreversible.

    Catastrophic = root, the whole home dir, a top-level critical dir itself
    (:data:`_RM_CRITICAL_DIRS`), or any path under a system dir
    (:data:`_RM_SYSTEM_PARENTS`, e.g. ``/etc/...``). A scoped path under
    ``/home`` / ``/opt`` / ``/tmp`` or a relative path is NOT catastrophic here
    (recoverable / the worker's own tree) — those fall to the ASK tier.

    :param target: A single tokenized ``rm`` argument, e.g. ``"/etc"``,
        ``"~"``, ``"build"``.
    :returns: ``True`` if deleting *target* recursively is catastrophic.
    """
    norm = target.rstrip("/") or "/"
    if norm in ("~", "$HOME", "${HOME}"):
        return True
    if target == "/*" or target.startswith("/*"):
        return True
    if norm in _RM_CRITICAL_DIRS:
        return True
    if target.startswith("/"):
        top = "/" + target.lstrip("/").split("/", 1)[0]
        if top in _RM_SYSTEM_PARENTS:
            return True
    return False


def _skip_shell_assignments(argv: list[str], start: int) -> int:
    """
    Return the first index after leading shell-style env assignments.

    Shell statements may prefix a command with temporary environment variables,
    e.g. ``CI=1 git push ...``. Those tokens are not the command itself and
    should not hide the destructive command from classification.

    :param argv: One statement's tokens, e.g. ``["CI=1", "git", "push"]``.
    :param start: Index where assignment scanning begins, e.g. ``0``.
    :returns: The first non-assignment index at or after *start*.
    """
    i = start
    while i < len(argv) and _ENV_ASSIGNMENT_RE.fullmatch(argv[i]):
        i += 1
    return i


def _command_index_after_shell_prefixes(argv: list[str]) -> int:
    """
    Return the command index after env assignments and optional ``sudo``.

    Parses shell-style env assignments plus common sudo flags so
    ``CI=1 sudo -n rm ...`` and ``sudo -u root rm ...`` classify the underlying
    command the same way as bare ``rm ...``.

    :param argv: One statement's tokens, e.g. ``["sudo", "-n", "rm", "-rf", "/"]``.
    :returns: The argv index of the command after any supported prefixes.
    """
    i = _skip_shell_assignments(argv, 0)
    if i >= len(argv) or argv[i] != "sudo":
        return i
    i += 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return _skip_shell_assignments(argv, i + 1)
        if tok.startswith("--"):
            i += 2 if tok in _SUDO_VALUE_OPTS and "=" not in tok and i + 1 < len(argv) else 1
            continue
        if tok.startswith("-") and tok != "-":
            value_opt_pos = next(
                (pos for pos, opt in enumerate(tok[1:]) if f"-{opt}" in _SUDO_VALUE_OPTS),
                None,
            )
            if value_opt_pos is None:
                i += 1
                continue
            value_is_attached = value_opt_pos < len(tok[1:]) - 1
            i += 1 if value_is_attached else 2
            continue
        return _skip_shell_assignments(argv, i)
    return len(argv)


def _rm_severity(argv: list[str]) -> str | None:
    """
    Classify a single ``rm`` statement by blast radius (flag-form robust).

    Detects a recursive ``rm`` in any spelling — combined (``-rf``, ``-Rf``),
    short (``-r``), or long (``--recursive``) — and a leading ``sudo`` wrapper,
    which the previous single regex matched only narrowly. Recursion is the
    blast-radius signal (mass deletion); ``-f`` does not change the verdict
    (matching the prior policy, which gated recursion with force optional). A
    recursive ``rm`` of a catastrophic target (:func:`_rm_target_is_catastrophic`)
    is ``"DENY"``; of any other target it is ``"ASK"``. A non-recursive ``rm``
    (single-file delete) returns ``None``.

    :param argv: One statement's tokens, e.g. ``["rm", "-rf", "/etc"]``.
    :returns: ``"DENY"``, ``"ASK"``, or ``None``.
    """
    i = _command_index_after_shell_prefixes(argv)
    if i >= len(argv) or argv[i] != "rm":
        return None
    recursive = False
    targets: list[str] = []
    positional_only = False  # everything after a bare ``--`` is a filename, not a flag
    for tok in argv[i + 1 :]:
        if positional_only:
            targets.append(tok)
        elif tok == "--":
            positional_only = True
        elif tok == "--force":
            continue
        elif tok == "--recursive":
            recursive = True
        elif tok.startswith("-") and len(tok) > 1 and not tok.startswith("--"):
            recursive = recursive or "r" in tok[1:] or "R" in tok[1:]
        elif not tok.startswith("-"):
            targets.append(tok)
    if not recursive:
        return None
    return "DENY" if any(_rm_target_is_catastrophic(t) for t in targets) else "ASK"


def _push_short_option_is_destructive(token: str) -> bool:
    """
    Whether a bundled ``git push`` short option token force-pushes or deletes.

    Git accepts combined short options such as ``-uf`` and ``-df``. A short
    option that takes an attached value (currently ``-o`` / push-option) stops
    flag parsing for the rest of that token so values like ``-o=fast`` are not
    mistaken for force/delete flags.

    :param token: A short-option token from after ``git push``, e.g. ``"-uf"``.
    :returns: ``True`` if the token contains destructive ``-f`` or ``-d`` flags.
    """
    for opt in token[1:]:
        if opt in ("f", "d"):
            return True
        if opt in _PUSH_SHORT_VALUE_OPTS:
            return False
    return False


def _push_severity(argv: list[str]) -> str | None:
    """
    Classify a single ``git push`` statement by blast radius.

    A force-push (``--force`` / ``--force-with-lease`` / ``-f`` / a
    ``+``-prefixed refspec / ``--mirror``) or a remote-branch deletion
    (``--delete`` / ``--prune`` / ``-d`` / a ``:``-prefixed refspec) is
    irreversible → ``"DENY"``. Any other ``git push`` is outward → ``"ASK"``.
    The ``git`` subcommand is resolved past global options
    (``git -C <path> push …``) so ``"push"`` appearing as an argument value
    (e.g. a commit message) is not mistaken for the subcommand. Anything that
    is not a ``git push`` returns ``None``.

    :param argv: One statement's tokens, e.g.
        ``["git", "push", "origin", "+main"]``.
    :returns: ``"DENY"``, ``"ASK"``, or ``None``.
    """
    i = _command_index_after_shell_prefixes(argv)
    if i >= len(argv) or argv[i] != "git":
        return None
    j = i + 1
    while j < len(argv) and argv[j].startswith("-"):
        j += 2 if argv[j] in _GIT_GLOBAL_VALUE_OPTS and j + 1 < len(argv) else 1
    if j >= len(argv) or argv[j] != "push":
        return None
    for tok in argv[j + 1 :]:
        if tok.startswith("--force") or tok in ("--delete", "--mirror", "--prune"):
            return "DENY"
        if (
            tok.startswith("-")
            and not tok.startswith("--")
            and _push_short_option_is_destructive(tok)
        ):
            return "DENY"
        if len(tok) > 1 and tok[0] in "+:":  # +refspec (force) / :refspec (delete)
            return "DENY"
    return "ASK"


def blast_radius(
    *,
    gate_pushes: bool = True,
    deny_reason: str = "Blocked by the blast-radius policy.",
) -> Callable[[_Json, _Json], _Json]:
    """
    Factory: gate high-blast-radius shell commands by reversibility.

    Catastrophic, irreversible commands (force-push, ``rm -rf /``,
    hard-reset to a remote ref) are DENIED. Outward or destructive but
    recoverable commands (``git push``, ``gh pr merge``, ``rm -rf`` of a
    path, infra deploy/destroy) return ASK so the human approves before
    they run. Everything else — reads, tests, edits, and local git
    (commit / merge / worktree) — is ALLOWED.

    :param gate_pushes: When ``True`` (default), recoverable-but-outward
        commands return ASK. When ``False`` only the catastrophic DENY
        set is enforced — use only for trusted unattended batch runs.
    :param deny_reason: Reason text surfaced on a DENY decision.
    :returns: An evaluator ``fn(event, config)`` returning a V0 decision.
    """

    def _evaluate(event: _Json, config: _Json) -> _Json:  # noqa: ARG001
        """
        Classify a ``sys_os_shell`` command by blast radius.

        :param event: V0 ``tool_call`` event for ``sys_os_shell``.
        :param config: Runtime config dict (unused; bounds come from the
            factory params).
        :returns: ALLOW / ASK / DENY decision dict.
        """
        # Match the Omnigent built-in OS shell, the Claude/Codex native
        # Bash tool, and Pi's native lowercase ``bash``. The PreToolUse hook
        # reports BOTH CLI harnesses' shell tool as ``Bash`` with a string
        # ``command`` (codex normalizes to this shape); Pi's ``tool_call``
        # hook reports ``bash`` with the same ``command`` key — so one match
        # set covers all three.
        args = _tool_call(event, {"sys_os_shell", "Bash", "bash"})
        if args is None:
            return _ALLOW
        command = args.get("command")
        # A Bash / sys_os_shell call always carries a string ``command`` by
        # contract; a non-str is a malformed payload no pattern can classify, so
        # there is nothing to gate.
        if not isinstance(command, str):
            return _ALLOW
        # rm + git push are classified by flag/refspec-robust helpers (a regex
        # missed split/long rm flags, root children, and force/delete refspecs);
        # the remaining regex patterns cover git-reset / gh / infra tools.
        statements = _shell_statements(command)
        severities = {
            sev for stmt in statements for sev in (_rm_severity(stmt), _push_severity(stmt))
        }
        if "DENY" in severities or any(p.search(command) for p in _DENY_PATTERNS):
            return _decision("DENY", f"{deny_reason} (irreversible: {command!r})")
        if gate_pushes and ("ASK" in severities or any(p.search(command) for p in _ASK_PATTERNS)):
            return _decision("ASK", f"High-blast-radius command needs approval: {command!r}")
        return _ALLOW

    return _evaluate


def spawn_bounds(
    *,
    max_dispatches_per_turn: int = 5,
    dispatch_tools: tuple[str, ...] = ("sys_session_send",),
) -> Callable[[_Json], _Json]:
    """
    Factory: cap how many workers the orchestrator may dispatch per turn.

    Counts the *dispatch_tools* tool calls within a single orchestrator turn
    and DENIES once *max_dispatches_per_turn* is exceeded, forcing fan-out in
    bounded waves rather than an unbounded fleet. The orchestrator dispatches
    every worker through a sub-agent send (``sys_session_send``), so that is the
    default counted tool. The counter resets each turn via the ``reset_turn``
    hook the runner calls (``omnigent/runner/policy.py``). This is the v1
    concurrency bound; true cross-turn live-concurrency accounting is a v1.x
    refinement.

    :param max_dispatches_per_turn: Maximum worker dispatches allowed in one
        turn, e.g. ``5``.
    :param dispatch_tools: Tool names that count as a worker dispatch, e.g.
        ``("sys_session_send",)``. A YAML list is accepted (coerced to a set).
    :returns: A stateful evaluator ``fn(event)`` carrying a ``reset_turn``
        attribute, returning a V0 decision dict.
    """
    counted = set(dispatch_tools)
    state = {"count": 0}

    def _evaluate(event: _Json) -> _Json:
        """
        Count and bound worker dispatches in the current turn.

        :param event: V0 event; a dispatch is a ``tool_call`` whose
            ``data["name"]`` is one of *dispatch_tools*.
        :returns: ALLOW, or DENY once the per-turn cap is exceeded.
        """
        if _tool_call(event, counted) is None:
            return _ALLOW
        state["count"] += 1
        if state["count"] > max_dispatches_per_turn:
            return _decision(
                "DENY",
                f"Exceeded {max_dispatches_per_turn} worker dispatches this turn; "
                "fan out in waves (collect the running batch before dispatching more).",
            )
        return _ALLOW

    def reset_turn() -> None:
        """
        Reset the per-turn dispatch counter at each turn boundary.

        :returns: ``None``.
        """
        state["count"] = 0

    # FunctionPolicy looks for this attribute to reset per-turn state.
    _evaluate.reset_turn = reset_turn  # type: ignore[attr-defined]
    return _evaluate


def headless_subagent_purpose_guard(
    *,
    allowed_purposes: tuple[str, ...] = ("implement", "review", "explore", "search"),
    deny_reason: str = (
        "Every sys_session_send must declare what kind of work it is. Set "
        "args.purpose to one of `implement` (write product code — any code "
        "change, however small), `review` (judge a diff against its contract), "
        "or `explore` / `search` (read-only investigation). All sub-agents "
        "(`claude_code`, `codex`, `pi`) accept all of these."
    ),
) -> Callable[[_Json], _Json]:
    """
    Factory: require every ``sys_session_send`` to declare its ``args.purpose``.

    The orchestrator delegates all work through sub-agents, so each dispatch must be
    tagged with an explicit ``args.purpose`` drawn from *allowed_purposes*.
    The policy fails loud on an unmarked or out-of-set purpose, keeping
    dispatches intentional rather than letting the model spawn a sub-agent
    with no declared role.

    :param allowed_purposes: Explicit ``args.purpose`` values accepted for a
        sub-agent dispatch, e.g. ``"review"`` or ``"implement"``.
    :param deny_reason: Human-facing reason returned on DENY.
    :returns: An evaluator ``fn(event)`` returning DENY for unmarked or
        out-of-set ``sys_session_send`` calls.
    """
    allowed = set(allowed_purposes)

    def _evaluate(event: _Json) -> _Json:
        """
        Deny unmarked or disallowed sub-agent dispatches.

        :param event: V0 ``tool_call`` event for ``sys_session_send``.
        :returns: ALLOW when ``args.purpose`` is allowed, DENY otherwise.
        """
        args = _tool_call(event, {"sys_session_send"})
        if args is None:
            return _ALLOW
        child_args = args.get("args")
        if not isinstance(child_args, dict):
            return _decision("DENY", f"{deny_reason} Missing object args with purpose.")
        purpose = child_args.get("purpose")
        if not isinstance(purpose, str) or purpose not in allowed:
            return _decision(
                "DENY",
                f"{deny_reason} Set args.purpose to one of {sorted(allowed)!r} "
                "when this is a legitimate sub-agent task.",
            )
        return _ALLOW

    return _evaluate


def worktree_guard(
    *,
    allowed_root: str = ".worktrees",
    deny_reason: str = "Worker writes must stay inside its worktree.",
) -> Callable[[_Json, _Json], _Json]:
    """
    Factory: confine a worker's file writes to its worktree subtree.

    DENIES ``sys_os_write`` / ``sys_os_edit`` whose ``path`` is absolute
    or escapes upward (a ``..`` segment) — what a worker would do to write
    outside *allowed_root*. Relative in-tree paths are ALLOWED. Workers run
    with their worktree as cwd, so legitimate edits are always relative and
    in-tree; this catches escapes. Intended for the (unsandboxed)
    implementer worker specs, not the orchestrator.

    :param allowed_root: The worktree root workers are confined to, e.g.
        ``".worktrees"``. Used only in the deny message.
    :param deny_reason: Reason text surfaced on a DENY decision.
    :returns: An evaluator ``fn(event, config)`` returning a V0 decision.
    """

    # Match Omnigent built-in OS write/edit, Claude/Codex native Write/Edit
    # (surfaced via the PreToolUse hook), and Pi's native lowercase
    # write/edit (surfaced via the pi ``tool_call`` hook). Pi uses the same
    # ``path`` argument key as the Omnigent tools, so no Pi-specific arg
    # branch is needed below.
    _write_tools = {"sys_os_write", "sys_os_edit", "Write", "Edit", "write", "edit"}

    def _evaluate(event: _Json, config: _Json) -> _Json:  # noqa: ARG001
        """
        Reject worker file writes that escape the worktree subtree.

        :param event: V0 ``tool_call`` event for ``sys_os_write`` /
            ``sys_os_edit`` / Claude native ``Write`` / ``Edit``.
        :param config: Runtime config dict (unused).
        :returns: DENY on an absolute or ``..``-escaping path, else ALLOW.
        """
        args = _tool_call(event, _write_tools)
        if args is None:
            return _ALLOW
        # Omnigent tools use ``path``; Claude native tools use ``file_path``.
        path = args.get("path") or args.get("file_path")
        if not isinstance(path, str):
            return _ALLOW
        if path.startswith(("/", "~")) or ".." in path.split("/"):
            return _decision("DENY", f"{deny_reason} (outside {allowed_root}/: {path!r})")
        return _ALLOW

    return _evaluate


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.inner.nessie.policies.blast_radius",
        "kind": "factory",
        "name": "Block Dangerous Shell Commands (force-push, rm -rf)",
        "description": "Classifies shell commands (sys_os_shell, Claude/Codex native Bash, "
        "and Pi native bash) as safe, risky (ASK), or catastrophic (DENY) to prevent "
        "destructive operations like force-push or rm -rf /",
    },
    {
        "handler": "omnigent.inner.nessie.policies.spawn_bounds",
        "kind": "factory",
        "name": "Limit Sub-Agent Dispatches Per Turn",
        "description": "Limits the number of sub-agent dispatches per turn "
        "to prevent runaway fan-out",
    },
    {
        "handler": "omnigent.inner.nessie.policies.headless_subagent_purpose_guard",
        "kind": "factory",
        "name": "Require Purpose on Sub-Agent Dispatches",
        "description": "Requires every sub-agent dispatch to declare a purpose "
        "(implement, review, explore, search)",
    },
    {
        "handler": "omnigent.inner.nessie.policies.worktree_guard",
        "kind": "factory",
        "name": "Restrict Writes to Git Worktree",
        "description": "Blocks file writes (sys_os_write/edit, Claude/Codex native "
        "Write/Edit, and Pi native write/edit) outside the worker's git worktree to "
        "prevent cross-branch contamination",
    },
]
