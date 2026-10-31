"""Host execution of a project's ``env_commands`` and ``setup_commands``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.host import project_setup_runner
from omnigent.host.git_worktree import linked_worktree
from omnigent.host.project_setup_runner import (
    SETUP_MARKER_NAME,
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
    release_unclaimed_gate,
    run_worktree_setup,
)
from omnigent.project_setup import ProjectSetup
from tests._helpers.git_repo import repo_with_worktree
from tests._helpers.polling import poll_until

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make bash the user's shell with a private home, so only these rc files load."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text("source ~/.bashrc\n")
    (home / ".bashrc").write_text("")
    monkeypatch.setenv("SHELL", shutil.which("bash") or "/bin/bash")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def worktree(home: Path, tmp_path: Path) -> Path:
    return repo_with_worktree(tmp_path / "src")[1]


def _run_setup(worktree: Path, lines: tuple[str, ...], **kwargs: object) -> str | None:
    """Claim and run a worktree's setup; return its warning."""
    git_dir = claim_worktree_setup(workspace=str(worktree))
    assert git_dir is not None
    result = run_worktree_setup(
        lines,
        git_dir=git_dir,
        workspace=str(worktree),
        session_id="conv_1",
        env=os.environ.copy(),
        **kwargs,  # type: ignore[arg-type]
    )
    return result.warning


def test_env_delta_keeps_project_changes_only() -> None:
    before = {"PATH": "/usr/bin", "PWD": "/a", "VIRTUAL_ENV": "/old", "HOME": "/h"}
    after = {
        "PATH": "/conda/bin:/usr/bin",
        "PWD": "/b",
        "HOME": "/h",
        "CONDA_DEFAULT_ENV": "proj",
        "RUNNER_ID": "spoofed",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "PYTHONPATH": "/proj",
    }

    delta = compute_env_delta(before, after)

    assert delta.set_vars == {"PATH": "/conda/bin:/usr/bin", "CONDA_DEFAULT_ENV": "proj"}
    assert delta.unset_vars == frozenset({"VIRTUAL_ENV"})
    assert delta.path_prepend == ("/conda/bin",)
    # Would change how the runner's own interpreter starts.
    assert delta.withheld == frozenset({"PYTHONPATH"})


def test_apply_env_delta_merges_path_and_never_overrides_runner_wiring() -> None:
    env = {"RUNNER_ID": "real", "VIRTUAL_ENV": "/old", "PATH": "/pinned/bin:/usr/bin"}
    delta = EnvDelta(
        set_vars={"PATH": "/conda/bin:/usr/bin", "RUNNER_ID": "spoofed"},
        unset_vars=frozenset({"VIRTUAL_ENV", "RUNNER_ID"}),
    )

    apply_env_delta(env, delta)

    assert env == {"RUNNER_ID": "real", "PATH": "/conda/bin:/usr/bin:/pinned/bin"}


def test_probe_sources_the_commands_after_the_users_rc_files(home: Path, tmp_path: Path) -> None:
    (home / ".bashrc").write_text('tool_activate() { export TOOL_ENV="$1"; }\n')

    delta, warning = probe_env_delta(
        ("tool_activate proj", 'export GREETING="$(cat <<EOF', "multi line", "EOF", ')"'),
        workspace=str(tmp_path),
    )

    assert warning is None and delta is not None
    assert delta.set_vars["TOOL_ENV"] == "proj"
    assert delta.set_vars["GREETING"] == "multi line"


def test_probe_failure_names_exit_code_and_log_but_not_output(home: Path, tmp_path: Path) -> None:
    delta, warning = probe_env_delta(("echo 'token=s3cret' >&2; false",), workspace=str(tmp_path))

    assert delta is None
    assert warning is not None and "exit 1" in warning and "log:" in warning
    # Warnings are shown in shared sessions; output stays in the host log.
    assert "s3cret" not in warning


def test_probe_that_hangs_is_killed_at_its_timeout(home: Path, tmp_path: Path) -> None:
    started = time.monotonic()

    delta, warning = probe_env_delta(("sleep 30",), workspace=str(tmp_path), timeout=0.5)

    assert delta is None and warning is not None and "timed out" in warning
    assert time.monotonic() - started < 5


