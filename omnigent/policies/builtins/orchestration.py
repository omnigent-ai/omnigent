"""Bounds and blast-radius policies for the coding orchestrator.

Each public function is a :class:`FunctionPolicy` *factory*: it takes the
YAML ``factory_params`` as keyword arguments and returns an evaluator
callable ``fn(event[, config]) -> {"result": ..., "reason": ...}``.
The evaluators run runner-side at tool dispatch
(``omnigent/runner/policy.py``) and add no server routes.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

from omnigent.host import usage_limits as _usage_limits
from omnigent.host.usage_limits import (
    ACTIVE_MONITOR_MAX_AGE_SECONDS,
    DEFAULT_MONITORED_WINDOW_KINDS,
    DEFAULT_USAGE_THRESHOLD_PERCENT,
    VALID_WINDOW_KINDS,
    Provider,
    env_usage_threshold_percent,
    exceeded_usage_windows,
    format_usage_limit_denial,
    provider_for_harness,
    provider_for_subagent_name,
    resolve_usage_threshold_percent,
    usage_limits_disabled,
    validate_usage_threshold_percent,
)
from omnigent.policies.builtins._shell import SHELL_TOOLS
from omnigent.policies.builtins.safety import NATIVE_WRITE_TOOLS

# Heterogeneous JSON-shaped maps — the V0 policy event + decision payloads.
_Json: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# A ready ALLOW decision (the common case — most tool calls pass).
_ALLOW: _Json = {"result": "ALLOW"}

_SPAWN_BOUNDS_STATE_KEY = "_policy_spawn_bounds_dispatches"


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


def _tool_call(event: _Json, tool_names: Collection[str]) -> _Json | None:
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
_SHELL_TOOLS: frozenset[str] = SHELL_TOOLS


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
    risky_action: str = "ASK",
    deny_reason: str = "Blocked by the blast-radius policy.",
) -> Callable[[_Json, _Json], _Json]:
    """
    Factory: gate high-blast-radius shell commands by reversibility.

    Catastrophic, irreversible commands (force-push, ``rm -rf /``,
    hard-reset to a remote ref) are DENIED. Outward or destructive but
    recoverable commands (``git push``, ``gh pr merge``, ``rm -rf`` of a
    path, infra deploy/destroy) return ``risky_action``. Everything else
    — reads, tests, edits, and local git (commit / merge / worktree) — is
    ALLOWED.

    :param gate_pushes: When ``True`` (default), recoverable-but-outward
        commands return ``risky_action``. When ``False`` only the
        catastrophic DENY set is enforced — use only for trusted
        unattended batch runs.
    :param risky_action: Verdict for recoverable-but-outward commands:
        ``"ASK"`` (default) or ``"DENY"``.
    :param deny_reason: Reason text surfaced on a DENY decision.
    :returns: An evaluator ``fn(event, config)`` returning a V0 decision.
    :raises ValueError: If ``risky_action`` is not ``"ASK"`` or ``"DENY"``.
    """
    normalized_risky_action = risky_action.strip().upper()
    if normalized_risky_action not in {"ASK", "DENY"}:
        raise ValueError(
            f"blast_radius: risky_action must be 'ASK' or 'DENY', got {risky_action!r}"
        )

    def _evaluate(event: _Json, config: _Json) -> _Json:  # noqa: ARG001
        """
        Classify a ``sys_os_shell`` command by blast radius.

        :param event: V0 ``tool_call`` event for ``sys_os_shell``.
        :param config: Runtime config dict (unused; bounds come from the
            factory params).
        :returns: ALLOW / ASK / DENY decision dict.
        """
        # Native harnesses use different names for the same command-shaped
        # shell tool; all are normalized to a ``command`` argument.
        args = _tool_call(event, _SHELL_TOOLS)
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
            reason = (
                "High-blast-radius command needs approval"
                if normalized_risky_action == "ASK"
                else deny_reason
            )
            return _decision(normalized_risky_action, f"{reason}: {command!r}")
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
    bounded waves rather than an unbounded fleet. The count is persisted in
    ``session_state`` because the deployed server rebuilds its policy engine
    for every tool call. Every request-phase event resets the persisted count
    — that is each inbound user-role message, including a sub-agent wake
    notice — so a wave collected by a wake gets a fresh budget. The closure
    remains the runner-local fallback, reset through ``reset_turn``.

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
        if event.get("type") == "request":
            state["count"] = 0
            return {
                "result": "ALLOW",
                "state_updates": [
                    {"key": _SPAWN_BOUNDS_STATE_KEY, "action": "set", "value": 0},
                ],
            }
        if _tool_call(event, counted) is None:
            return _ALLOW
        session_state = event.get("session_state") or {}
        persisted = session_state.get(_SPAWN_BOUNDS_STATE_KEY, 0)
        persisted_count = persisted if isinstance(persisted, int) else 0
        # A fresh server engine starts the closure at 0 and reads the
        # persisted count; the runner gate has no session_state and keeps
        # counting in the closure. max() serves both without double counting.
        state["count"] = max(state["count"], persisted_count) + 1
        result: _Json = (
            _decision(
                "DENY",
                f"Exceeded {max_dispatches_per_turn} worker dispatches this turn; "
                "fan out in waves (collect the running batch before dispatching more).",
            )
            if state["count"] > max_dispatches_per_turn
            else {"result": "ALLOW"}
        )
        # Increment rather than set: an ASK from another policy defers this
        # write until approval, and replaying an absolute count would wind
        # back the dispatches counted in between.
        result["state_updates"] = [
            {"key": _SPAWN_BOUNDS_STATE_KEY, "action": "increment", "value": 1},
        ]
        return result

    def reset_turn() -> None:
        """
        Reset the closure's per-turn dispatch counter at each turn boundary.

        Clears only the runner-local closure; the persisted count in
        ``session_state`` is reset by the request-phase event instead.

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

    # Match Omnigent built-in OS write/edit, every Claude/Codex native write
    # tool (surfaced via the PreToolUse hook), and Pi's native lowercase
    # write/edit (surfaced via the pi ``tool_call`` hook). Pi uses the same
    # ``path`` argument key as the Omnigent tools, so no Pi-specific arg
    # branch is needed below.
    _write_tools = NATIVE_WRITE_TOOLS | {"sys_os_write", "sys_os_edit", "write", "edit"}

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
        # Omnigent tools use ``path``; Claude native tools use ``file_path``,
        # except NotebookEdit which uses ``notebook_path``. Check EVERY
        # path-like argument present, not the first truthy one: a decoy
        # in-tree ``path`` alongside an escaping ``notebook_path`` (or
        # ``file_path``) must still DENY, since the tool acts on its own
        # canonical key regardless of what else rides in the payload.
        paths = [
            value
            for key in ("path", "file_path", "notebook_path")
            if isinstance(value := args.get(key), str)
        ]
        for path in paths:
            # Backslashes are not valid in POSIX paths and could confuse
            # downstream processing into treating them as separators, slipping a
            # ``..\\`` past the split-on-'/' traversal check.
            if "\\" in path:
                return _decision("DENY", f"{deny_reason} (outside {allowed_root}/: {path!r})")
            # posixpath, NOT os.path: the tool contract is POSIX-shaped, and
            # ntpath.normpath rewrites "/" to "\", which makes the leading-"/" test
            # below inert on a Windows runner (absolute paths would ALLOW there).
            # normpath collapses ``..``/``.``/repeated slashes and pushes every
            # upward traversal to the front, so a single startswith catches every
            # escape form (e.g. "a/../../escape" → "../../escape").
            normalized = posixpath.normpath(path)
            if normalized.startswith(("/", "~", "..")):
                return _decision("DENY", f"{deny_reason} (outside {allowed_root}/: {path!r})")
            # A drive-qualified path ("C:/Windows/x") is absolute on Windows but
            # reads as an ordinary relative dir named "C:" to posixpath, so the test
            # above misses it. Checked on the NORMALIZED path, not the raw one:
            # normpath strips a leading "./" (and collapses "a/../C:/..."), which
            # would otherwise hide the drive letter from a raw-string check. UNC
            # ("//server/share") keeps its leading slashes and is caught above.
            # ASCII-only: Windows drives are [A-Za-z], but str.isalpha() is
            # Unicode-aware and would also reject a relative dir named e.g. "Ω:".
            drive = normalized[:1]
            if drive.isascii() and drive.isalpha() and normalized[1:2] == ":":
                return _decision("DENY", f"{deny_reason} (outside {allowed_root}/: {path!r})")
        return _ALLOW

    return _evaluate


