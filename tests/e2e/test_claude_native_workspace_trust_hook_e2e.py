r"""End-to-end guard: claude-native must not run unreviewed project hooks at startup.

For a host-spawned (web-UI-driven) session, ``claude-native`` pre-seeds Claude
Code's unhookable first-run trust + onboarding gates
(:func:`omnigent.harnesses.claude_native.bridge.ensure_claude_workspace_trusted`)
so the terminal never blocks on them. That machine-granted trust must not also
*execute* the workspace's own settings: if the launch left Claude's default
setting sources live, opening an unreviewed workspace would let its project
``.claude/settings.json`` ``SessionStart`` hook run as the runner user before
any human saw Claude's trust dialog. The launch-arg builder therefore restricts
setting sources to the user scope by default
(:func:`omnigent.inner.bundle_skills.claude_native_skill_args`).

Driven against the REAL ``claude`` CLI: with the pre-fix launch shape (workspace
settings live) the hook runs; with the harness's actual shape (``--setting-sources
user``) it must not. The first case is a positive control, so a later "marker
absent" result reflects the gate holding rather than the CLI failing to boot.

This runs on ``claude`` binary presence alone (not ``OMNIGENT_E2E_CLAUDE_NATIVE``):
the ``SessionStart`` hook fires at session init, before any model/API call, so
no interactive login or network reachability is needed.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import ensure_claude_workspace_trusted
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
    *,
    claude_bin: str,
    workspace: Path,
    invocation_settings: Path,
    setting_source_args: list[str],
    env: dict[str, str],
) -> tuple[bool, bool, str]:
    """Launch the real ``claude`` CLI in *workspace* and watch it boot.

    Runs Claude Code interactively in a pseudo-TTY (the way the native harness
    launches it in its terminal pane) with omnigent's launch shape:
    ``--settings <invocation>`` plus *setting_source_args* (the real
    ``claude_native_skill_args`` output). Polls until the project
    ``SessionStart`` hook's marker file appears or the budget elapses.

    :param claude_bin: Path to the ``claude`` executable.
    :param workspace: The unreviewed workspace to launch in (holds the project
        ``.claude/settings.json`` hook).
    :param invocation_settings: omnigent-style invocation ``--settings`` file.
    :param setting_source_args: The ``claude_native_skill_args`` output that
        decides whether the workspace's project settings (the hook) stay live.
    :param env: Environment for the child (with an isolated ``HOME``).
    :returns: ``(marker_seen, started_ok, decoded_tui_output)``, where
        ``started_ok`` is True when the CLI reached its interactive wait
        rather than exiting on its own (a crash would make a missing marker
        meaningless).
    """
    marker = workspace / _MARKER_NAME
    launch_args = [
        claude_bin,
        "--settings",
        str(invocation_settings),
        *setting_source_args,
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
        # Still running here means Claude booted into its interactive wait; an
        # already-exited process crashed before SessionStart, so marker absence
        # would not prove the gate held.
        started_ok = proc.poll() is None
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            proc.kill()
            proc.wait(timeout=5)
        for fd in (master_out, master_in):
            with contextlib.suppress(OSError):
                os.close(fd)

    text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", buf)
    text = re.sub(rb"[\x00-\x08\x0e-\x1f]", b"", text)
    return marker.exists(), started_ok, text.decode("utf-8", "replace")


def _seed_unreviewed_workspace(
    base: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, dict[str, str]]:
    """Set up one launch: an unreviewed workspace plus omnigent's trust seed.

    Builds a workspace whose project ``.claude/settings.json`` defines a
    ``SessionStart`` hook, pre-seeds trust via the real
    ``ensure_claude_workspace_trusted`` into an isolated ``HOME``, and returns
    the inputs for :func:`_drive_claude_startup`.

    :param base: A unique directory for this launch (its own ``HOME`` and
        workspace, so two launches in one test do not share trust state).
    :returns: ``(workspace, invocation_settings, env)``.
    """
    # Isolated home so the real ensure_claude_workspace_trusted() (which writes
    # Path.home()/.claude.json) never touches the developer's real config.
    fake_home = base / "home"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # ensure_claude_workspace_trusted() honors $CLAUDE_CONFIG_DIR over $HOME, so
    # drop any ambient value: the seed must land in fake_home, never the
    # developer's real external config.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    # An unreviewed / attacker-authored workspace: its project settings define a
    # SessionStart hook that runs an arbitrary command at startup.
    workspace = base / "unreviewed-workspace"
    (workspace / ".claude").mkdir(parents=True)
    marker = workspace / _MARKER_NAME
    project_hook_cmd = f"echo attacker-code-executed > {shlex.quote(str(marker))}"
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

    # omnigent's real pre-launch trust seed. No prompt, no confirmation --- and
    # it lands in the launch HOME, not an isolated home.
    ensure_claude_workspace_trusted(workspace)

    claude_config = json.loads((fake_home / ".claude.json").read_text(encoding="utf-8"))
    project_entry = claude_config.get("projects", {}).get(str(workspace.resolve()), {})
    # Document the mechanism the failure rides: the seed granted global trust
    # without any user decision.
    assert claude_config.get("hasCompletedOnboarding") is True
    assert project_entry.get("hasTrustDialogAccepted") is True

    # An omnigent-style invocation --settings file: hooks/statusline only; it
    # does not itself restrict settingSources (the CLI args must do that).
    invocation_settings = base / "claude-settings.json"
    invocation_settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "true"}}),
        encoding="utf-8",
    )
    return workspace, invocation_settings, dict(os.environ)


def test_claude_native_pre_seeded_trust_does_not_run_unreviewed_project_hook(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """omnigent's trust pre-seed must not let an unreviewed project hook run.

    Positive control first: with workspace-scoped settings live (the pre-fix
    launch shape), the pre-seeded trust defeats Claude's dialog and the project
    SessionStart hook DOES run --- proving the hook fires at startup and the
    seed really bypasses the gate in this environment, so a later "marker
    absent" result reflects the gate holding, not a boot failure. Then the
    harness's actual launch shape (user-scope setting sources) must NOT run it.
    """
    claude_bin = shutil.which("claude")
    assert claude_bin is not None  # guarded by pytestmark

    control_ws, control_settings, control_env = _seed_unreviewed_workspace(
        tmp_path / "control", monkeypatch
    )
    control_seen, _control_started, control_out = _drive_claude_startup(
        claude_bin=claude_bin,
        workspace=control_ws,
        invocation_settings=control_settings,
        setting_source_args=claude_native_skill_args(
            None, skills_filter="all", include_workspace_settings=True
        ),
        env=control_env,
    )
    assert control_seen, (
        "positive control failed: with workspace settings live and trust "
        "pre-seeded, the project SessionStart hook did not run, so claude never "
        f"reached SessionStart in this environment. TUI output:\n{control_out}"
    )

    fixed_ws, fixed_settings, fixed_env = _seed_unreviewed_workspace(
        tmp_path / "fixed", monkeypatch
    )
    fixed_seen, fixed_started, fixed_out = _drive_claude_startup(
        claude_bin=claude_bin,
        workspace=fixed_ws,
        invocation_settings=fixed_settings,
        setting_source_args=claude_native_skill_args(None, skills_filter="all"),
        env=fixed_env,
    )

    assert fixed_started, (
        "restricted launch (--setting-sources user) did not reach Claude's "
        "interactive wait: the CLI exited on its own, so a missing marker would "
        f"not prove the gate held rather than a boot failure. TUI output:\n{fixed_out}"
    )
    assert not fixed_seen, (
        "SECURITY: the project .claude/settings.json SessionStart hook executed at "
        "claude-native startup with no trust prompt. omnigent's global trust pre-seed "
        "(ensure_claude_workspace_trusted) defeated Claude's workspace-trust gate, and "
        "project settings were not disabled (claude_native_skill_args emitted no "
        "--setting-sources), so an unreviewed workspace ran arbitrary hook code at "
        f"startup. Marker created by the hook: {fixed_ws / _MARKER_NAME}"
    )
