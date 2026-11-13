"""Tests for keeping Omnigent's ``/model`` switches out of the user's default model."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge, default_model_guard
from omnigent.harnesses.claude_native.bridge import claude_user_settings_path


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    """Settings and state paths; the adapter fixture disables background watching."""
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


@pytest.mark.parametrize("edit_at", ["first read", "retry"])
@pytest.mark.parametrize("edited_model", ["sonnet", "haiku"])
def test_an_edit_landing_mid_restore_is_kept(
    paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    edit_at: str,
    edited_model: str,
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    default_model_guard.begin_model_switch(settings, "later-model", state_dir=state)
    _write(settings, {"model": "sonnet"})
    real_chmod = default_model_guard.os.chmod
    real_restore = default_model_guard._write_settings_model

    def edit_settings() -> None:
        if _read(settings) == {"model": "sonnet"}:
            _write(settings, {"model": edited_model, "theme": "dark"})

    def edit_then_restore(path: Path, value: object, *, expected_model: str) -> object:
        if edit_at == "first read":
            edit_settings()
        return real_restore(path, value, expected_model=expected_model)

    def chmod_then_edit(path: object, mode: int) -> None:
        real_chmod(path, mode)
        if edit_at == "retry":
            edit_settings()

    monkeypatch.setattr(default_model_guard.os, "chmod", chmod_then_edit)
    monkeypatch.setattr(default_model_guard, "_write_settings_model", edit_then_restore)

    default_model_guard.settle_default_model(settings, state_dir=state)

    expected_model = "opus" if edited_model == "sonnet" else edited_model
    assert _read(settings) == {"model": expected_model, "theme": "dark"}
    _write(settings, {"model": "later-model", "theme": "dark"})
    assert not default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": expected_model, "theme": "dark"}


def test_keeps_a_users_own_change_and_restores_to_it(paths: tuple[Path, Path]) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)

    _write(settings, {"model": "haiku"})  # the user picks a new default meanwhile
    assert default_model_guard.begin_model_switch(settings, "haiku", state_dir=state) is None
    assert default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "haiku"}

    _write(settings, {"model": "sonnet"})  # the queued switch lands later
    default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "haiku"}


def test_a_second_session_does_not_take_an_unsettled_save_for_the_default(
    paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    first_root, second_root = settings.parent / "temp-one", settings.parent / "temp-two"
    monkeypatch.setattr(bridge, "read_user_settings_path", lambda _bridge: settings)
    monkeypatch.setenv("TMPDIR", str(first_root))
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT_PARENT", first_root)
    first = bridge._begin_model_switch(first_root / "bridge", "/model sonnet")
    assert first is not None
    _write(settings, {"model": "sonnet"})  # the first session's save, not settled yet

    monkeypatch.setenv("TMPDIR", str(second_root))
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT_PARENT", second_root)
    second = bridge._begin_model_switch(second_root / "bridge", "/model haiku")
    assert second is not None
    assert first.state_dir == second.state_dir == default_model_guard.state_directory(settings)
    state = first.state_dir
    _write(settings, {"model": "haiku"})
    default_model_guard.settle_default_model(settings, state_dir=state)

    assert _read(settings) == {"model": "opus"}


def test_registration_respects_another_process_holding_the_shared_lock(
    paths: tuple[Path, Path],
) -> None:
    """A second process cannot register until the shared record lock is released."""
    settings, state = paths
    _write(settings, {"model": "opus"})
    program = textwrap.dedent(
        """\
        import sys
        from pathlib import Path
        from omnigent.harnesses.claude_native import default_model_guard as guard

        # This child does not inherit pytest's watcher-isolation fixture.
        guard._ensure_watcher = lambda *_args: None
        guard._LOCK_TIMEOUT_S = 0.1
        try:
            guard.begin_model_switch(Path(sys.argv[1]), "sonnet", state_dir=Path(sys.argv[2]))
        except guard.ModelSwitchProtectionError:
            print("blocked")
        else:
            print("registered")
        """
    )

    def register_in_child() -> str:
        result = subprocess.run(
            [sys.executable, "-c", program, str(settings), str(state)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip()

    with default_model_guard._locked_record(state, settings.resolve()):
        assert register_in_child() == "blocked"
        assert not list(state.glob("*.json"))
    assert register_in_child() == "registered"
    _write(settings, {"model": "sonnet"})
    assert not default_model_guard.settle_default_model(settings, state_dir=state)
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


@pytest.mark.parametrize("already_settled", [False, True], ids=["both-pending", "settled"])
def test_cancelling_forgets_only_that_switch(
    paths: tuple[Path, Path], already_settled: bool
) -> None:
    settings, state = paths
    _write(settings, {"model": "opus"})
    sonnet = default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)
    haiku = default_model_guard.begin_model_switch(settings, "haiku", state_dir=state)
    assert sonnet is not None and haiku is not None
    if already_settled:
        _write(settings, {"model": "sonnet"})
        default_model_guard.settle_default_model(settings, state_dir=state)

    sonnet.cancel()
    _write(settings, {"model": "haiku"})

    assert not default_model_guard.settle_default_model(settings, state_dir=state)
    assert _read(settings) == {"model": "opus"}

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


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_the_watcher_restores_the_default_on_its_own(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_claude_default_model_guard: Callable[[Path, Path], None],
    failure: type[Exception],
) -> None:
    settings, state = tmp_path / "settings.json", tmp_path / "state"
    _write(settings, {"model": "opus"})
    real_settle = default_model_guard.settle_default_model
    calls: list[Path] = []

    def settle_failing_once(path: Path, *, state_dir: Path) -> bool:
        calls.append(path)
        if len(calls) == 1:
            raise failure("record briefly unreadable")
        return real_settle(path, state_dir=state_dir)

    monkeypatch.setattr(default_model_guard, "settle_default_model", settle_failing_once)
    monkeypatch.setattr(
        default_model_guard, "_ensure_watcher", _isolate_claude_default_model_guard
    )
    default_model_guard.begin_model_switch(settings, "sonnet", state_dir=state)

    _write(settings, {"model": "sonnet"})
    deadline = time.monotonic() + 5
    while _read(settings) != {"model": "opus"} and time.monotonic() < deadline:
        time.sleep(0.05)

    assert _read(settings) == {"model": "opus"}
    with default_model_guard._watchers_lock:
        watcher = default_model_guard._watchers.get((settings, state))
    if watcher is not None:
        watcher.join(timeout=5)
        assert not watcher.is_alive()


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