def read_only_os(
    *,
    deny_reason: str = (
        "This agent is report-only: it may read files and run shell, but never "
        "write or edit them. Describe the change in your report instead of applying it."
    ),
) -> Callable[[_Json, _Json], _Json]:
    """
    Factory: deny every file-mutating tool call (report-only agents).

    DENIES ``sys_os_write`` / ``sys_os_edit`` and the Claude/Codex/Pi native
    ``Write`` / ``Edit`` / ``MultiEdit`` / ``NotebookEdit`` aliases. Reads, searches, and shell
    commands are left untouched — pair with :func:`blast_radius` to also bound
    shell blast radius. Use on agents whose contract is to investigate and
    report, never to change code (e.g. a security reviewer and its read-only
    sub-agents): unlike prompt discipline alone, an accidental ``sys_os_edit``
    is refused at the policy layer.

    :param deny_reason: Reason text surfaced on a DENY decision.
    :returns: An evaluator ``fn(event, config)`` returning DENY for any
        write/edit tool call, ALLOW otherwise.
    """

    # Match Omnigent built-in OS write/edit, every Claude/Codex native write
    # tool, and Pi's native lowercase write/edit — the same tool set
    # worktree_guard gates, so the two write policies stay in lockstep.
    write_tools = NATIVE_WRITE_TOOLS | {"sys_os_write", "sys_os_edit", "write", "edit"}

    def _evaluate(event: _Json, config: _Json) -> _Json:  # noqa: ARG001
        """
        Deny any file-mutating tool call.

        :param event: V0 ``tool_call`` event.
        :param config: Runtime config dict (unused).
        :returns: DENY for a write/edit tool, ALLOW otherwise.
        """
        if _tool_call(event, write_tools) is None:
            return _ALLOW
        return _decision("DENY", deny_reason)

    return _evaluate


