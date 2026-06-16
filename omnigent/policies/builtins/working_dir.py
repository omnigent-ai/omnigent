"""Built-in working-directory / worktree policy (shell-surface).

A single factory, :func:`block_working_dir_changes`, that gates shell commands
which move the agent out of its current working directory or switch git
worktrees. It addresses the "I don't want the agent to switch working
directories or worktrees" guardrail: an agent confined to a workspace should
not be able to ``cd`` elsewhere or spin up / relocate a linked worktree and
operate from there.

It gates two things, dispatched off ``sys_os_shell`` (the built-in OS shell
tool) ``command`` strings:

- **Directory changes** (``block_cd``) — ``cd`` / ``chdir`` / ``pushd`` /
  ``popd`` shell built-ins, plus ``git -C <path> …`` (which runs git as if in
  another directory). When ``allowed_dirs`` is set, a ``cd`` into one of those
  directories (or a subdirectory) is permitted; everything else is gated.
- **Worktree switches** (``block_worktree``) — ``git worktree add`` /
  ``move`` / ``remove``, the subcommands that create, relocate, or delete a
  linked working tree. Read-only / maintenance worktree subcommands
  (``list`` / ``prune`` / ``lock`` / ``unlock`` / ``repair``) are not gated —
  they do not switch the agent into a different tree.

Like the GitHub built-in, the command string is parsed robustly: commands are
split on chaining operators (``cd /x && …``), wrapper / env prefixes are
stripped (``sudo`` / ``env`` / ``VAR=x``), and shell-interpreter / ``eval``
wrappers (``bash -c "cd /x"``) are unwrapped and re-parsed — so the gate
cannot be bypassed by chaining or wrapping. A segment that looks like a gated
command but cannot be tokenized (e.g. unbalanced quotes) is surfaced via the
configured ``action`` rather than silently allowed.

The policy abstains (returns ``None``) on every event that is not a gated
shell command, so it composes freely with other policies.

YAML usage::

    policies:
      no_dir_switch:
        type: function
        function:
          path: omnigent.policies.builtins.working_dir.block_working_dir_changes
          arguments:
            allowed_dirs: ["/workspace"]
            action: deny
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    real_invocation_tokens,
    split_command_segments,
    unwrap_shell_command,
)
from omnigent.policies.schema import PolicyEvent, PolicyResponse

# Shell built-ins that change the process / shell working directory.
_CD_COMMANDS: frozenset[str] = frozenset({"cd", "chdir", "pushd", "popd"})

# git worktree subcommands that create, relocate, or delete a linked working
# tree — i.e. that "switch" worktrees. ``list`` / ``prune`` / ``lock`` /
# ``unlock`` / ``repair`` are maintenance/read ops and are not gated.
_WORKTREE_SWITCH_SUBCMDS: frozenset[str] = frozenset({"add", "move", "remove"})

# git global options that consume a *separate* following token as their value,
# so the scanner skips both when locating the subcommand. ``-C`` additionally
# names a directory the command runs in.
_GIT_VALUE_OPTS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
)

# Shell tools whose command string this policy parses, by default.
# Includes both the Omnigent built-in OS shell and Claude Code / Codex
# native ``Bash`` tool (surfaced via the ``PreToolUse`` hook contract).
_DEFAULT_SHELL_TOOLS: tuple[str, ...] = ("sys_os_shell", "Bash")


@dataclass(frozen=True)
class _DirOp:
    """
    One directory-switching operation parsed from a shell command segment.

    :param kind: ``"cd"`` for a working-directory change (``cd`` family or
        ``git -C``), or ``"worktree"`` for a git worktree switch.
    :param target: For ``kind == "cd"``, the directory the command moves into,
        or ``None`` when it cannot be determined (``popd`` / bare ``cd`` /
        ``cd -``). Always ``None`` for ``kind == "worktree"``.
    :param detail: Short human-readable description for the decision reason,
        e.g. ``"cd /etc"``, ``"git -C /tmp"``, or ``"git worktree add"``.
    """

    kind: str
    target: str | None
    detail: str


def _normalize_dir(path: str) -> str:
    """
    Reduce a directory reference to a normalized path with no trailing slash.

    :param path: A directory path, e.g. ``"/workspace/"`` or ``"/a/./b"``.
    :returns: The ``os.path.normpath`` of the trimmed input (``"/workspace"`` /
        ``"/a/b"``), or empty string when *path* is blank.
    """
    stripped = path.strip()
    return os.path.normpath(stripped) if stripped else ""


def _normalize_dirs(values: list[str] | None) -> frozenset[str]:
    """
    Normalize a list of directory references to a set of normalized paths.

    :param values: Directory paths, e.g. ``["/workspace", "/tmp/"]``. ``None``
        → empty list.
    :returns: Set of normalized paths, blank entries dropped.
    """
    return frozenset(d for value in (values or []) if (d := _normalize_dir(value)))


def _is_under_allowed(target: str, allowed_dirs: frozenset[str]) -> bool:
    """
    Whether a target directory equals or sits under one of the allowed dirs.

    :param target: The directory a ``cd`` / ``git -C`` moves into, e.g.
        ``"/workspace/sub"``.
    :param allowed_dirs: Normalized allowed directories, e.g.
        ``frozenset({"/workspace"})``.
    :returns: ``True`` if the normalized *target* is one of *allowed_dirs* or a
        subdirectory of one. A relative *target* only matches a relative
        allowed entry (paths are compared after ``normpath`` without resolving
        against a cwd the policy does not know).
    """
    norm = _normalize_dir(target)
    if not norm:
        return False
    return any(norm == allowed or norm.startswith(allowed + os.sep) for allowed in allowed_dirs)


def _classify_git(tokens: list[str], block_cd: bool, block_worktree: bool) -> _DirOp | None:
    """
    Classify a ``git`` invocation into a directory/worktree :class:`_DirOp`.

    Scans past git's global options (skipping the value token of options like
    ``-C`` / ``-c`` that take one) to find the subcommand, capturing any
    ``-C <path>`` directory.

    :param tokens: Tokens starting at ``git``, e.g.
        ``["git", "-C", "/tmp", "worktree", "add", "wt"]``.
    :param block_cd: Whether ``git -C <path>`` (run-in-other-directory) is gated.
    :param block_worktree: Whether ``git worktree add/move/remove`` is gated.
    :returns: A worktree :class:`_DirOp` for a gated worktree switch, a ``cd``
        :class:`_DirOp` for a gated ``git -C``, or ``None`` when neither
        applies (local git command, or the relevant gate is off).
    """
    rest = tokens[1:]
    c_path: str | None = None
    positionals: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in _GIT_VALUE_OPTS and i + 1 < len(rest):
            if token == "-C":
                c_path = rest[i + 1]
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        positionals.append(token)
        i += 1

    subcmd = positionals[0] if positionals else None
    if block_worktree and subcmd == "worktree":
        action_word = positionals[1] if len(positionals) > 1 else ""
        if action_word in _WORKTREE_SWITCH_SUBCMDS:
            return _DirOp(kind="worktree", target=None, detail=f"git worktree {action_word}")
    if block_cd and c_path is not None:
        return _DirOp(kind="cd", target=c_path, detail=f"git -C {c_path}")
    return None


def _classify_cd(tokens: list[str]) -> _DirOp:
    """
    Classify a ``cd`` / ``chdir`` / ``pushd`` / ``popd`` invocation.

    :param tokens: Tokens starting at the cd-family command, e.g.
        ``["cd", "/etc"]``, ``["pushd", "/tmp"]``, or ``["popd"]``.
    :returns: A ``cd`` :class:`_DirOp`. ``target`` is the first non-flag
        argument (``"/etc"``), or ``None`` for a switch with no explicit
        directory (``popd``, bare ``cd`` → home, ``cd -`` → previous dir).
    """
    cmd = tokens[0]
    args = [token for token in tokens[1:] if not token.startswith("-")]
    target = args[0] if args else None
    detail = f"{cmd} {target}" if target else cmd
    return _DirOp(kind="cd", target=target, detail=detail)


def _classify_segment(tokens: list[str], block_cd: bool, block_worktree: bool) -> _DirOp | None:
    """
    Classify one real-invocation token list into a gated :class:`_DirOp`.

    :param tokens: Real-invocation tokens of a single segment (wrappers / env
        already stripped, shell wrappers already unwrapped), e.g.
        ``["cd", "/etc"]`` or ``["git", "worktree", "add", "wt"]``.
    :param block_cd: Whether directory-change commands are gated.
    :param block_worktree: Whether worktree-switch commands are gated.
    :returns: The parsed :class:`_DirOp`, or ``None`` when the segment is not a
        gated command.
    """
    # Strip a leading subshell paren so ``(cd /x`` is still seen as ``cd``.
    head = tokens[0].lstrip("(")
    if head == "git":
        git_tokens = ["git", *tokens[1:]]
        return _classify_git(git_tokens, block_cd, block_worktree)
    if block_cd and head in _CD_COMMANDS:
        return _classify_cd([head, *tokens[1:]])
    return None


def _segment_hides_gated_command(tokens: list[str], block_cd: bool, block_worktree: bool) -> bool:
    """
    Whether a gated cd/worktree command sits behind an unrecognized leading token.

    :func:`real_invocation_tokens` only strips known command wrappers; an
    unknown one (``stdbuf``, ``nice`` …) leaves a real ``cd`` / ``git -C`` /
    ``git worktree`` deeper in the token list, where the head dispatch in
    :func:`_classify_segment` misses it and the segment is silently allowed.
    This is the fail-closed backstop, matching on actual command tokens (not a
    bare mention, so a quoted ``echo "cd /etc"`` stays one token and does not
    trip it).

    :param tokens: Real-invocation tokens of a segment whose leading token was
        not itself a recognized gated command.
    :param block_cd: Whether directory-change commands are gated.
    :param block_worktree: Whether worktree-switch commands are gated.
    :returns: ``True`` if a gated command appears behind the leading token.
    """
    for i in range(1, len(tokens)):
        word = tokens[i].lstrip("(")
        if block_cd and word in _CD_COMMANDS:
            return True
        if word == "git":
            rest = tokens[i + 1 :]
            if block_worktree and "worktree" in rest:
                return True
            if block_cd and any(t == "-C" or t.startswith("-C") for t in rest):
                return True
    return False


def _looks_like_dir_op(segment: str, block_cd: bool, block_worktree: bool) -> bool:
    """
    Heuristic: does an un-tokenizable segment appear to switch dir/worktree?

    Used only when ``shlex`` cannot parse a segment (e.g. unbalanced quotes),
    to decide whether to surface it for the configured action rather than let
    a possibly-gated command through unchecked.

    :param segment: The raw, un-tokenizable command segment, e.g.
        ``'cd "/etc'``.
    :param block_cd: Whether directory-change commands are gated.
    :param block_worktree: Whether worktree-switch commands are gated.
    :returns: ``True`` if the segment mentions a gated command keyword.
    """
    if block_cd and re.search(r"\b(cd|chdir|pushd|popd)\b", segment):
        return True
    if re.search(r"\bgit\b", segment):
        if block_worktree and re.search(r"\bworktree\b", segment):
            return True
        if block_cd and re.search(r"(?<!\S)-C\b", segment):
            return True
    return False


def block_working_dir_changes(
    *,
    block_cd: bool = True,
    block_worktree: bool = True,
    allowed_dirs: list[str] | None = None,
    action: str = "deny",
    shell_tools: list[str] | None = None,
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """
    Build a policy callable that gates working-directory and worktree switches.

    :param block_cd: Gate ``cd`` / ``chdir`` / ``pushd`` / ``popd`` and
        ``git -C <path>``. Defaults to ``True``.
    :param block_worktree: Gate ``git worktree add`` / ``move`` / ``remove``.
        Defaults to ``True``.
    :param allowed_dirs: Directories a ``cd`` / ``git -C`` may move into (the
        directory itself or a subdirectory is allowed), as paths e.g.
        ``["/workspace"]``. ``None`` / empty means no directory change is
        allowed (every gated ``cd`` fails). Does not affect worktree gating.
    :param action: What a gated command yields — ``"deny"`` (block it) or
        ``"ask"`` (park for human approval). Defaults to ``"deny"``.
    :param shell_tools: Names of the shell tools whose ``command`` argument is
        parsed. ``None`` uses the defaults: ``["sys_os_shell", "Bash"]``
        (Omnigent built-in + Claude/Codex native). Commands run through
        a tool not listed here are not inspected.
    :returns: A one-argument policy callable returning a :class:`PolicyResponse`
        (DENY / ASK) on a gated command, or ``None`` to abstain (ALLOW).
    :raises ValueError: If *action* is not ``"deny"`` / ``"ask"``, or if both
        *block_cd* and *block_worktree* are ``False`` (the policy could then
        never fire — fail loud at spec load rather than ship a no-op gate).
    """
    if action not in ("deny", "ask"):
        raise ValueError(f"action must be 'deny' or 'ask', got {action!r}")
    if not block_cd and not block_worktree:
        raise ValueError(
            "block_working_dir_changes gates nothing when both block_cd and "
            "block_worktree are False; enable at least one."
        )

    normalized_allowed = _normalize_dirs(allowed_dirs)
    shell_tool_names = (
        frozenset(shell_tools) if shell_tools is not None else frozenset(_DEFAULT_SHELL_TOOLS)
    )
    result_kind = "DENY" if action == "deny" else "ASK"

    def _violation(reason: str) -> PolicyResponse:
        """
        Build the configured DENY / ASK response for a gated command.

        :param reason: Human-readable explanation, e.g.
            ``"Blocked: 'cd /etc' changes the working directory."``.
        :returns: A :class:`PolicyResponse` carrying the configured action.
        """
        return {"result": result_kind, "reason": reason}

    def _gate(op: _DirOp) -> PolicyResponse | None:
        """
        Apply the policy to one parsed :class:`_DirOp`.

        :param op: The parsed directory/worktree operation.
        :returns: ``None`` to allow (a ``cd`` into an allowed dir), or a
            :class:`PolicyResponse` carrying the configured action.
        """
        if op.kind == "worktree":
            return _violation(
                f"Blocked: '{op.detail}' switches git worktrees, which this policy forbids."
            )
        # op.kind == "cd"
        if (
            normalized_allowed
            and op.target is not None
            and _is_under_allowed(op.target, normalized_allowed)
        ):
            return None
        if normalized_allowed:
            return _violation(
                f"Blocked: '{op.detail}' changes the working directory outside the allowed "
                f"directories {sorted(normalized_allowed)}."
            )
        return _violation(
            f"Blocked: '{op.detail}' changes the working directory, which this policy forbids."
        )

    def _evaluate_command(command: str, _depth: int = 0) -> PolicyResponse | None:
        """
        Evaluate a shell command string for gated dir/worktree switches.

        Splits the command into segments, unwraps shell-interpreter / ``eval``
        wrappers recursively, and returns the most restrictive decision across
        all gated ops found (DENY > ASK > abstain).

        :param command: The shell command string, e.g. ``"cd /etc && ls"``.
        :param _depth: Internal recursion guard for nested shell wrappers;
            callers leave it 0.
        :returns: A :class:`PolicyResponse` for a gated command, or ``None``
            when nothing in the command is gated.
        """
        if _depth > MAX_SHELL_NESTING:
            return None
        worst: PolicyResponse | None = None
        for segment in split_command_segments(command):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                # Unbalanced quotes etc.: if it looks like a gated command, surface
                # it via the configured action rather than letting it through.
                if _looks_like_dir_op(segment, block_cd, block_worktree):
                    worst = _worse(
                        worst,
                        _violation(
                            f"Blocked: could not parse a shell command ({segment[:60]!r}) that "
                            f"appears to switch directories or worktrees."
                        ),
                    )
                continue
            tokens = real_invocation_tokens(tokens)
            if not tokens:
                continue
            inner = unwrap_shell_command(tokens)
            if inner is not None:
                worst = _worse(worst, _evaluate_command(inner, _depth + 1))
                continue
            op = _classify_segment(tokens, block_cd, block_worktree)
            if op is not None:
                worst = _worse(worst, _gate(op))
            elif _segment_hides_gated_command(tokens, block_cd, block_worktree):
                # An unrecognized leading wrapper hides a gated cd/worktree
                # command — fail closed via the configured action.
                worst = _worse(
                    worst,
                    _violation(
                        f"Blocked: a shell command ({segment[:60]!r}) appears to switch "
                        f"directories or worktrees behind an unrecognized wrapper."
                    ),
                )
        return worst

    def _evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Evaluate one policy event against the dir/worktree rules.

        Acts on ``tool_call`` events for the configured shell tools only;
        abstains on everything else so the policy composes with others.

        :param event: The policy event.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain.
        """
        if event.get("type") != "tool_call":
            return None
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        raw_tool = data.get("name")
        if raw_tool not in shell_tool_names:
            return None
        args = data.get("arguments")
        args = args if isinstance(args, dict) else {}
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        return _evaluate_command(command)

    return _evaluate


