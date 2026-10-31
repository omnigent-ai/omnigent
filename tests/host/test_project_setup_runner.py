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
    EnvDelta,
    EnvProbeCache,
    apply_env_delta,
    claim_worktree_setup,
    clear_setup_marker,
    compute_env_delta,
    kill_running_project_commands,
    pending_setup_marker,
    prepare_project_env,
    probe_env_delta,
    read_setup_log,
    run_worktree_setup,
    split_deferred_env,
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


def _run_claimed_setup(worktree: Path, commands: tuple[str, ...]) -> str | None:
    """Claim and run a worktree's setup; return its warning."""
    git_dir = claim_worktree_setup(workspace=str(worktree))
    assert git_dir is not None
    result = run_worktree_setup(
        commands, git_dir=git_dir, workspace=str(worktree), session_id="conv_1", env={}
    )
    return result.warning


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


def test_apply_env_delta_merges_path_and_never_overrides_runner_wiring() -> None:
    env = {"RUNNER_ID": "real", "VIRTUAL_ENV": "/old", "PATH": "/pinned/bin:/usr/bin"}
    delta = EnvDelta(
        set_vars={"PATH": "/conda/bin:/usr/bin", "RUNNER_ID": "spoofed"},
        unset_vars=frozenset({"VIRTUAL_ENV", "RUNNER_ID"}),
    )

    apply_env_delta(env, delta)

    # The project's order wins; the runner's pinned entry stays reachable.
    assert env == {"RUNNER_ID": "real", "PATH": "/conda/bin:/usr/bin:/pinned/bin"}


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


def test_probe_failure_warning_points_to_the_log_without_the_output(
    bash_login_shell: None, tmp_path: Path
) -> None:
    delta, warning = probe_env_delta(
        ("echo 'token=s3cret' >&2; false", "export NEVER_RUNS=1"), workspace=str(tmp_path)
    )

    assert delta is None
    assert warning is not None and "exit 1" in warning and "log:" in warning
    # Warnings are shown in shared sessions, so command output stays in the host log.
    assert "s3cret" not in warning


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


def test_setup_runs_once_per_worktree_even_after_the_commands_change(
    bash_login_shell: None, linked_worktree: Path
) -> None:
    marker = pending_setup_marker(str(linked_worktree))
    assert marker is not None and not marker.exists()

    warning = _run_claimed_setup(
        linked_worktree, ("echo ran >> setup.log", 'test -d "$OMNIGENT_ROOT_PATH/.git"')
    )

    assert warning is None
    assert json.loads(marker.read_text())["status"] == "succeeded"
    # The marker lives in the worktree's private git dir, not the checkout.
    assert marker.parent.parent.parent.name == ".git"
    # An edited list does not re-run where an agent may already be working.
    assert pending_setup_marker(str(linked_worktree)) is None
    assert claim_worktree_setup(workspace=str(linked_worktree)) is None
    assert (linked_worktree / "setup.log").read_text().split() == ["ran"]


def test_failed_setup_names_the_step_records_it_and_can_be_forced_again(
    bash_login_shell: None, linked_worktree: Path
) -> None:
    warning = _run_claimed_setup(
        linked_worktree, ("true", "echo 'token=s3cret' >&2; exit 3", "touch never-ran")
    )

    assert warning == "setup command 2 of 3 failed (exit 3)"
    assert not (linked_worktree / "never-ran").exists()
    # Recorded, so a waiting runner proceeds and resuming does not retry it...
    assert claim_worktree_setup(workspace=str(linked_worktree)) is None
    # ...until the user asks for a re-run.
    assert clear_setup_marker(str(linked_worktree)) is True
    assert claim_worktree_setup(workspace=str(linked_worktree)) is not None


def test_concurrent_launches_in_one_worktree_run_setup_once(linked_worktree: Path) -> None:
    first = claim_worktree_setup(workspace=str(linked_worktree))
    second = claim_worktree_setup(workspace=str(linked_worktree))

    assert first is not None
    assert second is None
    run_worktree_setup(
        ("true",), git_dir=first, workspace=str(linked_worktree), session_id=None, env={}
    )


def test_setup_is_skipped_outside_a_linked_worktree(linked_worktree: Path, tmp_path: Path) -> None:
    assert claim_worktree_setup(workspace=str(tmp_path / "repo")) is None
    assert claim_worktree_setup(workspace=str(tmp_path)) is None


def test_setup_interrupted_by_host_shutdown_runs_again_next_launch(
    bash_login_shell: None, linked_worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    threading.Timer(0.5, kill_running_project_commands).start()

    warning = _run_claimed_setup(linked_worktree, ("sleep 30",))

    assert warning == "interrupted by host shutdown"
    assert claim_worktree_setup(workspace=str(linked_worktree)) is not None


def test_a_finished_host_shutdown_does_not_mark_later_setups_interrupted(
    bash_login_shell: None, linked_worktree: Path
) -> None:
    kill_running_project_commands()

    assert _run_claimed_setup(linked_worktree, ("true",)) is None


def test_env_commands_may_span_lines_and_stop_at_the_first_failure(
    bash_login_shell: None, tmp_path: Path
) -> None:
    delta, warning = probe_env_delta(
        ('export FROM_HEREDOC="$(cat <<EOF\nmulti\nEOF\n)"', "false", "export NEVER=1"),
        workspace=str(tmp_path),
    )

    assert delta is None and warning is not None and "exit 1" in warning
    delta, warning = probe_env_delta(
        ('export FROM_HEREDOC="$(cat <<EOF\nmulti\nEOF\n)"',), workspace=str(tmp_path)
    )
    assert warning is None and delta is not None and delta.set_vars["FROM_HEREDOC"] == "multi"


def test_agent_sockets_and_python_runtime_vars_never_reach_the_runner_process() -> None:
    delta = compute_env_delta(
        {"PATH": "/usr/bin"},
        {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/tmp/a", "PYTHONPATH": "/proj", "FOO": "1"},
    )

    now, deferred = split_deferred_env(delta)

    assert now.set_vars == {"FOO": "1"}
    assert deferred == {"set": {"PYTHONPATH": "/proj"}, "unset": []}


def test_a_failed_probe_is_remembered_briefly(
    bash_login_shell: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real_probe = project_setup_runner.probe_env_delta

    def counting_probe(commands: tuple[str, ...], **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return real_probe(commands, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(project_setup_runner, "probe_env_delta", counting_probe)
    cache = EnvProbeCache(failure_ttl_s=60)
    broken = ProjectSetup(env_commands=("false",))

    first = prepare_project_env(broken, workspace=str(tmp_path), cache=cache)
    second = prepare_project_env(broken, workspace=str(tmp_path), cache=cache)

    assert calls == 1
    assert first.warnings == second.warnings and first.warnings


def test_the_setup_log_is_readable_for_its_worktree_only(
    bash_login_shell: None, linked_worktree: Path, tmp_path: Path
) -> None:
    assert read_setup_log(str(linked_worktree)) is None

    _run_claimed_setup(linked_worktree, ("echo installing-deps",))

    log = read_setup_log(str(linked_worktree))
    assert log is not None and "installing-deps" in log
    assert read_setup_log(str(tmp_path / "repo")) is None
