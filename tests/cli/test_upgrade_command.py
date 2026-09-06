"""Tests for the ``omni upgrade`` command (omnigent.cli.upgrade)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.cli_invocation import WRAPPER_COMMAND_ENV
from omnigent.host.local_server import LocalServerInfo
from omnigent.update_check import (
    _build_nightly_upgrade_suggestion,
    _build_upgrade_suggestion,
    _InstalledWheelInfo,
    _newest_nightly_version,
    _upgrade_failure_message,
    _uv_python_pin,
)

# uv upgrade commands pin the interpreter omnigent is running under, so
# expectations derive the flag instead of hardcoding a python version.
_UV_PY = _uv_python_pin()


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
    # Pretend a uv tool receipt exists so the install is not mistaken for uv pip.
    monkeypatch.setattr(
        "omnigent.update_check._uv_tool_receipt_path", lambda: Path("/dev/null/uv-receipt.toml")
    )
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


# ── deprecated ``omni update`` spelling ──────────────────────────────


def test_update_is_hidden_and_shares_upgrade_options() -> None:
    """``update`` is a hidden shim over ``upgrade``, not a second command.

    It has to be its own Click object to warn, so the guard is that it reuses
    ``upgrade``'s parameter objects — the option surface cannot drift.
    """
    update_cmd = cli.commands["update"]
    upgrade_cmd = cli.commands["upgrade"]

    assert update_cmd is not upgrade_cmd
    assert update_cmd.hidden is True
    assert upgrade_cmd.hidden is False
    # The very same Parameter objects, not merely equally-named ones.
    assert all(a is b for a, b in zip(update_cmd.params, upgrade_cmd.params, strict=True))


def test_update_warns_then_runs_upgrade(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``omni update`` still works, but warns and names ``upgrade``."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.1.0")

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("upgrade command ran while already up to date")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "`update` is deprecated" in result.stderr
    assert "upgrade" in result.stderr
    assert "up to date" in result.stdout
    assert "0.1.0" in result.stdout


def test_update_deprecation_notice_honors_the_wrapper_command(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """Under a wrapper the notice says ``isaac omni upgrade``, not ``omni``.

    The wrapper guard rejects naked ``omnigent`` calls, so a hint spelling the
    naked binary would name a command that cannot run.
    """
    monkeypatch.setenv(WRAPPER_COMMAND_ENV, "isaac omni")
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.1.0")
    monkeypatch.setattr(
        "omnigent.update_check._run_upgrade_command",
        lambda *_a, **_k: pytest.fail("upgrade ran while already up to date"),
    )

    result = CliRunner().invoke(cli, ["update"])

    assert result.exit_code == 0, result.output
    assert "`isaac omni upgrade`" in result.stderr


def test_update_check_matches_upgrade_check(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``update --check`` behaves identically to ``upgrade --check``."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda *_a, **_k: "0.2.0")

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("--check must not run the upgrade")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    runner = CliRunner()
    update_result = runner.invoke(cli, ["update", "--check"])
    upgrade_result = runner.invoke(cli, ["upgrade", "--check"])

    assert update_result.exit_code == upgrade_result.exit_code == 1
    assert update_result.stdout == upgrade_result.stdout
    assert "v0.1.0 → v0.2.0" in update_result.stdout
    # The warning is the only difference between the two.
    assert "`update` is deprecated" in update_result.stderr
    assert "deprecated" not in upgrade_result.stderr


def test_update_suppresses_update_check_like_upgrade() -> None:
    """``update`` is special-cased alongside ``upgrade`` in the skip set."""
    from omnigent.cli import _should_skip_update_check

    assert _should_skip_update_check(["update"]) is True
    assert _should_skip_update_check(["upgrade"]) is True


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
    assert ran == [
        f"uv tool install --reinstall{_UV_PY} git+https://github.com/omnigent-ai/omnigent.git"
    ]
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


def _nightly_ls_remote(*names: str) -> str:
    """Synthesize ``git ls-remote --tags`` output for the given tag names."""
    sha = "0" * 40
    return "".join(f"{sha}\trefs/tags/{name}\n" for name in names)


def test_newest_nightly_version_filters_and_sorts() -> None:
    """Only vX.Y.Z.devYYYYMMDD tags count; PEP 440 order survives version bumps."""
    out = _nightly_ls_remote(
        "v0.4.0.dev0",  # legacy stray: no 8-digit datestamp
        "v0.8.0rc1",  # rc: not a nightly
        "v0.8.0",  # final: not a nightly
        "v0.8.0.dev20260731",
        "v0.8.0.dev20260801",
        "v0.8.0.dev20260801^{}",  # annotated-tag peel line
        "v0.9.0.dev20260801",
        "v0.10.0.dev20260101",  # older date but newer version: wins
    )
    assert _newest_nightly_version(out) == "0.10.0.dev20260101"


def test_newest_nightly_version_none_when_no_nightlies() -> None:
    """No nightly-shaped tags (or no output at all) → None."""
    assert _newest_nightly_version("") is None
    assert _newest_nightly_version(_nightly_ls_remote("v0.8.0", "v0.8.0rc1")) is None


def test_nightly_suggestion_shapes_per_installer() -> None:
    """Every installer maps to a git spec pinned to the nightly tag."""
    version = "0.9.0.dev20260804"
    spec = f"git+https://github.com/omnigent-ai/omnigent@v{version}"

    def info_for(installer: str | None) -> _InstalledWheelInfo:
        return _InstalledWheelInfo(
            install_time_epoch=0.0,
            installer=installer,
            vcs_url=None,
            commit_sha=None,
            is_editable=False,
            package_version="0.1.0",
            detected_installer=installer,
        )

    uv = _build_nightly_upgrade_suggestion(info_for("uv"), version)
    # ``--reinstall``, never ``--force``: uv's ``--force`` removes the working
    # install before the replacement is built, and nightlies build from source,
    # so a failed build would leave nothing behind.
    assert (uv.command, uv.runnable) == (
        f"uv tool install --reinstall{_UV_PY} {spec}",
        True,
    )
    pipx = _build_nightly_upgrade_suggestion(info_for("pipx"), version)
    assert (pipx.command, pipx.runnable) == (f"pipx install --force {spec}", True)
    pip = _build_nightly_upgrade_suggestion(info_for("pip"), version)
    assert pip.runnable and pip.command.endswith(f"install --force-reinstall {spec}")
    unknown = _build_nightly_upgrade_suggestion(info_for("conda"), version)
    assert not unknown.runnable and spec in unknown.command


def test_upgrade_nightly_up_to_date(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """Installed version == newest nightly → no-op, nothing stopped or run."""
    monkeypatch.setattr("omnigent.update_check._latest_nightly_version", lambda: "0.1.0")

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("upgrade command ran while already on the newest nightly")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade", "--nightly"])

    assert result.exit_code == 0, result.output
    assert "already on the newest nightly" in result.output


def test_upgrade_nightly_installs_pinned_tag(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A newer nightly → runs the git-pinned installer command and verifies."""
    monkeypatch.setattr(
        "omnigent.update_check._latest_nightly_version", lambda: "0.2.0.dev20260804"
    )

    ran: list[str] = []

    def _run(command: str, _console: object) -> int:
        ran.append(command)
        return 0

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _run)
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution",
        lambda: ("0.2.0.dev20260804", None),
    )

    result = CliRunner().invoke(cli, ["upgrade", "--nightly"])

    assert result.exit_code == 0, result.output
    assert ran == [
        f"uv tool install --reinstall{_UV_PY} "
        "git+https://github.com/omnigent-ai/omnigent@v0.2.0.dev20260804"
    ]
    assert "Upgraded to nightly v0.2.0.dev20260804" in result.output