def _worse(
    current: PolicyResponse | None, candidate: PolicyResponse | None
) -> PolicyResponse | None:
    """
    Return the more restrictive of two decisions (DENY > ASK > ALLOW).

    :param current: The decision accumulated so far (``None`` = abstain/ALLOW).
    :param candidate: A new decision to fold in (``None`` = abstain/ALLOW).
    :returns: Whichever decision is more restrictive; ``current`` on a tie.
    """
    rank = {"DENY": 3, "ASK": 2}
    current_rank = rank.get(current["result"], 1) if current else 1
    candidate_rank = rank.get(candidate["result"], 1) if candidate else 1
    return candidate if candidate_rank > current_rank else current


# ── Registry ───────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [  # type: ignore[explicit-any]
    {
        "handler": "omnigent.policies.builtins.working_dir.block_working_dir_changes",
        "kind": "factory",
        "name": "Block Working Directory & Worktree Changes",
        "description": (
            "Gates shell commands (sys_os_shell and Claude/Codex native Bash) "
            "that switch the working directory (cd / chdir / pushd / popd, "
            "git -C) or git worktrees (git worktree add / move / remove). "
            "Optionally allow cd into specific directories via allowed_dirs. "
            "Chained, wrapped (bash -c), and env-prefixed commands are parsed "
            "so the gate cannot be trivially bypassed."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "block_cd": {
                    "type": "boolean",
                    "description": "Gate cd/chdir/pushd/popd and git -C.",
                    "default": True,
                },
                "block_worktree": {
                    "type": "boolean",
                    "description": "Gate git worktree add/move/remove.",
                    "default": True,
                },
                "allowed_dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Directories a cd / git -C may move into "
                    "(the dir or a subdirectory). Empty = no change allowed.",
                },
                "action": {
                    "type": "string",
                    "enum": ["deny", "ask"],
                    "description": "Whether a gated command is denied or sent "
                    "for human approval (ask).",
                    "default": "deny",
                },
                "shell_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Shell tools whose command arg is parsed "
                    "(default: sys_os_shell, Bash).",
                },
            },
        },
    },
]