@dataclass(frozen=True)
class SubscriptionUsageGuardConfig:
    """Resolved runtime parameters for subscription usage-limit preemption."""

    threshold_percent: float
    window_kinds: frozenset[str]
    dispatch_tools: frozenset[str]


def _normalize_window_kinds(window_kinds: Sequence[str]) -> frozenset[str]:
    if isinstance(window_kinds, (str, bytes)) or not isinstance(window_kinds, Sequence):
        raise ValueError("subscription_usage_guard: window_kinds must be a non-empty sequence")
    normalized = frozenset(str(kind).strip() for kind in window_kinds if isinstance(kind, str))
    if not normalized or len(normalized) != len(window_kinds):
        raise ValueError("subscription_usage_guard: window_kinds must contain non-empty strings")
    unknown = sorted(normalized - VALID_WINDOW_KINDS)
    if unknown:
        raise ValueError(
            f"subscription_usage_guard: unknown window_kinds {unknown!r}; "
            f"must be chosen from {sorted(VALID_WINDOW_KINDS)!r}"
        )
    return normalized


def _normalize_dispatch_tools(dispatch_tools: Sequence[str]) -> frozenset[str]:
    if isinstance(dispatch_tools, (str, bytes)) or not isinstance(dispatch_tools, Sequence):
        raise ValueError("subscription_usage_guard: dispatch_tools must be a non-empty sequence")
    normalized = frozenset(
        tool.strip() for tool in dispatch_tools if isinstance(tool, str) and tool.strip()
    )
    if not normalized or len(normalized) != len(dispatch_tools):
        raise ValueError("subscription_usage_guard: dispatch_tools must contain non-empty strings")
    return normalized


def extract_subscription_usage_guard_config(
    spec: object | None,
) -> SubscriptionUsageGuardConfig | None:
    """Resolve active ``subscription_usage_guard`` settings from *spec* or environment."""
    if usage_limits_disabled():
        return None
    guardrails = getattr(spec, "guardrails", None) if spec is not None else None
    policies = getattr(guardrails, "policies", None) or ()
    for policy_spec in policies:
        if getattr(policy_spec, "enabled", True) is False:
            continue
        func_ref = getattr(policy_spec, "function", None)
        path = getattr(func_ref, "path", None)
        if not isinstance(path, str) or not path.endswith(".subscription_usage_guard"):
            continue
        raw_args = getattr(func_ref, "arguments", None)
        args = raw_args if isinstance(raw_args, Mapping) else {}
        raw_threshold = args.get("threshold_percent")
        configured_threshold = (
            validate_usage_threshold_percent(raw_threshold)
            if raw_threshold is not None
            else DEFAULT_USAGE_THRESHOLD_PERCENT
        )
        raw_windows = args.get("window_kinds")
        window_kinds = (
            _normalize_window_kinds(raw_windows)
            if isinstance(raw_windows, Sequence) and not isinstance(raw_windows, (str, bytes))
            else frozenset(DEFAULT_MONITORED_WINDOW_KINDS)
        )
        raw_tools = args.get("dispatch_tools")
        dispatch_tools = (
            _normalize_dispatch_tools(raw_tools)
            if isinstance(raw_tools, Sequence) and not isinstance(raw_tools, (str, bytes))
            else frozenset({"sys_session_send", "sys_session_create"})
        )
        return SubscriptionUsageGuardConfig(
            threshold_percent=resolve_usage_threshold_percent(configured_threshold),
            window_kinds=window_kinds,
            dispatch_tools=dispatch_tools,
        )
    env_threshold = env_usage_threshold_percent()
    if env_threshold is not None:
        return SubscriptionUsageGuardConfig(
            threshold_percent=env_threshold,
            window_kinds=frozenset(DEFAULT_MONITORED_WINDOW_KINDS),
            dispatch_tools=frozenset({"sys_session_send", "sys_session_create"}),
        )
    return None