def test_upgrade_nightly_check_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--nightly --check`` with a different nightly → report and exit 1, no upgrade."""
    monkeypatch.setattr(
        "omnigent.update_check._latest_nightly_version", lambda: "0.2.0.dev20260804"
    )

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("--check must not run the upgrade")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade", "--nightly", "--check"])

    assert result.exit_code == 1, result.output
    assert "Newest nightly: v0.2.0.dev20260804" in result.output


def test_upgrade_nightly_no_tags_is_actionable(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """No nightly tag reachable → a clear error, not a crash or a PyPI fallback."""
    monkeypatch.setattr("omnigent.update_check._latest_nightly_version", lambda: None)

    result = CliRunner().invoke(cli, ["upgrade", "--nightly"])

    assert result.exit_code != 0, result.output
    assert "Couldn't find a nightly tag" in result.output


def test_nightly_suggestion_preserves_and_unions_extras() -> None:
    """Receipt extras union with --extra overrides, as an egg fragment on the spec."""
    info = _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer="uv",
        vcs_url=None,
        commit_sha=None,
        is_editable=False,
        package_version="0.1.0",
        detected_installer="uv",
        extras=("all",),
    )

    suggestion = _build_nightly_upgrade_suggestion(
        info, "0.9.0.dev20260804", extra_overrides=("server",)
    )

    assert suggestion.runnable
    assert suggestion.command == (
        f"uv tool install --reinstall{_UV_PY} "
        "git+https://github.com/omnigent-ai/omnigent@v0.9.0.dev20260804#egg=omnigent[all,server]"
    )


def test_upgrade_nightly_refuses_registry_pip(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A registry pip install → manual git command, exit 0, installer never runs."""
    pip_info = _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer="pip",
        vcs_url=None,
        commit_sha=None,
        is_editable=False,
        package_version="0.1.0",
        detected_installer="pip",
    )
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", lambda: pip_info)
    monkeypatch.setattr(
        "omnigent.update_check._latest_nightly_version", lambda: "0.2.0.dev20260804"
    )

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("refused install shape must not run the installer")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade", "--nightly"])

    assert result.exit_code == 0, result.output
    assert "install the nightly manually" in result.output
    assert "git+https://github.com/omnigent-ai/omnigent@v0.2.0.dev20260804" in result.output


