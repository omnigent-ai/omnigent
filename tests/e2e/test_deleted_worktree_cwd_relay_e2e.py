"""E2E regression test: ``Path.cwd()`` on a deleted working directory must
not crash comment-relay pre-start or turn setup.

Reported journey (a native-harness — e.g. ``claude-native`` — session whose
runner is rooted in a git worktree that is deleted underneath it)::

    1. launch a native-harness session; its runner's process cwd is a git
       worktree W (``omnigent run`` from a transient worktree, or a host runner
       rooted in one),
    2. run a turn (works),
    3. delete W underneath the running runner (``git worktree remove --force``),
    4. observable failure: the very next relay pre-start or turn dies with
       ``FileNotFoundError: [Errno 2] No such file or directory`` and the runner
       logs ``Failed to pre-start comment relay for <id>`` / ``turn setup failed
       for <id>: [Errno 2] ...``.

Root cause (one cause, two reported signatures): building the native-relay tool
list constructs a :class:`~omnigent.tools.manager.ToolManager` **without a
workdir**, so :class:`~omnigent.tools.builtins.load_skill.LoadSkillTool` is
instantiated with ``agent_root=None`` and falls back to ``Path.cwd()`` for
host-scope skill discovery (``omnigent/tools/builtins/load_skill.py`` — the
``discovery_root = agent_root or Path.cwd()`` line). When the process cwd has
been removed, ``os.getcwd()`` raises ``FileNotFoundError`` and the exception
escapes.

The report names both crash sites as reached through the same function:

* ``omnigent/runner/app.py`` ``_start_claude_relay_early``  → relay pre-start
  → ``Failed to pre-start comment relay for %s`` (9 attempts), and
* ``omnigent/runner/app.py`` ``_run_turn_bg``               → turn setup
  → ``turn setup failed for %s: %s`` (5 attempts, hit released users),

both via ``omnigent/runner/tool_dispatch.py``
``build_native_relay_tool_schemas`` — the single reach point exercised here.
The native TUI (claude-native/codex-native) needs an interactive login and
cannot run in CI, so this drives the exact shared reach function against a real
deleted git worktree instead of the TUI, reproducing the identical crash at the
identical call site.

``test_native_relay_tool_schemas_build_with_live_worktree_cwd`` is the baseline
(passes on any build) proving the schema build succeeds — and registers
``load_skill`` — when the worktree cwd exists. ``test_native_relay_tool_schemas_
survive_deleted_worktree_cwd`` is the regression guard: it FAILS on the buggy
build (``build_native_relay_tool_schemas`` raises ``FileNotFoundError`` from a
``Path.cwd()`` read — ``load_skill.py``'s discovery fallback, then the OS-tool
schema-extraction spec in ``tool_dispatch.py``) and passes once no step of the
schema build requires the process cwd to still exist.

Linux-only: a removed cwd makes ``os.getcwd()`` raise on Linux (the reported
traceback is ``/usr/lib/python3.12/pathlib.py`` → ``os.getcwd()``); other
platforms may keep serving the stale path.

Runs against no server and needs no credentials::

    pytest tests/e2e/test_deleted_worktree_cwd_relay_e2e.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason=(
        "a removed cwd makes os.getcwd() raise on Linux (the reported traceback); "
        "other platforms may keep serving the stale path"
    ),
)


def _make_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """Create a git repo with one worktree W (the runner's launch cwd).

    :param tmp_path: Per-test temp dir.
    :returns: ``(repo, W)`` — the repo dir and the worktree path.
    """
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=e2e@test",
            "-c",
            "user.name=e2e",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    w = tmp_path / "worktrees" / "W"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "-q",
            str(w),
            "-b",
            f"w-{uuid.uuid4().hex[:8]}",
        ],
        check=True,
    )
    return repo, w


def _native_spec() -> AgentSpec:
    """A resolved ``claude-native`` agent spec.

    The exact shape the runner hands to ``build_native_relay_tool_schemas`` from
    both ``_start_claude_relay_early`` (relay pre-start) and ``_run_turn_bg``
    (turn setup) via ``_ensure_comment_relay_started``.

    :returns: A minimal non-``None`` native-harness :class:`AgentSpec`.
    """
    return AgentSpec(
        spec_version=1,
        name="deleted-cwd-native",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )


def test_native_relay_tool_schemas_build_with_live_worktree_cwd(tmp_path: Path) -> None:
    """Baseline: with the worktree cwd present, the native relay schema build
    succeeds and registers ``load_skill``.

    Isolates the deleted cwd as the trigger: the identical call in the identical
    process succeeds here, so a failure in the sibling test is caused only by the
    removed working directory, not by the spec or the build itself.
    """
    _repo, w = _make_worktree(tmp_path)
    original_cwd = os.getcwd()
    spec = _native_spec()
    try:
        os.chdir(w)
        schemas = build_native_relay_tool_schemas(spec)
    finally:
        os.chdir(original_cwd)

    names = {s["name"] for s in schemas}
    assert schemas, "expected a non-empty native relay tool surface"
    assert "load_skill" in names, (
        "load_skill must ride the native relay (it is what discovers host-scope "
        f"skills); got {sorted(names)}"
    )


def test_native_relay_tool_schemas_survive_deleted_worktree_cwd(tmp_path: Path) -> None:
    """Regression guard: building the native relay tool schemas must not
    crash when the runner's process cwd (a git worktree) has been deleted
    underneath it.

    On the unfixed build ``build_native_relay_tool_schemas`` constructs a
    ``ToolManager`` with no workdir, so ``LoadSkillTool`` falls back to
    ``Path.cwd()`` → ``os.getcwd()`` → ``FileNotFoundError: [Errno 2]``. That is
    exactly the relay pre-start crash (``Failed to pre-start comment relay``) and
    the turn-setup crash (``turn setup failed: [Errno 2] ...``) the report
    describes. This test therefore FAILS on the buggy build (the call raises)
    and passes once no step of the schema build requires the process cwd to
    still exist.
    """
    repo, w = _make_worktree(tmp_path)
    original_cwd = os.getcwd()
    spec = _native_spec()
    try:
        os.chdir(w)

        # Sanity: the schema build works while the worktree cwd is live.
        assert build_native_relay_tool_schemas(spec), "precondition: build works with live cwd"

        # The reported journey: the worktree is removed underneath the running
        # runner (`git worktree remove --force`), leaving the process cwd gone.
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(w)],
            check=False,
        )
        shutil.rmtree(w, ignore_errors=True)
        assert not w.exists()

        # THE observable: on the buggy build this raises FileNotFoundError from
        # load_skill.py's `Path.cwd()`; after the fix it returns the schemas.
        schemas = build_native_relay_tool_schemas(spec)
    finally:
        os.chdir(original_cwd)

    names = {s["name"] for s in schemas}
    assert schemas, (
        "build_native_relay_tool_schemas returned no schemas on a deleted cwd; "
        "the native relay surface must survive a removed working directory"
    )
    assert "load_skill" in names, (
        "load_skill must still be registered on the native relay when the process "
        f"cwd has been deleted (host-scope discovery yields nothing); got {sorted(names)}"
    )