def _provider_from_config_path(config_path: object) -> Provider | None:
    if not isinstance(config_path, str) or not config_path.strip():
        return None
    trimmed = config_path.strip().replace("\\", "/").rstrip("/")
    for suffix in ("/config.yaml", "/config.yml", ".yaml", ".yml"):
        if trimmed.endswith(suffix):
            trimmed = trimmed[: -len(suffix)]
            break
    slug = posixpath.basename(trimmed)
    return provider_for_subagent_name(slug)


def _target_subscription_provider(event: _Json, args: _Json) -> Provider | None:
    data = event.get("data")
    tool_name = data.get("name") if isinstance(data, Mapping) else None
    nested_args = args.get("args")
    if isinstance(nested_args, Mapping):
        raw_harness = nested_args.get("harness")
        if isinstance(raw_harness, str) and raw_harness.strip():
            return provider_for_harness(raw_harness)
    top_harness = args.get("harness")
    if isinstance(top_harness, str) and top_harness.strip():
        return provider_for_harness(top_harness)

    if tool_name == "sys_session_create":
        for key in ("agent_id", "agent"):
            candidate = args.get(key)
            if isinstance(candidate, str) and candidate.strip():
                resolved = provider_for_harness(candidate) or provider_for_subagent_name(candidate)
                if resolved is not None:
                    return resolved
        return _provider_from_config_path(args.get("config_path"))

    agent_name = args.get("agent")
    if not isinstance(agent_name, str) or not agent_name.strip():
        return None
    context = event.get("context")
    sub_agent_providers = (
        context.get("sub_agent_providers") if isinstance(context, Mapping) else None
    )
    if isinstance(sub_agent_providers, Mapping):
        mapped = sub_agent_providers.get(agent_name)
        return mapped if mapped in ("claude", "codex") else None
    return provider_for_subagent_name(agent_name)


