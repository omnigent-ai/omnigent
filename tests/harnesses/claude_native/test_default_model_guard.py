"""Tests for keeping Omnigent's ``/model`` switches out of the user's default model."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge, default_model_guard
from omnigent.harnesses.claude_native.bridge import claude_user_settings_path


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Settings file and state dir, with the background watcher off so tests settle by hand."""
    monkeypatch.setattr(default_model_guard, "_ensure_watcher", lambda *_args: None)
    return tmp_path / "settings.json", tmp_path / "state"


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"model": "opus", "theme": "dark"}, {"model": "opus", "theme": "dark"}),
        ({"theme": "dark"}, {"theme": "dark"}),
    ],
    ids=["puts the previous default back", "removes a key that was absent"],
)
def test_undoes_the_save_of_a_switch_omnigent_typed(
    paths: tuple[Path, Path], before: dict[str, object], after: dict[str, object]
) -> None:
    settings, state = paths
    _write(settings, before)
    settings.chmod(0o640)
    assert default_model_guard.begin_model_switch(settings, "claude-sonnet-5-5", state_dir=state)

    _write(settings, {**before, "model": "claude-sonnet-5-5"})  # Claude's save
    assert not default_model_guard.settle_default_model(settings, state_dir=state)

    assert _read(settings) == after
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    assert not list(state.glob("*.json"))


def test_a_failed_restore_stays_pending_and_is_retried(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    _write(settings, {"model": "sonnet"})
    real_replace = default_model_guard.os.replace
    calls: list[object] = []

    def flaky_replace(src: object, dst: object) -> None:
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError("settings.json is locked")
        real_replace(src, dst)

    monkeypatch.setattr(default_model_guard.os, "replace", flaky_replace)

    assert default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "sonnet"}
    assert not default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "opus"}


def test_an_edit_landing_mid_restore_is_kept(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    _write(settings, {"model": "sonnet"})
    real_chmod = default_model_guard.os.chmod

    def chmod_then_edit(path: object, mode: int) -> None:
        real_chmod(path, mode)
        if _read(settings) == {"model": "sonnet"}:  # another writer, once
            _write(settings, {"model": "sonnet", "theme": "dark"})

    monkeypatch.setattr(default_model_guard.os, "chmod", chmod_then_edit)

    default_model_guard.settle_default_model(settings, state_dir=state)

    assert _read(settings) == {"model": "opus", "theme": "dark"}


def test_keeps_a_users_own_change_and_restores_to_it(paths: tuple[Path, Path]) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)

    _write(settings, {"model": "haiku"})  # the user picks a new default meanwhile
    assert default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "haiku"}

    _write(settings, {"model": "sonnet"})  # the queued switch lands later
    default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "haiku"}


def test_a_second_session_does_not_take_an_unsettled_save_for_the_default(
    paths: tuple[Path, Path],
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    _write(settings, {"model": "sonnet"})  # the first session's save, not settled yet

    default_model_guard.begin_model_switch(settings, "haiku", state_dir=state)
    _write(settings, {"model": "haiku"})
    default_model_guard.settle_default_model(settings, state_dir=state)

    assert _read(settings) == {"model": "opus"}


def test_symlink_aliases_share_one_default_baseline(paths: tuple[Path, Path]) -> None:
    settings, state = paths
    alias = settings.parent / "alias.json"
    alias.symlink_to(settings)
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    _write(settings, {"model": "sonnet"})

    default_model_guard.begin_model_switch(alias, "haiku", state_dir=state)
    default_model_guard.settle_default_model(settings, state_dir=state)
    _write(settings, {"model": "haiku"})
    default_model_guard.settle_default_model(alias, state_dir=state)

    assert _read(settings) == {"model": "opus"}


def test_an_idle_poll_leaves_the_record_alone(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    writes: list[Path] = []
    real_write = default_model_guard._atomic_write
    monkeypatch.setattr(
        default_model_guard,
        "_atomic_write",
        lambda target, *args, **kwargs: (
            writes.append(target) or real_write(target, *args, **kwargs)
        ),
    )

    assert default_model_guard.settle_default_model(settings, state_dir=state)

    assert writes == []


def test_registers_nothing_when_the_default_is_already_that_model(
    paths: tuple[Path, Path],
) -> None:
    settings, state = paths
    _write(settings, {"model": "Sonnet"})

    assert default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state) is None
    assert not list(state.glob("*.json"))


def test_cancelling_forgets_only_that_switch(paths: tuple[Path, Path]) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    sonnet = default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    haiku = default_model_guard.begin_model_switch(settings, "haiku", state_dir=state)
    assert sonnet is not None and haiku is not None
    _write(settings, {"model": "sonnet"})
    default_model_guard.settle_default_model(settings, state_dir=state)

    sonnet.cancel()  # already settled while haiku keeps the record: a no-op
    haiku.cancel()
    _write(settings, {"model": "haiku"})

    assert not default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "haiku"}


def test_an_expired_switch_leaves_a_later_save_alone(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    now = time.time()
    monkeypatch.setattr(
        default_model_guard.time,
        "time",
        lambda: now + default_model_guard.PENDING_SWITCH_TTL_S + 1,
    )
    _write(settings, {"model": "sonnet"})

    assert not default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "sonnet"}


def test_the_watcher_restores_the_default_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, state = tmp_path / "settings.json", tmp_path / "state"
    _write(settings, {"model": "opus"})
    real_settle = default_model_guard.settle_default_model
    calls: list[Path] = []

    def settle_failing_once(path: Path, *, state_dir: Path) -> bool:
        calls.append(path)
        if len(calls) == 1:
            raise OSError("state dir briefly unavailable")
        return real_settle(path, state_dir=state_dir)

    monkeypatch.setattr(default_model_guard, "settle_default_model", settle_failing_once)
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)

    _write(settings, {"model": "sonnet"})
    deadline = time.monotonic() + 5
    while _read(settings) != {"model": "opus"} and time.monotonic() < deadline:
        time.sleep(0.05)

    assert _read(settings) == {"model": "opus"}


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HOME": "/home/user"}, "/home/user/.claude/settings.json"),
        ({"HOME": "/home/user", "CLAUDE_CONFIG_DIR": "/srv/claude"}, "/srv/claude/settings.json"),
        ({"HOME": "/home/user", "CLAUDE_CONFIG_DIR": "cfg/../conf"}, "/repo/conf/settings.json"),
    ],
    ids=["home", "config dir", "relative config dir"],
)
def test_settings_path_follows_claudes_config_dir(env: dict[str, str], expected: str) -> None:
    assert claude_user_settings_path(Path("/repo"), env) == Path(expected)


def test_a_bridge_records_the_settings_file_of_its_launch_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setattr(bridge, "claude_user_settings_path", claude_user_settings_path)

    bridge_dir = bridge.prepare_bridge_dir(
        "conv_abc",
        workspace=tmp_path,
        launch_env={"CLAUDE_CONFIG_DIR": "/srv/claude"},
    )

    assert bridge.read_user_settings_path(bridge_dir) == Path("/srv/claude/settings.json")


def test_a_bridge_from_before_the_record_uses_the_runners_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "bridge.json").write_text('{"bridge_id": "b1"}', encoding="utf-8")
    monkeypatch.setattr(bridge, "claude_user_settings_path", claude_user_settings_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/srv/claude")

    assert bridge.read_user_settings_path(bridge_dir) == Path("/srv/claude/settings.json")
