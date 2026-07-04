"""Unit tests for nessie's bounds + blast-radius policies.

These exercise the real :mod:`omnigent.inner.nessie.policies` evaluator
logic. The callables take and return plain dicts, so no mocks are needed —
the tests construct real V0 event dicts and assert on the decision. Each
test fails if the corresponding guard regresses (a command mis-classified,
the per-turn cap broken, or a worktree escape let through).
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.inner.nessie.policies import (
    blast_radius,
    headless_subagent_purpose_guard,
    read_only_os,
    spawn_bounds,
    worktree_guard,
)


def _tool_call(tool: str, **args: Any) -> dict[str, Any]:
    """
    Build a V0 ``tool_call`` event for *tool* with *args*.

    :param tool: The tool name the engine reports, e.g. ``"sys_os_shell"``.
    :param args: The tool's argument dict, e.g. ``command="git status"``.
    :returns: An event dict shaped like the policy engine delivers, i.e.
        ``{"type": "tool_call", "data": {"name": ..., "arguments": {...}}}``.
    """
    return {"type": "tool_call", "data": {"name": tool, "arguments": dict(args)}}


def _result(decision: dict[str, Any]) -> str:
    """:returns: the ``result`` string of a policy decision dict."""
    return decision["result"]


@pytest.mark.parametrize(
    "command,expected",
    [
        # Local / read-only work is always allowed.
        ("git status", "ALLOW"),
        ("pytest tests/ -q", "ALLOW"),
        ("git commit -m wip", "ALLOW"),
        ("git merge --no-ff nessie/t1", "ALLOW"),
        ("git worktree add .worktrees/t1 -b nessie/t1", "ALLOW"),
        # Outward / destructive-but-recoverable → ASK the human.
        ("git push origin main", "ASK"),
        ("gh pr merge 42", "ASK"),
        ("terraform apply", "ASK"),
        ("rm -rf build", "ASK"),
        # Irreversible → DENY outright.
        ("git push --force origin main", "DENY"),
        ("git push -f", "DENY"),
        ("rm -rf /", "DENY"),
        ("git reset --hard origin/main", "DENY"),
    ],
)
def test_blast_radius_classifies_commands(command: str, expected: str) -> None:
    """
    blast_radius gates shell commands by reversibility.

    A wrong result means a mis-classification: e.g. if ``git push`` returned
    ALLOW the outward-push gate regressed (silent unreviewed pushes); if
    ``git commit`` returned ASK the policy over-blocks ordinary local work and
    would stall every run on an approval prompt.
    """
    evaluate = blast_radius()
    assert _result(evaluate(_tool_call("sys_os_shell", command=command), {})) == expected


def test_blast_radius_gates_native_bash_tool() -> None:
    """
    blast_radius must also gate Claude/Codex native ``Bash`` tool calls
    (surfaced via the ``PreToolUse`` hook contract).

    Both harnesses' shell tool reaches the policy layer as a ``Bash``
    tool_call with a string ``command`` — codex-native normalizes its
    ``shell`` tool to this Claude-compatible hook shape (verified by a live
    capture), so the single ``Bash`` match set covers both. If this returns
    ALLOW, a native-harness ``git push --force`` bypasses the gate entirely.
    """
    evaluate = blast_radius()
    # Catastrophic via native Bash — should DENY.
    assert (
        _result(evaluate(_tool_call("Bash", command="git push --force origin main"), {})) == "DENY"
    )
    # Recoverable via native Bash — should ASK.
    assert _result(evaluate(_tool_call("Bash", command="git push origin main"), {})) == "ASK"
    # Safe via native Bash — should ALLOW.
    assert _result(evaluate(_tool_call("Bash", command="git status"), {})) == "ALLOW"


def test_blast_radius_gates_pi_native_bash_tool() -> None:
    """
    blast_radius must also gate Pi's lowercase native ``bash`` tool.

    Pi surfaces its in-process shell as ``bash`` (lowercase) with the same
    string ``command`` key via the pi ``tool_call`` hook — distinct from the
    Claude/Codex ``Bash`` casing. If this returns ALLOW, a pi worker's
    ``git push --force`` bypasses the catastrophic-command gate entirely.
    """
    evaluate = blast_radius()
    assert (
        _result(evaluate(_tool_call("bash", command="git push --force origin main"), {})) == "DENY"
    )
    assert _result(evaluate(_tool_call("bash", command="git push origin main"), {})) == "ASK"
    assert _result(evaluate(_tool_call("bash", command="git status"), {})) == "ALLOW"


def test_blast_radius_ignores_non_shell_tools() -> None:
    """
    Non-shell tool calls pass through ALLOW — blast_radius only inspects
    ``sys_os_shell`` and ``Bash``. A failure here means the guard is matching
    on the wrong tool and would corrupt unrelated tool dispatch.
    """
    evaluate = blast_radius()
    assert _result(evaluate(_tool_call("sys_session_send", agent="impl_claude"), {})) == "ALLOW"


def test_blast_radius_gate_pushes_false_allows_recoverable_not_catastrophic() -> None:
    """
    ``gate_pushes=False`` drops only the ASK tier; the catastrophic DENY set
    still applies. Fails if the flag stopped controlling the ASK tier (would
    make the flag a no-op) or if disabling it also disabled the DENY set
    (would let force-push / ``rm -rf /`` through unattended).
    """
    evaluate = blast_radius(gate_pushes=False)
    assert (
        _result(evaluate(_tool_call("sys_os_shell", command="git push origin main"), {}))
        == "ALLOW"
    )
    assert _result(evaluate(_tool_call("sys_os_shell", command="rm -rf /"), {})) == "DENY"


# Catastrophic commands that the previous single-regex DENY set MISSED — each
# fell through to ASK, then ALLOW under the checked-in ``gate_pushes=False``,
# so a worker could run them ungated. They must now DENY at both gate settings
# (the DENY tier runs regardless of ``gate_pushes``).
_DENY_GAP_COMMANDS = [
    "rm -rf /etc",  # root child (regex matched only bare `/`)
    "rm -rf /usr/local",  # path under a system dir
    "rm -rf /*",  # glob of root
    "rm -r -f /",  # split recursive/force flags
    "rm --recursive --force /",  # long flags (bypassed even the ASK regex)
    "sudo rm -rf /var",  # leading sudo
    "sudo -n rm -rf /var",  # sudo option before the destructive command
    "sudo -- rm -rf /var",  # sudo option terminator before the command
    "sudo -u root rm -rf /var",  # sudo option with a separate value
    "CI=1 rm -rf /etc",  # shell env assignment before the command
    "CI=1 sudo -n rm -rf /var",  # env assignment before sudo wrapper
    "rm -rf ~",  # whole home dir
    "rm -rf $HOME",
    "git push origin +main",  # force-push via +refspec
    "git push origin --delete main",  # remote-branch deletion
    "git push -d origin main",  # remote-branch deletion via short option
    "git push -uf origin main",  # force-push via bundled short option
    "git push -df origin main",  # delete/force via bundled short option
    "git push --mirror origin",  # mirrors force updates and deletions
    "git push --prune origin",  # deletes remote refs missing locally
    "git push origin :main",  # remote-branch deletion via :refspec
    "git -C repo push -d origin main",  # deletion after a git global option
    "sudo -n git push -d origin main",  # deletion through sudo wrapper
    "CI=1 git push -d origin main",  # env assignment before git push
    "cd repo && rm -rf /etc",  # destructive statement after a separator
]


@pytest.mark.parametrize("command", _DENY_GAP_COMMANDS)
def test_blast_radius_denies_destructive_variants(command: str) -> None:
    """
    blast_radius DENIES catastrophic ``rm`` / ``git push`` in every flag,
    path, and refspec form — at both gate settings.

    Regression guard for the DENY-pattern gaps: each command here previously
    fell through to ASK (then ALLOW under ``gate_pushes=False``). A non-DENY
    result means the flag/refspec-robust classifier regressed and a worker
    could destroy ``/etc``, force-push, or delete a remote branch ungated.
    """
    assert _result(blast_radius()(_tool_call("Bash", command=command), {})) == "DENY"
    # The catastrophic DENY tier runs regardless of gate_pushes (the nessie
    # specs ship gate_pushes=False), so these must still DENY there.
    assert (
        _result(blast_radius(gate_pushes=False)(_tool_call("Bash", command=command), {})) == "DENY"
    )


@pytest.mark.parametrize(
    "command",
    [
        "rm -r node_modules",  # recursive, no force, relative
        "rm -rf /home/u/proj/build",  # scoped path under /home, not a system dir
        "git push origin main",  # ordinary outward push (also asserts the gate=False ALLOW)
        "git push -u origin main",  # set-upstream is outward, not force/delete
        "git push -o ci.skip origin main",  # push-option value is not a destructive flag
        "git push -o=fast origin main",  # attached push-option value must not over-match `f`
        "CI=1 rm -rf build",  # env assignment does not make scoped cleanup catastrophic
        "CI=1 git push origin main",  # env assignment preserves ordinary-push ASK tier
    ],
)
def test_blast_radius_recoverable_variants_ask_not_deny(command: str) -> None:
    """
    Recoverable destructive commands ASK (not DENY) and ALLOW under
    ``gate_pushes=False`` — so the hardened classifier did not over-block.

    A DENY here means the catastrophic-target test is too broad (it would
    block routine worker cleanup like ``rm -rf build`` or its own pushes); an
    ALLOW at gate=True means the recursive-rm / outward-push ASK tier
    regressed.
    """
    assert _result(blast_radius()(_tool_call("Bash", command=command), {})) == "ASK"
    assert (
        _result(blast_radius(gate_pushes=False)(_tool_call("Bash", command=command), {}))
        == "ALLOW"
    )


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "push to main soon"',  # "push" only in the commit message
        "git push-notes --ref x",  # not the `push` subcommand
        "rm file.txt",  # non-recursive single-file delete
        "rm -f stale.log",  # force without recursion
        "rm -- -rf",  # `--` ends flags: deletes a file literally named "-rf" (not recursive)
        "git status",
    ],
)
def test_blast_radius_safe_commands_allow(command: str) -> None:
    """
    Safe commands ALLOW — the tokenizer must not over-match.

    Guards the over-match traps the robust classifier could fall into: a
    ``push`` substring inside a commit message, a ``git`` subcommand that
    merely starts with ``push``, a non-recursive ``rm``, and a post-``--``
    dash-prefixed filename mis-read as a recursive flag. A non-ALLOW here
    means the classifier is matching on a substring rather than the parsed
    subcommand / flags.
    """
    assert _result(blast_radius()(_tool_call("Bash", command=command), {})) == "ALLOW"


def test_spawn_bounds_caps_then_resets_per_turn() -> None:
    """
    spawn_bounds allows up to N dispatches per turn, DENIES the (N+1)th, and
    reset_turn clears the count.

    If the DENY never fires, the per-turn fan-out cap regressed (unbounded
    spawning). If the post-reset call is not ALLOW, reset_turn stopped
    clearing state and the cap would silently degrade to per-session.
    """
    evaluate = spawn_bounds(max_dispatches_per_turn=2)
    send = _tool_call("sys_session_send", agent="impl_claude")
    assert _result(evaluate(send)) == "ALLOW"  # dispatch 1 (within cap)
    assert _result(evaluate(send)) == "ALLOW"  # dispatch 2 (at cap)
    assert _result(evaluate(send)) == "DENY"  # dispatch 3 exceeds cap=2
    evaluate.reset_turn()
    assert _result(evaluate(send)) == "ALLOW"  # counter cleared at turn boundary


def test_spawn_bounds_only_counts_dispatches() -> None:
    """
    Only ``sys_session_send`` calls consume the per-turn budget; other tool
    calls do not. A failure means the counter is incrementing on the wrong
    events and would starve the orchestrator's own shell/registry work.
    """
    evaluate = spawn_bounds(max_dispatches_per_turn=1)
    assert _result(evaluate(_tool_call("sys_os_shell", command="ls"))) == "ALLOW"  # not a dispatch
    assert _result(evaluate(_tool_call("sys_session_send", agent="x"))) == "ALLOW"  # dispatch 1
    assert _result(evaluate(_tool_call("sys_session_send", agent="x"))) == "DENY"  # exceeds cap=1


def test_spawn_bounds_only_counts_configured_dispatch_tools() -> None:
    """
    ``dispatch_tools`` controls which tool calls ``spawn_bounds`` counts.

    This fails if the configurable tool set is ignored: a tool outside the set
    would consume the per-turn budget (over-counting) or a tool inside it would
    be skipped (unbounded fan-out).
    """
    evaluate = spawn_bounds(
        max_dispatches_per_turn=2,
        dispatch_tools=("sys_session_send",),
    )

    assert _result(evaluate(_tool_call("sys_os_shell", command="ls"))) == "ALLOW"  # uncounted
    assert _result(evaluate(_tool_call("sys_session_send", agent="claude_code"))) == "ALLOW"
    assert _result(evaluate(_tool_call("sys_session_send", agent="codex"))) == "ALLOW"
    assert _result(evaluate(_tool_call("sys_session_send", agent="claude_code"))) == "DENY"


@pytest.mark.parametrize(
    "child_args,expected",
    [
        ({"input": "Implement issue 1425.", "purpose": "implement"}, "ALLOW"),
        ({"input": "Review this diff.", "purpose": "review"}, "ALLOW"),
        ({"input": "Explore the relevant code.", "purpose": "explore"}, "ALLOW"),
        ({"input": "Search for the owner.", "purpose": "search"}, "ALLOW"),
        # `small_scoped` was the loophole that let implementation masquerade as a
        # lightweight ask routed to a non-implementer; it is retired, so it must
        # now DENY. If this flips back to ALLOW the routing hole is reopened.
        ({"input": "Make this tiny scoped edit.", "purpose": "small_scoped"}, "DENY"),
        ({"input": "Work on issue 1425."}, "DENY"),
        ({"input": "Implement issue 1425.", "purpose": "bogus"}, "DENY"),
    ],
)
def test_headless_subagent_purpose_guard_requires_explicit_purpose(
    child_args: dict[str, Any],
    expected: str,
) -> None:
    """
    headless_subagent_purpose_guard requires every dispatch to declare a purpose
    in the allowed set (implement / review / explore / search).

    A missing, retired (``small_scoped``), or out-of-set purpose must DENY,
    otherwise the model can spawn a ``sys_session_send`` sub-agent with no
    declared role — or relabel implementation as a lightweight ask.
    """
    evaluate = headless_subagent_purpose_guard()

    decision = evaluate(_tool_call("sys_session_send", agent="claude_code", args=child_args))
    assert _result(decision) == expected


def test_headless_subagent_purpose_guard_honors_custom_allowed_purposes() -> None:
    """
    The ``allowed_purposes`` factory param controls the accepted set.

    With a restricted set of only ``implement``, an ``implement`` dispatch
    ALLOWs and a ``review`` dispatch DENYs — proving the param is honored. If it
    were ignored (a hardcoded set used instead), the ``review`` case would
    wrongly ALLOW.
    """
    evaluate = headless_subagent_purpose_guard(allowed_purposes=("implement",))

    allowed = evaluate(
        _tool_call(
            "sys_session_send",
            agent="codex",
            args={"input": "Implement issue 1425.", "purpose": "implement"},
        )
    )
    denied = evaluate(
        _tool_call(
            "sys_session_send",
            agent="codex",
            args={"input": "Review this diff.", "purpose": "review"},
        )
    )
    assert _result(allowed) == "ALLOW"
    assert _result(denied) == "DENY"


def test_headless_subagent_purpose_guard_ignores_non_session_tools() -> None:
    """
    Non-``sys_session_send`` tool calls pass through the purpose guard.

    A failure means the policy is matching the wrong tool and would block
    unrelated tool dispatch (e.g. the orchestrator's own shell/registry work).
    """
    evaluate = headless_subagent_purpose_guard()

    assert _result(evaluate(_tool_call("sys_os_shell", command="git status"))) == "ALLOW"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/app.py", "ALLOW"),
        ("web/src/store/chatStore.ts", "ALLOW"),
        ("/etc/passwd", "DENY"),
        ("~/.bashrc", "DENY"),
        ("../outside.py", "DENY"),
        ("a/../../escape.py", "DENY"),
    ],
)
def test_worktree_guard_blocks_escapes(path: str, expected: str) -> None:
    """
    worktree_guard ALLOWS relative in-tree write paths and DENIES absolute or
    ``..``-escaping ones.

    A DENY-case failure means an unsandboxed worker could write outside its
    worktree (the confinement that makes workers safe is gone). An ALLOW-case
    failure means ordinary in-worktree edits are blocked and workers can't do
    their job.
    """
    evaluate = worktree_guard()
    assert _result(evaluate(_tool_call("sys_os_write", path=path, content=""), {})) == expected


@pytest.mark.parametrize(
    "tool,path_key,path,expected",
    [
        # Claude native Write uses ``file_path``, not ``path``.
        ("Write", "file_path", "src/app.py", "ALLOW"),
        ("Write", "file_path", "/etc/passwd", "DENY"),
        ("Write", "file_path", "../escape.py", "DENY"),
        # Claude native Edit also uses ``file_path``.
        ("Edit", "file_path", "main.py", "ALLOW"),
        ("Edit", "file_path", "~/.bashrc", "DENY"),
        # Claude native MultiEdit also uses ``file_path``. Without it in the
        # gated set, an unsandboxed worker could escape its worktree via a
        # multi-file edit -- the gap these cases pin.
        ("MultiEdit", "file_path", "src/app.py", "ALLOW"),
        ("MultiEdit", "file_path", "/etc/passwd", "DENY"),
        ("MultiEdit", "file_path", "../escape.py", "DENY"),
        # Pi native write/edit (lowercase) use ``path`` (Omnigent convention).
        ("write", "path", "src/app.py", "ALLOW"),
        ("write", "path", "/etc/passwd", "DENY"),
        ("edit", "path", "../escape.py", "DENY"),
    ],
    ids=[
        "Write-in-tree",
        "Write-absolute",
        "Write-escape",
        "Edit-in-tree",
        "Edit-home-escape",
        "MultiEdit-in-tree",
        "MultiEdit-absolute",
        "MultiEdit-escape",
        "pi-write-in-tree",
        "pi-write-absolute",
        "pi-edit-escape",
    ],
)
def test_worktree_guard_gates_native_write_edit(
    tool: str,
    path_key: str,
    path: str,
    expected: str,
) -> None:
    """
    worktree_guard must also gate Claude/Codex native ``Write`` and ``Edit``
    tools (surfaced via the ``PreToolUse`` hook).

    If this returns ALLOW for an escape path, an unsandboxed native-harness
    worker could write outside its worktree — the confinement that makes
    workers safe would be bypassed.

    :param tool: Native tool name, e.g. ``"Write"``.
    :param path_key: The argument key carrying the file path (``"file_path"``
        for Claude native, ``"path"`` for Omnigent built-in).
    :param path: The file path value to test.
    :param expected: ``"ALLOW"`` or ``"DENY"``.
    """
    evaluate = worktree_guard()
    assert _result(evaluate(_tool_call(tool, **{path_key: path, "content": ""}), {})) == expected


def test_worktree_guard_only_guards_writes() -> None:
    """
    Reads and shells pass through — the guard constrains only write/edit
    tools. Fails if the guard broadened to reads (workers couldn't read
    files outside the worktree, breaking exploration).
    """
    evaluate = worktree_guard()
    assert _result(evaluate(_tool_call("sys_os_read", path="/etc/hosts"), {})) == "ALLOW"


@pytest.mark.parametrize(
    "tool,args",
    [
        # Omnigent built-in write/edit.
        ("sys_os_write", {"path": "a.py", "content": "x"}),
        ("sys_os_edit", {"path": "a.py", "old": "x", "new": "y"}),
        # Claude/Codex native aliases.
        ("Write", {"file_path": "a.py", "content": "x"}),
        ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}),
        ("MultiEdit", {"file_path": "a.py", "edits": []}),
        # Pi native lowercase.
        ("write", {"path": "a.py", "content": "x"}),
        ("edit", {"path": "a.py"}),
    ],
)
def test_read_only_os_denies_every_write_tool(tool: str, args: dict[str, Any]) -> None:
    """
    read_only_os DENIES every file-mutating tool regardless of path.

    A report-only agent (e.g. Sentinel and its sub-agents) carries the bundled
    sys_os_write / sys_os_edit tools but must never use them; if this returns
    anything but DENY, an "auto-fix" slips past the policy layer.
    """
    assert _result(read_only_os()(_tool_call(tool, **args), {})) == "DENY"


def test_read_only_os_allows_reads_and_shell() -> None:
    """
    Reads, searches, and shell pass through — read_only_os gates only writes.
    Fails if it broadened to reads (a reviewer couldn't open files to
    fact-check) or to shell (pair blast_radius for that, not this policy).
    """
    evaluate = read_only_os()
    assert _result(evaluate(_tool_call("sys_os_read", path="a.py"), {})) == "ALLOW"
    assert _result(evaluate(_tool_call("sys_os_shell", command="rg secret"), {})) == "ALLOW"
    assert _result(evaluate(_tool_call("Read", file_path="a.py"), {})) == "ALLOW"