def test_probe_uses_the_account_login_shell_when_shell_is_unset(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SHELL")
    monkeypatch.setattr(
        "pwd.getpwuid", lambda _uid: SimpleNamespace(pw_shell=shutil.which("bash"))
    )

    delta, warning = probe_env_delta(("export FROM_ACCOUNT_SHELL=1",), workspace=str(tmp_path))

    assert warning is None and delta is not None
    assert delta.set_vars["FROM_ACCOUNT_SHELL"] == "1"


@pytest.mark.parametrize("succeeds", [True, False], ids=["success", "failure"])
def test_probe_results_are_shared_remembered_and_invalidated(
    monkeypatch: pytest.MonkeyPatch, succeeds: bool
) -> None:
    calls: list[tuple[str, ...]] = []
    release = threading.Event()

    def fake_probe(commands: tuple[str, ...], **_: object) -> tuple[EnvDelta | None, str | None]:
        calls.append(commands)
        release.wait(5)
        return (EnvDelta(set_vars={"A": "1"}), None) if succeeds else (None, "failed")

    monkeypatch.setattr(project_setup_runner, "probe_env_delta", fake_probe)
    cache = EnvProbeCache(failure_ttl_s=60)
    setup = ProjectSetup(env_commands=("export A=1",))

    def launch() -> None:
        prepare_project_env(setup, workspace="/w", cache=cache)

    # Launches that arrive together share one probe.
    launches = [threading.Thread(target=launch) for _ in range(3)]
    for thread in launches:
        thread.start()
    release.set()
    for thread in launches:
        thread.join(5)
    later = prepare_project_env(setup, workspace="/w", cache=cache)
    cache.invalidate("/w")
    prepare_project_env(setup, workspace="/w", cache=cache)

    assert len(calls) == 2, "one shared probe, then one after the explicit invalidation"
    assert (later.env_delta.set_vars == {"A": "1"}) is succeeds
    assert (later.warnings == ["failed"]) is not succeeds


def test_setup_runs_once_per_worktree_and_leaves_the_checkout_clean(
    worktree: Path, tmp_path: Path
) -> None:
    marker = pending_setup_marker(str(worktree))
    assert marker is not None

    warning = _run_setup(worktree, ('echo "$OMNIGENT_ROOT_PATH" > root.txt',))

    assert warning is None
    assert (
        Path((worktree / "root.txt").read_text().strip()) == (tmp_path / "src" / "repo").resolve()
    )
    assert json.loads(marker.read_text())["status"] == "succeeded"
    assert claim_worktree_setup(workspace=str(worktree)) is None
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True, check=True
    )
    assert status.stdout.split() == ["??", "root.txt"], "the marker stays out of the checkout"


def test_setup_puts_the_project_env_ahead_of_the_login_profile_path(
    home: Path, worktree: Path
) -> None:
    (home / ".bash_profile").write_text('export PATH="/profile/bin:$PATH"\n')

    warning = _run_setup(
        worktree, ('echo "$PATH" > path.txt',), path_prepend=("/project/env/bin",)
    )

    assert warning is None
    assert (worktree / "path.txt").read_text().startswith("/project/env/bin:/profile/bin:")


def test_failed_setup_stops_records_the_failure_and_can_be_forced_again(worktree: Path) -> None:
    warning = _run_setup(worktree, ("true", "echo 'token=s3cret' >&2; exit 3", "touch never-ran"))

    assert warning == "setup commands failed (exit 3)"
    assert not (worktree / "never-ran").exists()
    # Recorded, so a waiting runner proceeds and resuming does not retry it...
    assert claim_worktree_setup(workspace=str(worktree)) is None
    # ...until the user asks for a re-run.
    assert clear_setup_marker(str(worktree)) is True
    assert claim_worktree_setup(workspace=str(worktree)) is not None


def test_concurrent_launches_run_setup_once_and_keep_waiting_on_it(worktree: Path) -> None:
    first = claim_worktree_setup(workspace=str(worktree))
    second = claim_worktree_setup(workspace=str(worktree))
    release_unclaimed_gate(str(worktree))

    assert first is not None and second is None
    # A running setup is not marked skipped under a runner waiting on it.
    assert pending_setup_marker(str(worktree)) is not None


def test_a_setup_that_finishes_during_a_claim_is_neither_rerun_nor_overwritten(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = pending_setup_marker(str(worktree))
    assert marker is not None
    assert _run_setup(worktree, ("true",)) is None
    # A launch that checked for the marker just before the setup recorded it.
    monkeypatch.setattr(project_setup_runner, "pending_setup_marker", lambda _workspace: marker)

    assert claim_worktree_setup(workspace=str(worktree)) is None
    release_unclaimed_gate(str(worktree))
    assert json.loads(marker.read_text())["status"] == "succeeded"


def test_a_wait_on_setup_that_will_not_run_is_released(worktree: Path, tmp_path: Path) -> None:
    release_unclaimed_gate(str(worktree))

    assert pending_setup_marker(str(worktree)) is None
    assert claim_worktree_setup(workspace=str(tmp_path / "src" / "repo")) is None
    assert claim_worktree_setup(workspace=str(tmp_path)) is None


def test_setup_interrupted_by_host_shutdown_runs_again(worktree: Path) -> None:
    def shut_down_once_started() -> None:
        poll_until(lambda: (worktree / "started").exists(), timeout_s=10, interval_s=0.05)
        kill_running_project_commands()

    shutdown = threading.Thread(target=shut_down_once_started)
    shutdown.start()

    warning = _run_setup(worktree, ("touch started", "sleep 30"))
    shutdown.join(10)

    assert warning == "interrupted by host shutdown"
    assert _run_setup(worktree, ("true",)) is None


def test_setup_log_is_redacted_and_read_only_from_the_hosts_log_dir(worktree: Path) -> None:
    assert read_setup_log(str(worktree)) is None

    _run_setup(worktree, ("echo installing-deps", "echo 'API_KEY=fake-test-value'"))

    log = read_setup_log(str(worktree))
    assert log is not None and "installing-deps" in log
    assert "fake-test-value" not in log
    # The marker sits where the agent can write; a log path outside the host's
    # own log directory is refused.
    git_dir = linked_worktree(str(worktree)).git_dir  # type: ignore[union-attr]
    (git_dir / SETUP_MARKER_NAME).write_text(json.dumps({"log_path": "/etc/hosts"}))
    assert read_setup_log(str(worktree)) is None
