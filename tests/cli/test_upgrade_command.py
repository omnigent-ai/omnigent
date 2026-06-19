"""Tests for the ``omni upgrade`` command (omnigent.cli.upgrade)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.host.local_server import LocalServerInfo
from omnigent.update_check import _InstalledWheelInfo


def _uv_registry_info() -> _InstalledWheelInfo:
    """A registry uv-tool install → ``uv tool upgrade omnigent`` (runnable)."""
    return _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer="uv",
        vcs_url=None,
        commit_sha=None,
        is_editable=False,
        package_version="0.1.0",
        detected_installer="uv",
    )


def _git_install_info() -> _InstalledWheelInfo:
    """A git/VCS uv-tool install → ``uv tool install --reinstall git+…``."""
    return _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer="uv",
        vcs_url="git+https://github.com/omnigent-ai/omnigent.git",
        commit_sha="a" * 40,
        is_editable=False,
        package_version="0.1.0",
        detected_installer="uv",
    )


@pytest.fixture
def _wheel_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the running interpreter look like a registry uv-tool install.

    Pins the resolved versions and stubs the install-shape detectors so
    every upgrade test starts from "installed v0.1.0 via uv, not a clone".
    The PyPI lookup and the server-stop side effects are stubbed per test.
    """
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.1.0")
    monkeypatch.setattr("omnigent.update_check._find_repo_root", lambda: None)
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", _uv_registry_info)
    # Neutralize the process side effects unless a test opts in to assert them.
    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(running=False, pid=None, port=None, url=None),
    )
    monkeypatch.setattr("omnigent.cli._stop_local_server_and_daemon", lambda *, force: False)