def test_upgrade_nightly_dry_run_prints_without_running(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--nightly --dry-run`` prints the command and exits before any side effects."""
    monkeypatch.setattr(
        "omnigent.update_check._latest_nightly_version", lambda: "0.2.0.dev20260804"
    )

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("--dry-run must not run the upgrade")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade", "--nightly", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert (
        f"Would run: uv tool install --reinstall{_UV_PY} "
        "git+https://github.com/omnigent-ai/omnigent@v0.2.0.dev20260804" in result.output
    )


def test_upgrade_nightly_rejects_target_version(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--nightly`` and ``--target-version`` are mutually exclusive flags."""
    result = CliRunner().invoke(cli, ["upgrade", "--nightly", "--target-version", "0.9.0"])

    assert result.exit_code == 2, result.output
    assert "mutually exclusive" in result.output


def test_nightly_uv_never_uses_destructive_force() -> None:
    """No uv upgrade command may use ``--force``, whatever the install shape.

    uv's ``--force`` removes the tool environment (and the ``omni`` /
    ``omnigent`` executables) before the replacement is built. Nightlies build
    from source, so the build can fail, and then nothing is left to fall back
    to. ``--reinstall`` performs the same registry-to-git hop and the same
    tag-to-tag hop while keeping the working install until the new one exists.
    """
    version = "0.9.0.dev20260804"
    for extras in ((), ("databricks",)):
        for vcs_url in (None, "git+https://github.com/a-fork/omnigent.git"):
            info = _InstalledWheelInfo(
                install_time_epoch=0.0,
                installer="uv",
                vcs_url=vcs_url,
                commit_sha=None,
                is_editable=False,
                package_version="0.8.2",
                detected_installer="uv",
                extras=extras,
            )
            command = _build_nightly_upgrade_suggestion(info, version).command
            assert "--force" not in command, command
            assert command.startswith(f"uv tool install --reinstall{_UV_PY} ")
            # Always the nightly tag, never the fork's recorded URL.
            assert f"git+https://github.com/omnigent-ai/omnigent@v{version}" in command


def test_nightly_pipx_spells_out_the_tag_instead_of_reinstall() -> None:
    """pipx must not fall back to ``reinstall`` for a nightly hop.

    ``pipx reinstall`` re-pulls the spec pipx recorded at install time, i.e.
    the *old* tag, which would silently no-op the upgrade. Only the extras-less
    in-place refresh may use it.
    """

    def pipx_info(vcs_url: str | None) -> _InstalledWheelInfo:
        return _InstalledWheelInfo(
            install_time_epoch=0.0,
            installer="pipx",
            vcs_url=vcs_url,
            commit_sha=None,
            is_editable=False,
            package_version="0.8.2",
            detected_installer="pipx",
        )

    nightly = _build_nightly_upgrade_suggestion(pipx_info(None), "0.9.0.dev20260804")
    assert nightly.command == (
        "pipx install --force git+https://github.com/omnigent-ai/omnigent@v0.9.0.dev20260804"
    )
    # An ordinary extras-less VCS refresh still uses the cheap re-pull.
    same_source = _build_upgrade_suggestion(pipx_info("git+https://host/omnigent.git"))
    assert same_source.command == "pipx reinstall omnigent"


def test_upgrade_failure_message_reports_a_destroyed_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed upgrade must not claim the old install survived when it didn't."""
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: (None, None)
    )

    message = _upgrade_failure_message(1, {"databricks"})

    assert "your previous install is intact" not in message
    assert "no longer installed" in message
    # Actionable recovery, preserving the extras this install had.
    assert "install.sh | sh -s -- --extra databricks" in message
    assert "omnigent login" in message
    assert "OMNIGENT_SKIP_WEB_UI=true" in message


def test_upgrade_failure_message_confirms_a_surviving_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the probe still finds omnigent, the reassuring message is correct."""
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: ("0.8.2", None)
    )

    message = _upgrade_failure_message(1)

    assert message == "Upgrade command exited with status 1; your previous install is intact."


def test_upgrade_nightly_failure_surfaces_recovery_when_install_is_gone(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A failed nightly build that took the install with it must say so.

    This is the reported bug end to end: the web-UI build fails, the installer
    exits non-zero, and the CLI used to answer "your previous install is
    intact" purely from the exit code, sending the user to inspect PATH while
    ``omni`` was actually gone.
    """
    monkeypatch.setattr(
        "omnigent.update_check._latest_nightly_version", lambda: "0.2.0.dev20260804"
    )
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a: 1)
    # The installer replaced the environment, then the build failed.
    monkeypatch.setattr(
        "omnigent.update_check._probe_installed_distribution", lambda: (None, None)
    )

    result = CliRunner().invoke(cli, ["upgrade", "--nightly"])

    assert result.exit_code != 0
    assert "your previous install is intact" not in result.output
    assert "no longer installed" in result.output
    assert "install.sh" in result.output
