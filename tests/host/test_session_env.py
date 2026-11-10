"""Tests for the per-session environment a launching CLI hands its runner."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

import click
import pytest

from omnigent._platform import IS_WINDOWS
from omnigent.host.session_env import (
    SESSION_ENV_PASSTHROUGH_ENV_VAR,
    _handoff_path,
    consume_session_env_handoff,
    requested_session_env,
    session_env_handoff,
)

_POSIX_ONLY = pytest.mark.skipif(
    IS_WINDOWS, reason="Forwarding relies on POSIX file ownership and modes"
)
_PLANTED_LAUNCH = "planted-launch"


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data_dir))
    return data_dir


def _handoff_files(data_dir: Path) -> list[Path]:
    directory = data_dir / "session-env"
    return sorted(directory.iterdir()) if directory.exists() else []


def _plant_handoff(session_id: str, payload: object, mode: int = 0o600) -> Path:
    path = _handoff_path(session_id, _PLANTED_LAUNCH)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_requested_session_env_reads_listed_names_and_skips_unset_ones() -> None:
    source = {
        SESSION_ENV_PASSTHROUGH_ENV_VAR: (
            " ARCATERM_CAPABILITY , MACTERM_SOCKET,,ARCATERM_PROJECTS_ROOT"
        ),
        "ARCATERM_CAPABILITY": "capability-for-pane-1",
        "MACTERM_SOCKET": "",
        "UNLISTED": "nope",
    }

    assert requested_session_env(source) == {
        "ARCATERM_CAPABILITY": "capability-for-pane-1",
        "MACTERM_SOCKET": "",
    }


def test_requested_session_env_is_empty_without_a_list() -> None:
    assert requested_session_env({"ARCATERM_CAPABILITY": "capability-for-pane-1"}) == {}


@pytest.mark.parametrize(
    "name",
    [
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
        "omnigent_data_dir",
        "RUNNER_SERVER_URL",
        "Runner_Server_Url",
        "BAD-NAME",
    ],
)
def test_requested_session_env_rejects_runner_wiring_and_malformed_names(name: str) -> None:
    source = {SESSION_ENV_PASSTHROUGH_ENV_VAR: f"ARCATERM_CAPABILITY,{name}", name: "value-secret"}

    with pytest.raises(click.ClickException) as excinfo:
        requested_session_env(source)

    assert name in excinfo.value.message
    assert "value-secret" not in excinfo.value.message


@_POSIX_ONLY
def test_daemon_consumes_only_its_own_launchs_handoff_once(_data_dir: Path) -> None:
    values = {"ARCATERM_CAPABILITY": "capability-for-pane-1"}

    with session_env_handoff("conv_a", values) as handoff_id:
        assert handoff_id
        assert consume_session_env_handoff("conv_b", handoff_id) == {}
        assert consume_session_env_handoff("conv_a", "another-launch") == {}
        assert consume_session_env_handoff("conv_a", None) == {}
        assert len(_handoff_files(_data_dir)) == 1
        assert consume_session_env_handoff("conv_a", handoff_id) == values
        assert consume_session_env_handoff("conv_a", handoff_id) == {}
        assert _handoff_files(_data_dir) == []
    assert _handoff_files(_data_dir) == []


def test_launch_without_values_hands_off_nothing(_data_dir: Path) -> None:
    with session_env_handoff("conv_a", {}) as handoff_id:
        assert handoff_id is None
        assert _handoff_files(_data_dir) == []


@_POSIX_ONLY
def test_handoff_is_owner_only_and_removed_when_the_launch_fails(_data_dir: Path) -> None:
    with pytest.raises(RuntimeError):
        with session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "capability-for-pane-1"}):
            (path,) = _handoff_files(_data_dir)
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert "conv_a" not in path.name
            raise RuntimeError("launch failed")

    assert _handoff_files(_data_dir) == []


@_POSIX_ONLY
def test_abandoned_handoff_is_left_alone_by_later_launches_until_it_expires(
    _data_dir: Path,
) -> None:
    killed_cli = session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "killed-pane"})
    killed_cli.__enter__()
    (abandoned,) = _handoff_files(_data_dir)

    assert consume_session_env_handoff("conv_a", None) == {}
    with session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "new-pane"}) as handoff_id:
        assert consume_session_env_handoff("conv_a", handoff_id) == {
            "ARCATERM_CAPABILITY": "new-pane"
        }
    assert _handoff_files(_data_dir) == [abandoned]

    old = time.time() - 3600
    os.utime(abandoned, (old, old))
    assert consume_session_env_handoff("conv_a", None) == {}
    assert _handoff_files(_data_dir) == []


@_POSIX_ONLY
def test_overlapping_launches_of_one_session_each_hand_off_their_own_values(
    _data_dir: Path,
) -> None:
    with (
        session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "first-pane"}) as first,
        session_env_handoff("conv_a", {"ARCATERM_CAPABILITY": "second-pane"}) as second,
    ):
        assert consume_session_env_handoff("conv_a", second) == {
            "ARCATERM_CAPABILITY": "second-pane"
        }
        assert consume_session_env_handoff("conv_a", first) == {
            "ARCATERM_CAPABILITY": "first-pane"
        }
    assert _handoff_files(_data_dir) == []


@_POSIX_ONLY
def test_consume_ignores_and_removes_a_handoff_other_users_can_read(
    _data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _plant_handoff("conv_a", {"ARCATERM_CAPABILITY": "leaked"}, mode=0o644)

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {}
    assert _handoff_files(_data_dir) == []
    assert "readable by other users" in caplog.text
    assert "leaked" not in caplog.text


def test_windows_launch_refuses_values_before_writing_any(
    _data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.host.session_env.IS_WINDOWS", True)
    permissive_dir = _data_dir / "session-env"
    permissive_dir.mkdir(parents=True)
    os.chmod(permissive_dir, 0o777)
    listed = {SESSION_ENV_PASSTHROUGH_ENV_VAR: "ARCATERM_CAPABILITY,MACTERM_SOCKET"}

    with pytest.raises(click.ClickException) as excinfo:
        requested_session_env({**listed, "ARCATERM_CAPABILITY": "value-secret"})

    assert "not supported on Windows" in excinfo.value.message
    assert "value-secret" not in excinfo.value.message
    assert requested_session_env(listed) == {}
    assert _handoff_files(_data_dir) == []


def test_windows_daemon_ignores_and_removes_a_handoff_it_cannot_verify(
    _data_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_handoff("conv_a", {"ARCATERM_CAPABILITY": "capability-from-elsewhere"})
    monkeypatch.setattr("omnigent.host.session_env.IS_WINDOWS", True)

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {}
    assert _handoff_files(_data_dir) == []
    assert "cannot be verified on Windows" in caplog.text
    assert "capability-from-elsewhere" not in caplog.text


@_POSIX_ONLY
def test_consume_ignores_and_removes_a_handoff_owned_by_another_user(
    _data_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_handoff("conv_a", {"ARCATERM_CAPABILITY": "planted"})
    daemon_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: daemon_uid)

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {}
    assert _handoff_files(_data_dir) == []
    assert "owned by another user" in caplog.text


@_POSIX_ONLY
def test_consume_ignores_a_symlinked_handoff(_data_dir: Path, tmp_path: Path) -> None:
    path = _plant_handoff("conv_a", {})
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"ARCATERM_CAPABILITY": "redirected"}), encoding="utf-8")
    os.chmod(target, 0o600)
    path.unlink()
    path.symlink_to(target)

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {}
    assert _handoff_files(_data_dir) == []
    assert target.exists()


def test_consume_ignores_a_handoff_left_by_an_earlier_cli(_data_dir: Path) -> None:
    path = _plant_handoff("conv_a", {"ARCATERM_CAPABILITY": "old-pane"})
    old = time.time() - 3600
    os.utime(path, (old, old))

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {}
    assert _handoff_files(_data_dir) == []


def test_any_daemon_launch_removes_expired_handoffs_of_other_sessions(_data_dir: Path) -> None:
    expired = _plant_handoff("conv_b", {"ARCATERM_CAPABILITY": "abandoned-pane"})
    expired_temp = expired.with_name(".session-env-abandoned")
    expired_temp.write_text("{}", encoding="utf-8")
    old = time.time() - 3600
    for path in (expired, expired_temp):
        os.utime(path, (old, old))
    waiting = _plant_handoff("conv_c", {"ARCATERM_CAPABILITY": "launching-pane"})

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {}

    assert _handoff_files(_data_dir) == [waiting]


@_POSIX_ONLY
def test_consume_keeps_only_forwardable_string_values(_data_dir: Path) -> None:
    _plant_handoff(
        "conv_a",
        {
            "ARCATERM_CAPABILITY": "capability-for-pane-1",
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": "forged",
            "RUNNER_SERVER_URL": "http://elsewhere",
            "MACTERM_SESSION": 7,
        },
    )

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {
        "ARCATERM_CAPABILITY": "capability-for-pane-1"
    }


def test_consume_ignores_a_malformed_handoff(_data_dir: Path) -> None:
    path = _plant_handoff("conv_a", {})
    path.write_text("{not json", encoding="utf-8")

    assert consume_session_env_handoff("conv_a", _PLANTED_LAUNCH) == {}
    assert _handoff_files(_data_dir) == []
