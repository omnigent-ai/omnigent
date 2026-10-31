"""Host execution of a project's ``env_commands`` and ``setup_commands``."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.host import project_setup_runner
from omnigent.host.project_setup_runner import (
    SETUP_MARKER_NAME,
    EnvDelta,
    EnvProbeCache,
    apply_env_delta,
    claim_worktree_setup,
    clear_setup_marker,
    compute_env_delta,
    kill_running_project_commands,
    prepare_project_env,
    probe_env_delta,
    run_worktree_setup,
)
from omnigent.project_setup import ProjectSetup

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def bash_login_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make bash the user's shell with an empty home, so rc files are predictable."""
    monkeypatch.setenv("SHELL", shutil.which("bash") or "/bin/bash")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


@pytest.fixture
def linked_worktree(tmp_path: Path) -> Path:
    """A real repo with one linked worktree; returns the worktree path."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git(
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    worktree = tmp_path / "wt"
    git("worktree", "add", "-q", "-b", "feature", str(worktree))
    return worktree


def test_env_delta_keeps_project_changes_and_drops_noise_and_wiring() -> None:
    before = {"PATH": "/usr/bin", "PWD": "/a", "VIRTUAL_ENV": "/old", "HOME": "/h"}
    after = {
        "PATH": "/conda/bin:/usr/bin",
        "PWD": "/b",
        "CONDA_DEFAULT_ENV": "proj",
        "RUNNER_ID": "spoofed",
        "OMNIGENT_SERVER_URL": "spoofed",
        "HOME": "/h",
    }

    delta = compute_env_delta(before, after)

    assert delta.set_vars == {"PATH": "/conda/bin:/usr/bin", "CONDA_DEFAULT_ENV": "proj"}
    assert delta.unset_vars == frozenset({"VIRTUAL_ENV"})


def test_apply_env_delta_never_overrides_runner_wiring() -> None:
    env = {"RUNNER_ID": "real", "VIRTUAL_ENV": "/old", "PATH": "/usr/bin"}
    delta = EnvDelta(
        set_vars={"PATH": "/conda/bin", "RUNNER_ID": "spoofed"},
        unset_vars=frozenset({"VIRTUAL_ENV", "RUNNER_ID"}),
    )

    apply_env_delta(env, delta)

    assert env == {"RUNNER_ID": "real", "PATH": "/conda/bin"}


def test_probe_captures_exports_from_the_users_shell(
    bash_login_shell: None, tmp_path: Path
) -> None:
    delta, warning = probe_env_delta(
        ("export PROJECT_TOOL=ready", "export PATH=/project/bin:$PATH; unset PROBE_GONE"),
        workspace=str(tmp_path),
    )

    assert warning is None
    assert delta is not None
    assert delta.set_vars["PROJECT_TOOL"] == "ready"
    assert delta.set_vars["PATH"].startswith("/project/bin:")


def test_probe_failure_is_a_warning_with_the_output(
    bash_login_shell: None, tmp_path: Path
) -> None:
    delta, warning = probe_env_delta(
        ("echo 'no such env' >&2; false", "export NEVER_RUNS=1"), workspace=str(tmp_path)
    )

    assert delta is None
    assert warning is not None
    assert "exit 1" in warning
    assert "no such env" in warning


def test_probe_that_hangs_is_killed_within_its_timeout(
    bash_login_shell: None, tmp_path: Path
) -> None:
    delta, warning = probe_env_delta(("sleep 30",), workspace=str(tmp_path), timeout=0.5)

    assert delta is None
    assert warning is not None and "timed out" in warning


def test_non_posix_login_shell_skips_env_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SHELL", "/usr/bin/fish")

    delta, warning = probe_env_delta(("set -x A 1",), workspace=str(tmp_path))

    assert delta is None
    assert warning is not None and "bash or zsh" in warning


def test_successful_probe_is_reused_until_the_commands_change(
    bash_login_shell: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    real_probe = project_setup_runner.probe_env_delta

    def counting_probe(commands: tuple[str, ...], **kwargs: object) -> object:
        calls.append(commands)
        return real_probe(commands, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(project_setup_runner, "probe_env_delta", counting_probe)
    cache = EnvProbeCache()
    first = ProjectSetup(env_commands=("export A=1",))

    prepare_project_env(first, workspace=str(tmp_path), cache=cache)
    outcome = prepare_project_env(first, workspace=str(tmp_path), cache=cache)
    prepare_project_env(
        ProjectSetup(env_commands=("export A=2",)), workspace=str(tmp_path), cache=cache
    )

    assert outcome.env_delta.set_vars["A"] == "1"
    assert calls == [("export A=1",), ("export A=2",)]


def test_setup_runs_once_per_worktree_and_again_after_an_edit(
    bash_login_shell: None, linked_worktree: Path
) -> None:
    commands = ("echo run >> setup.log", 'test -d "$OMNIGENT_ROOT_PATH/.git"')

    git_dir = claim_worktree_setup(commands, workspace=str(linked_worktree))
    assert git_dir is not None
    result = run_worktree_setup(
        commands, git_dir=git_dir, workspace=str(linked_worktree), session_id="conv_1", env={}
    )
    assert result.warning is None

    assert claim_worktree_setup(commands, workspace=str(linked_worktree)) is None
    edited = (*commands, "echo edited >> setup.log")
    git_dir = claim_worktree_setup(edited, workspace=str(linked_worktree))
    assert git_dir is not None
    run_worktree_setup(
        edited, git_dir=git_dir, workspace=str(linked_worktree), session_id="conv_2", env={}
    )

    assert (linked_worktree / "setup.log").read_text().splitlines() == ["run", "run", "edited"]
    marker = json.loads((git_dir / SETUP_MARKER_NAME).read_text())
    assert marker["status"] == "succeeded"
    # The marker lives in the worktree's private git dir, not the checkout.
    assert not (linked_worktree / SETUP_MARKER_NAME).exists()


def test_failed_setup_stops_records_the_failure_and_can_be_forced_again(
    bash_login_shell: None, linked_worktree: Path
) -> None:
    commands = ("echo broken install >&2; exit 3", "touch never-ran")

    git_dir = claim_worktree_setup(commands, workspace=str(linked_worktree))
    assert git_dir is not None
    result = run_worktree_setup(
        commands, git_dir=git_dir, workspace=str(linked_worktree), session_id=None, env={}
    )

    assert result.warning is not None
    assert "exit 3" in result.warning and "broken install" in result.warning
    assert not (linked_worktree / "never-ran").exists()
    # A failed run is recorded, so resuming the session does not retry it...
    assert claim_worktree_setup(commands, workspace=str(linked_worktree)) is None
    # ...until the user asks for a re-run.
    assert clear_setup_marker(str(linked_worktree)) is True
    assert claim_worktree_setup(commands, workspace=str(linked_worktree)) is not None


def test_concurrent_launches_in_one_worktree_run_setup_once(linked_worktree: Path) -> None:
    commands = ("true",)

    first = claim_worktree_setup(commands, workspace=str(linked_worktree))
    second = claim_worktree_setup(commands, workspace=str(linked_worktree))

    assert first is not None
    assert second is None
    run_worktree_setup(
        commands, git_dir=first, workspace=str(linked_worktree), session_id=None, env={}
    )


def test_setup_is_skipped_outside_a_linked_worktree(linked_worktree: Path, tmp_path: Path) -> None:
    main_checkout = tmp_path / "repo"

    assert claim_worktree_setup(("true",), workspace=str(main_checkout)) is None
    assert claim_worktree_setup(("true",), workspace=str(tmp_path)) is None


def test_setup_interrupted_by_host_shutdown_runs_again_next_launch(
    bash_login_shell: None, linked_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_setup_runner, "_shutting_down", threading.Event())
    commands = ("sleep 30",)
    git_dir = claim_worktree_setup(commands, workspace=str(linked_worktree))
    assert git_dir is not None
    killer = threading.Timer(0.5, kill_running_project_commands)
    killer.start()

    result = run_worktree_setup(
        commands, git_dir=git_dir, workspace=str(linked_worktree), session_id=None, env={}
    )

    assert result.warning == "interrupted by host shutdown"
    assert claim_worktree_setup(commands, workspace=str(linked_worktree)) is not None


def test_login_shell_comes_from_the_account_when_shell_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(
        "pwd.getpwuid", lambda _uid: SimpleNamespace(pw_shell=shutil.which("bash"))
    )

    delta, warning = probe_env_delta(("export FROM_ACCOUNT_SHELL=1",), workspace=str(tmp_path))

    assert warning is None
    assert delta is not None and delta.set_vars["FROM_ACCOUNT_SHELL"] == "1"
