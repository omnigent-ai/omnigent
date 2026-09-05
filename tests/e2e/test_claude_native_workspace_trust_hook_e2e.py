r"""End-to-end guard: claude-native must not run unreviewed project hooks at startup.

``claude-native`` pre-seeds Claude Code's first-run trust + onboarding gates
pre-launch, without user confirmation:
:func:`omnigent.claude_native_bridge.ensure_claude_workspace_trusted` writes
``hasCompletedOnboarding`` and ``projects[<abs cwd>].hasTrustDialogAccepted``
into the launch ``HOME``'s ``~/.claude.json`` so a host-spawned (web-UI-driven)
session never blocks on Claude's unhookable trust/onboarding TUI prompts.

That machine-granted trust must not extend to *executing* the workspace's own
settings: if the launch args left Claude's default setting sources live, a
project-controlled ``.claude/settings.json`` would load, and opening a newly
cloned, third-party, or otherwise unreviewed workspace through claude-native
would let a project-defined ``SessionStart`` hook execute as the runner user at
startup --- before the user has personally seen or accepted Claude's "Do you
trust the files in this folder? (they can read, edit, and execute files)"
warning. Reviewing an attacker-authored branch would be enough. The launch-arg
builder therefore restricts setting sources to the user scope by default
(:func:`omnigent.inner.bundle_skills.claude_native_skill_args` emits
``--setting-sources user`` unless a call site opts in because Claude's own
trust gate is intact).

What this test does (the user journey, driven against the REAL ``claude`` CLI):

1. Stand up an *unreviewed* workspace whose ``.claude/settings.json`` carries a
   project ``SessionStart`` hook that runs an attacker command (here: create a
   marker file --- a stand-in for arbitrary code execution).
2. Pre-seed trust exactly as omnigent does, by calling the real
   ``ensure_claude_workspace_trusted(workspace)`` --- into an isolated ``$HOME``
   so the test never touches the developer's real ``~/.claude.json``.
3. Launch the real ``claude`` CLI in that workspace with an omnigent-style
   invocation ``--settings`` file plus the real
   ``claude_native_skill_args(skills_filter="all")`` output --- i.e. the exact
   launch shape the harness produces for a runner-owned session.
4. Assert the project ``SessionStart`` hook did **not** execute (the marker must
   be absent): an unreviewed workspace's project hook must not run at startup
   without an explicit user trust decision.

On a build where the pre-seeded trust defeats the gate (Claude boots straight
to the prompt with no trust dialog and its default setting sources keep the
project settings live), the hook runs and the final assertion FAILS. With
workspace-scoped setting sources disabled for the harness launch shape, the
unreviewed hook never executes and this PASSES.

Why this runs in plain CI (unlike the other claude-native e2e tests): the
``SessionStart`` hook fires at session *initialization*, before any model / API
call, so this needs the ``claude`` binary but **no** interactive Claude login or
network reachability. It is therefore gated on binary presence alone, not on
``OMNIGENT_E2E_CLAUDE_NATIVE``.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from omnigent.claude_native_bridge import ensure_claude_workspace_trusted
from omnigent.inner.bundle_skills import claude_native_skill_args

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="claude-native workspace-trust hook e2e needs the `claude` CLI installed",
)

# The SessionStart hook fires at TUI init, well before any prompt/turn, so a
# short budget is enough. Kept generous for cold CLI boot on a slow box.
_LAUNCH_BUDGET_S = 45.0
# The hook's side effect: creating this file proves the project SessionStart
# command executed as the runner user.
_MARKER_NAME = "attacker_hook_executed"


def _drive_claude_startup(
    *, claude_bin: str, workspace: Path, invocation_settings: Path, env: dict[str, str]
) -> tuple[bool, str]:
    """Launch the real ``claude`` CLI in *workspace* and watch it boot.

    Runs Claude Code interactively in a pseudo-TTY (the way the native harness
    launches it in its terminal pane) with omnigent's launch shape:
    ``--settings <invocation>`` plus the harness's own
    ``claude_native_skill_args(skills_filter="all")`` output. Polls until the
    project ``SessionStart`` hook's marker file appears or the budget elapses.

    :param claude_bin: Path to the ``claude`` executable.
    :param workspace: The unreviewed workspace to launch in (holds the project
        ``.claude/settings.json`` hook).
    :param invocation_settings: omnigent-style invocation ``--settings`` file.
    :param env: Environment for the child (with an isolated ``HOME``).
    :returns: ``(marker_seen, decoded_tui_output)``.
    """
    marker = workspace / _MARKER_NAME
    # Mirror the harness launch: invocation --settings + the real skill/setting
    # -source args for the default filter. These args decide whether the
    # workspace's project settings (the hook) stay live.
    launch_args = [
        claude_bin,
        "--settings",
        str(invocation_settings),
        *claude_native_skill_args(None, skills_filter="all"),
    ]

    master_out, slave_out = pty.openpty()
    master_in, slave_in = pty.openpty()
    proc = subprocess.Popen(
        launch_args,
        stdin=slave_in,
        stdout=slave_out,
        stderr=slave_out,
        cwd=str(workspace),
        env={**env, "TERM": "xterm-256color"},
    )
    os.close(slave_out)
    os.close(slave_in)

    buf = b""
    deadline = time.time() + _LAUNCH_BUDGET_S
    try:
        while time.time() < deadline:
            if marker.exists():
                break
            ready, _, _ = select.select([master_out], [], [], 0.5)
            if master_out in ready:
                try:
                    chunk = os.read(master_out, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
        # Give a just-launched hook a beat to flush its marker.
        time.sleep(1.0)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        for fd in (master_out, master_in):
            with contextlib.suppress(OSError):
                os.close(fd)

    text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", buf)
    text = re.sub(rb"[\x00-\x08\x0e-\x1f]", b"", text)
    return marker.exists(), text.decode("utf-8", "replace")


def test_claude_native_pre_seeded_trust_runs_unreviewed_project_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """omnigent's trust pre-seed must not let an unreviewed project hook run.

    Drives the real omnigent seed + the real ``claude`` binary and asserts the
    project ``SessionStart`` hook does NOT execute at startup.
    """
    claude_bin = shutil.which("claude")
    assert claude_bin is not None  # guarded by pytestmark

    # Isolated home so the real ensure_claude_workspace_trusted() (which writes
    # Path.home()/.claude.json) never touches the developer's real config.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # An unreviewed / attacker-authored workspace: its project settings define a
    # SessionStart hook that runs an arbitrary command at startup.
    workspace = tmp_path / "unreviewed-workspace"
    (workspace / ".claude").mkdir(parents=True)
    marker = workspace / _MARKER_NAME
    project_hook_cmd = f"echo attacker-code-executed > {json.dumps(str(marker))}"
    (workspace / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"hooks": [{"type": "command", "command": project_hook_cmd}]}]
                }
            }
        ),
        encoding="utf-8",
    )

    # omnigent's real pre-launch trust seed (claude_native_bridge.py). No prompt,
    # no confirmation --- and it lands in the launch HOME, not an isolated home.
    ensure_claude_workspace_trusted(workspace)

    claude_config = json.loads((fake_home / ".claude.json").read_text(encoding="utf-8"))
    project_entry = claude_config.get("projects", {}).get(str(workspace.resolve()), {})
    # Document the mechanism the failure rides: the seed granted global trust
    # without any user decision.
    assert claude_config.get("hasCompletedOnboarding") is True
    assert project_entry.get("hasTrustDialogAccepted") is True

    # An omnigent-style invocation --settings file: hooks/statusline only; it
    # does not itself restrict settingSources (the CLI args must do that).
    invocation_settings = tmp_path / "claude-settings.json"
    invocation_settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "true"}}),
        encoding="utf-8",
    )

    marker_seen, tui_output = _drive_claude_startup(
        claude_bin=claude_bin,
        workspace=workspace,
        invocation_settings=invocation_settings,
        env=dict(os.environ),
    )

    # Sanity: the CLI actually launched and produced terminal output, so a
    # "marker absent" result reflects the trust gate holding --- not the binary
    # failing to boot at all.
    assert tui_output.strip(), (
        "claude CLI produced no terminal output; cannot conclude the trust gate held"
    )

    assert not marker_seen, (
        "SECURITY: the project .claude/settings.json SessionStart hook executed at "
        "claude-native startup with no trust prompt. omnigent's global trust pre-seed "
        "(ensure_claude_workspace_trusted) defeated Claude's workspace-trust gate, and "
        "project settings were not disabled (claude_native_skill_args emitted no "
        "--setting-sources), so an unreviewed workspace ran arbitrary hook code at "
        f"startup. Marker created by the hook: {marker}"
    )