def test_upgrade_up_to_date(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """Latest == installed → reports up to date, exit 0, nothing stopped/run."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.1.0")

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("upgrade command ran while already up to date")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    assert "0.1.0" in result.output


def test_upgrade_check_reports_newer_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--check`` with a newer release → prints the delta and exits non-zero, no upgrade."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.2.0")

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("--check must not run the upgrade")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade", "--check"])

    assert result.exit_code == 1, result.output
    assert "v0.1.0 → v0.2.0" in result.output


def test_upgrade_runs_installer_and_drains_first(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A newer release → drain (no force), stop the server, run the uv command."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.2.0")

    events: list[str] = []
    monkeypatch.setattr(
        "omnigent.cli._wait_for_local_sessions_to_drain", lambda: events.append("drained")
    )

    def _stop(*, force: bool) -> bool:
        events.append(f"stop(force={force})")
        return True

    monkeypatch.setattr("omnigent.cli._stop_local_server_and_daemon", _stop)

    ran: list[str] = []

    def _run(command: str, _console: object) -> int:
        ran.append(command)
        return 0

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _run)
    # The post-upgrade verification re-reads the installed version in a fresh
    # subprocess; stub it to report the version the installer "moved" to.
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.2.0", None)
    )

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    # Drain happened before the stop, before the install ran.
    assert events == ["drained", "stop(force=False)"]
    assert ran == ["uv tool upgrade omnigent"]
    assert "Upgraded to v0.2.0" in result.output


def test_upgrade_force_skips_drain_and_force_stops(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--force`` skips the drain wait and force-stops the server."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.2.0")

    def _no_drain() -> None:
        raise AssertionError("--force must not wait for sessions to drain")

    monkeypatch.setattr("omnigent.cli._wait_for_local_sessions_to_drain", _no_drain)

    stop_calls: list[bool] = []
    monkeypatch.setattr(
        "omnigent.cli._stop_local_server_and_daemon",
        lambda *, force: stop_calls.append(force) or False,
    )
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.2.0", None)
    )

    result = CliRunner().invoke(cli, ["upgrade", "--force"])

    assert result.exit_code == 0, result.output
    assert stop_calls == [True]


def test_upgrade_install_failure_surfaces(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A non-zero installer exit → ClickException naming the status, exit non-zero."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.2.0")
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 3)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "exited with status 3" in result.output


def test_upgrade_index_unreachable(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """Index unreachable → clear error, no upgrade attempted."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: None)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "package index" in result.output


def test_upgrade_rejects_dev_clone(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """A source checkout is redirected to ``git pull``, not upgraded."""
    monkeypatch.setattr("omnigent.update_check._find_repo_root", lambda: Path("/repo"))

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "git pull" in result.output


def test_upgrade_rejects_editable(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """An editable install is redirected to ``git pull``, not upgraded."""
    editable = _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer="uv",
        vcs_url="file:///Users/me/omnigent",
        commit_sha=None,
        is_editable=True,
        package_version="0.1.0",
        detected_installer="uv",
    )
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", lambda: editable)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "git pull" in result.output


def test_upgrade_pre_check_detects_release_candidate(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--pre --check`` includes pre-releases and reports the rc as available."""
    captured: dict[str, object] = {}

    def _fetch(include_prereleases: bool = False, **_kw: object) -> str:
        captured["include_prereleases"] = include_prereleases
        return "0.1.1rc1" if include_prereleases else "0.1.0"

    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", _fetch)

    result = CliRunner().invoke(cli, ["upgrade", "--pre", "--check"])

    assert result.exit_code == 1, result.output
    assert "v0.1.0 → v0.1.1rc1" in result.output
    assert captured["include_prereleases"] is True


def test_upgrade_without_pre_ignores_release_candidate(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """Without ``--pre`` the rc is invisible → reports up to date."""

    def _fetch(include_prereleases: bool = False, **_kw: object) -> str | None:
        return "0.1.1rc1" if include_prereleases else "0.1.0"

    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", _fetch)

    result = CliRunner().invoke(cli, ["upgrade", "--check"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_count_running_sessions_ignores_idle_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``status == "running"`` sessions count; idle-connected ones don't."""
    from omnigent.cli import _count_running_sessions, _SessionPagesResult

    sessions = [
        {"id": "a", "status": "idle"},
        {"id": "b", "status": "running"},
        {"id": "c", "status": "idle"},
        {"id": "d", "status": "running"},
        {"id": "e"},  # missing status → not running
    ]
    monkeypatch.setattr(
        "omnigent.cli._fetch_session_pages",
        lambda **_kw: _SessionPagesResult(sessions=sessions, error=None),
    )

    assert _count_running_sessions("http://127.0.0.1:6767") == 2


def test_drain_returns_immediately_when_only_idle_connected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: 39 idle-but-connected sessions must not block the drain.

    Previously the drain counted *connected* sessions, so a box with idle
    sessions holding their connection open hung ``omni upgrade`` forever.
    """
    from omnigent.cli import _SessionPagesResult, _wait_for_local_sessions_to_drain

    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(running=True, pid=1, port=6767, url="http://127.0.0.1:6767"),
    )
    monkeypatch.setattr(
        "omnigent.cli._fetch_session_pages",
        lambda **_kw: _SessionPagesResult(
            sessions=[{"id": f"conv_{i}", "status": "idle"} for i in range(39)], error=None
        ),
    )

    _wait_for_local_sessions_to_drain()  # must return, not hang

    assert "Waiting for" not in capsys.readouterr().out


def test_upgrade_pre_passes_prerelease_flag_to_installer(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--pre`` upgrade runs the uv command with ``--prerelease allow``."""
    monkeypatch.setattr(
        "omnigent.update_check.fetch_latest_version",
        lambda include_prereleases=False, **_kw: "0.1.1rc1",
    )
    monkeypatch.setattr("omnigent.cli._wait_for_local_sessions_to_drain", lambda: None)
    monkeypatch.setattr("omnigent.cli._stop_local_server_and_daemon", lambda *, force: False)
    ran: list[str] = []
    monkeypatch.setattr(
        "omnigent.update_check._run_upgrade_command",
        lambda command, _console: ran.append(command) or 0,
    )
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.1.1rc1", None)
    )

    result = CliRunner().invoke(cli, ["upgrade", "--pre"])

    assert result.exit_code == 0, result.output
    assert ran == ["uv tool upgrade omnigent --prerelease allow"]


def test_upgrade_noop_install_reports_failure_not_success(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """Regression: a no-op upgrade must NOT claim success.

    The installer exits 0 but the installed version doesn't move (a pinned
    spec, a cooldown/exclude-newer excluding the release, or a stale index
    cache). The old code printed "✓ Upgraded to v0.2.0" anyway, so the next
    ``--check`` kept reporting the same update — the bug this guards against.
    """
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.2.0")
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 0)
    # The installer ran (exit 0) but the version is unchanged.
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.1.0", None)
    )

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0, result.output
    assert "still v0.1.0" in result.output
    assert "✓ Upgraded" not in result.output


def test_upgrade_unconfirmed_version_is_honest(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """When the new version can't be read back, don't assert a version."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.2.0")
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: (None, None)
    )

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "couldn't confirm" in result.output
    assert "✓ Upgraded" not in result.output


def test_upgrade_git_install_up_to_date(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A git install whose ref hasn't moved reports up to date by commit."""
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", _git_install_info)
    # fetch_latest_version must never be consulted for a git install.
    monkeypatch.setattr(
        "omnigent.update_check.fetch_latest_version",
        lambda *_a, **_k: pytest.fail("git install must not query PyPI"),
    )
    monkeypatch.setattr("omnigent.update_check._remote_git_head", lambda _url: "a" * 40)

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("nothing to re-pull when already at the ref HEAD")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_upgrade_git_check_behind_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--check`` on a git install behind its ref reports the delta, exits non-zero."""
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", _git_install_info)
    monkeypatch.setattr("omnigent.update_check._remote_git_head", lambda _url: "b" * 40)

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("--check must not re-pull")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade", "--check"])

    assert result.exit_code == 1, result.output
    assert "newer commit is available" in result.output


def test_upgrade_git_install_repulls_and_verifies_commit(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A git install behind its ref re-pulls and reports the NEW commit."""
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", _git_install_info)
    monkeypatch.setattr("omnigent.update_check._remote_git_head", lambda _url: "b" * 40)

    ran: list[str] = []
    monkeypatch.setattr(
        "omnigent.update_check._run_upgrade_command",
        lambda command, _console: ran.append(command) or 0,
    )
    # After re-pull the install is at the new commit.
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.1.0", "b" * 40)
    )

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert ran == ["uv tool install --reinstall git+https://github.com/omnigent-ai/omnigent.git"]
    assert "Updated to git bbbbbbbbb" in result.output


def test_upgrade_git_install_noop_does_not_claim_update(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """If the re-pull leaves the commit unchanged, say so — don't claim an update."""
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", _git_install_info)
    # Remote unknown (e.g. offline) → re-pull anyway, then verify by commit.
    monkeypatch.setattr("omnigent.update_check._remote_git_head", lambda _url: None)
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.1.0", "a" * 40)
    )

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "nothing changed" in result.output
    assert "✓ Updated" not in result.output


def test_upgrade_git_confirmed_behind_but_repull_noop_fails(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """Regression: known-behind git install whose re-pull doesn't move the commit
    must FAIL, not silently exit 0 (else it recreates the loop on the git path)."""
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", _git_install_info)
    # Remote HEAD differs from the installed commit ("a"*40) → positively behind.
    monkeypatch.setattr("omnigent.update_check._remote_git_head", lambda _url: "b" * 40)
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 0)
    # …but the re-pull left the install on the SAME commit (pinned ref / cached).
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.1.0", "a" * 40)
    )

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0, result.output
    assert "still at aaaaaaaaa" in result.output
    assert "✓ Updated" not in result.output