def subscription_usage_guard(
    *,
    threshold_percent: float = DEFAULT_USAGE_THRESHOLD_PERCENT,
    window_kinds: Sequence[str] = DEFAULT_MONITORED_WINDOW_KINDS,
    dispatch_tools: Sequence[str] = ("sys_session_send", "sys_session_create"),
) -> Callable[[_Json], Awaitable[_Json]]:
    """
    Factory: hold off sub-agent dispatches once subscription usage reaches *threshold_percent*.

    Probes the local Claude or Codex CLI usage windows for subscription-backed
    workers and returns ``DENY`` when any monitored window (5-hour ``session`` or
    ``weekly`` by default) is at or above the threshold. API-key and gateway
    setups report ``unavailable`` rate limits and pass through unaffected.
    """
    configured_threshold = validate_usage_threshold_percent(
        threshold_percent, source="subscription_usage_guard: threshold_percent"
    )
    monitored_windows = _normalize_window_kinds(window_kinds)
    monitored_tools = _normalize_dispatch_tools(dispatch_tools)

    async def _evaluate(event: _Json) -> _Json:
        args = _tool_call(event, monitored_tools)
        if args is None or usage_limits_disabled():
            return _ALLOW
        provider = _target_subscription_provider(event, args)
        if provider is None:
            return _ALLOW
        effective_threshold = resolve_usage_threshold_percent(configured_threshold)
        readings = await _usage_limits.read_usage_limits(
            (provider,), max_age_seconds=ACTIVE_MONITOR_MAX_AGE_SECONDS
        )
        exceeded = [
            window
            for reading in readings
            for window in exceeded_usage_windows(
                reading,
                threshold_percent=effective_threshold,
                window_kinds=monitored_windows,
            )
        ]
        if exceeded:
            return _decision(
                "DENY",
                format_usage_limit_denial(
                    provider,
                    exceeded,
                    threshold_percent=effective_threshold,
                ),
            )
        return _ALLOW

    return _evaluate


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, object]] = [
    {
        "handler": "omnigent.policies.builtins.orchestration.blast_radius",
        "kind": "factory",
        "name": "Block Dangerous Shell Commands",
        "description": "Allows safe shell commands, applies a configurable ASK or DENY action "
        "to recoverable risky commands, and always denies catastrophic commands such as "
        "force-push or rm -rf /. Supports Omnigent, Claude/Codex, Cursor, Pi, Hermes, and Goose.",
        "params_schema": {
            "type": "object",
            "properties": {
                "gate_pushes": {
                    "type": "boolean",
                    "description": "Controls recoverable risky commands such as ordinary pushes, "
                    "scoped recursive deletes, PR merges, and deployments. True applies "
                    "risky_action; false allows them without prompting. Catastrophic commands "
                    "are always denied.",
                    "default": True,
                },
                "risky_action": {
                    "type": "string",
                    "enum": ["ASK", "DENY"],
                    "description": "Action when gate_pushes is true: ASK prompts the user before "
                    "running the command; DENY blocks it immediately. This setting never "
                    "weakens catastrophic-command denial.",
                    "default": "ASK",
                },
                "deny_reason": {
                    "type": "string",
                    "description": "Message shown when the policy returns DENY, including "
                    "catastrophic commands and recoverable commands blocked by "
                    "risky_action=DENY.",
                    "default": "Blocked by the blast-radius policy.",
                },
            },
        },
    },
    {
        "handler": "omnigent.policies.builtins.orchestration.spawn_bounds",
        "kind": "factory",
        "name": "Limit Sub-Agent Dispatches Per Turn",
        "description": "Limits the number of sub-agent dispatches per turn "
        "to prevent runaway fan-out",
    },
    {
        "handler": "omnigent.policies.builtins.orchestration.headless_subagent_purpose_guard",
        "kind": "factory",
        "name": "Require Purpose on Sub-Agent Dispatches",
        "description": "Requires every sub-agent dispatch to declare a purpose "
        "(implement, review, explore, search)",
    },
    {
        "handler": "omnigent.policies.builtins.orchestration.worktree_guard",
        "kind": "factory",
        "name": "Restrict Writes to Git Worktree",
        "description": "Blocks file writes (sys_os_write/edit, Claude/Codex native "
        "Write/Edit/MultiEdit/NotebookEdit, and Pi native write/edit) outside the worker's "
        "git worktree to prevent cross-branch contamination",
    },
    {
        "handler": "omnigent.policies.builtins.orchestration.read_only_os",
        "kind": "factory",
        "name": "Report-Only (Deny File Writes)",
        "description": "Denies every file-mutating tool (sys_os_write/edit, Claude/Codex "
        "native Write/Edit/MultiEdit/NotebookEdit, and Pi native write/edit) so a "
        "report-only agent can read and run shell but never change code",
    },
    {
        "handler": "omnigent.policies.builtins.orchestration.subscription_usage_guard",
        "kind": "factory",
        "name": "Hold Off Sub-Agents Near Subscription Usage Limit",
        "description": "Checks local Claude and Codex CLI subscription usage windows "
        "(5-hour session and weekly) and holds off new dispatches once utilization "
        "reaches the configured threshold percentage",
        "params_schema": {
            "type": "object",
            "properties": {
                "threshold_percent": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Utilization percentage at or above which new sub-agent "
                    "dispatches to that subscription provider are held off (overridable via "
                    "OMNIGENT_USAGE_LIMIT_THRESHOLD_PERCENT).",
                    "default": 90,
                },
                "window_kinds": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["session", "weekly", "weekly_model", "monthly"],
                    },
                    "description": "Usage window kinds that trigger hold-off when any reaches "
                    "threshold_percent.",
                    "default": ["session", "weekly"],
                },
                "dispatch_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Orchestration tool names gated against subscription usage.",
                    "default": ["sys_session_send", "sys_session_create"],
                },
            },
        },
    },
]
