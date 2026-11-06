"""An app-server that exits during startup must say why, not only that stdout closed."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from omnigent.errors import HarnessTransportClosedError
from omnigent.inner import codex_executor
from omnigent.inner.codex_executor import (
    CodexExecutor,
    _codex_config_check_error,
    _startup_config_check,
)

_PROVIDER = "example-provider"
_APP_SERVER_ERROR = f"Error: Model provider `{_PROVIDER}` not found"
_CONFIG_ERROR = "Error: unknown variant `example` for key `example_key` in config.toml"


def _write_scripted_codex(tmp_path: Path, *, mode: str) -> Path:
    """Write a fake ``codex`` that records each launch to ``calls.jsonl``.

    ``mode="config_error"``: ``app-server`` prints a routine warning and the
    generic fallback error, then exits 1 before answering ``initialize``;
    ``debug models`` exits 1 with the underlying config error.
    ``mode="config_ok"``: as above, but ``debug models`` succeeds.
    ``mode="check_hangs"``: ``debug models`` writes its pid to ``check.pid``
    and sleeps.
    ``mode="mid_session"``: ``app-server`` answers ``initialize`` and
    ``thread/start``, then exits on ``turn/start`` without replying.
    """
    calls = tmp_path / "calls.jsonl"
    script = tmp_path / "codex"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import sys
            import time

            with open({str(calls)!r}, "a", encoding="utf-8") as handle:
                record = {{
                    "argv": sys.argv[1:],
                    "cwd": os.getcwd(),
                    "codex_home": os.environ.get("CODEX_HOME"),
                }}
                handle.write(json.dumps(record) + "\\n")
            mode = {mode!r}
            if "--version" in sys.argv:
                print("codex-cli 0.0.0-scripted")
                raise SystemExit(0)
            if sys.argv[1:3] == ["debug", "models"]:
                if mode == "check_hangs":
                    with open({str(tmp_path / "check.pid")!r}, "w") as handle:
                        handle.write(str(os.getpid()))
                    time.sleep(60)
                if mode == "config_error":
                    sys.stderr.write({_CONFIG_ERROR!r} + "\\n")
                    raise SystemExit(1)
                print(json.dumps({{"models": [{{"slug": "scripted-model"}}]}}))
                raise SystemExit(0)
            if len(sys.argv) < 2 or sys.argv[1] != "app-server":
                raise SystemExit(2)
            if mode != "mid_session":
                sys.stderr.write("WARNING: proceeding without helper aliases\\n")
                sys.stderr.write({_APP_SERVER_ERROR!r} + "\\n")
                raise SystemExit(1)
            for line in sys.stdin:
                if not line.strip():
                    continue
                request = json.loads(line)
                method = request.get("method")
                if method == "turn/start":
                    raise SystemExit(0)
                if method == "initialize":
                    result = {{}}
                elif method == "thread/start":
                    result = {{"thread": {{"id": "thread-1"}}}}
                else:
                    result = {{}}
                sys.stdout.write(json.dumps({{"id": request["id"], "result": result}}) + "\\n")
                sys.stdout.flush()
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _calls(tmp_path: Path) -> list[dict[str, object]]:
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


async def _run_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str
) -> HarnessTransportClosedError:
    """Drive one real ``CodexExecutor`` turn and return its transport error."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "src-codex-home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = CodexExecutor(
        cwd=str(workspace),
        codex_path=str(_write_scripted_codex(tmp_path, mode=mode)),
        model_provider_override=_PROVIDER,
        enable_web_search=False,
    )

    async def _collect() -> None:
        async for _event in executor.run_turn(
            [{"role": "user", "content": "hi", "session_id": "startup-exit"}], [], ""
        ):
            pass

    try:
        with pytest.raises(HarnessTransportClosedError) as raised:
            await asyncio.wait_for(_collect(), timeout=30)
    finally:
        await asyncio.wait_for(executor.close(), timeout=30)
    return raised.value


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.timeout(120)
async def test_startup_exit_reports_exit_code_stderr_and_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = await _run_turn(tmp_path, monkeypatch, mode="config_error")

    assert str(error) == (
        "Codex app-server closed stdout; exited with code 1 during startup; "
        f"stderr: {_APP_SERVER_ERROR}; codex config check: {_CONFIG_ERROR}"
    )
    assert error.replay_safe is True
    # The check must load the config the way the app-server did.
    calls = _calls(tmp_path)
    app_server = next(call for call in calls if call["argv"][0] == "app-server")
    check = next(call for call in calls if call["argv"][:2] == ["debug", "models"])
    assert f'model_provider="{_PROVIDER}"' in app_server["argv"]
    assert check["argv"][2:] == app_server["argv"][1:]
    assert check["cwd"] == app_server["cwd"] == str(tmp_path / "workspace")
    assert check["codex_home"] == app_server["codex_home"]


@pytest.mark.timeout(120)
async def test_startup_exit_omits_a_passing_config_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = await _run_turn(tmp_path, monkeypatch, mode="config_ok")

    assert str(error) == (
        f"Codex app-server closed stdout; exited with code 1 during startup; "
        f"stderr: {_APP_SERVER_ERROR}"
    )


@pytest.mark.timeout(120)
async def test_failed_config_check_keeps_the_startup_detail_and_replay_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_check(*_args: object, **_kwargs: object) -> str | None:
        raise OSError("diagnostic pipe failed")

    monkeypatch.setattr(codex_executor, "_codex_config_check_error", failing_check)

    error = await _run_turn(tmp_path, monkeypatch, mode="config_error")

    assert str(error) == (
        f"Codex app-server closed stdout; exited with code 1 during startup; "
        f"stderr: {_APP_SERVER_ERROR}"
    )
    assert error.replay_safe is True


@pytest.mark.timeout(120)
async def test_exit_after_startup_keeps_the_plain_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = await _run_turn(tmp_path, monkeypatch, mode="mid_session")

    assert str(error) == "Codex app-server closed stdout"
    assert not any(
        call["argv"][:2] == ["debug", "models"] and "-c" in call["argv"]
        for call in _calls(tmp_path)
    )


@pytest.mark.parametrize("sandboxed, signer_backed", [(True, False), (False, True)])
def test_contained_workers_get_no_out_of_sandbox_config_check(
    sandboxed: bool, signer_backed: bool
) -> None:
    check = _startup_config_check(
        sandboxed=sandboxed,
        signer_backed=signer_backed,
        codex_path="/opt/example/codex",
        config_overrides=['model_provider="example"'],
        env={"CODEX_HOME": "/tmp/example-home"},
        cwd="/tmp/example-workspace",
    )

    assert check is None


def _hanging_check(tmp_path: Path):
    return _startup_config_check(
        sandboxed=False,
        signer_backed=False,
        codex_path=str(_write_scripted_codex(tmp_path, mode="check_hangs")),
        config_overrides=[],
        env={"PATH": os.environ.get("PATH", "")},
        cwd=str(tmp_path),
    )


async def _check_pid(tmp_path: Path) -> int:
    pid_file = tmp_path / "check.pid"
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            return int(pid_file.read_text())
        await asyncio.sleep(0.05)
    pytest.fail("config check never started")


@pytest.mark.timeout(60)
async def test_config_check_timeout_kills_and_reaps_the_check(tmp_path: Path) -> None:
    check = _hanging_check(tmp_path)
    assert check is not None
    task = asyncio.create_task(_codex_config_check_error(check, timeout=1.0))
    pid = await _check_pid(tmp_path)

    assert await task is None
    assert not _pid_alive(pid)


@pytest.mark.timeout(60)
async def test_cancelled_config_check_kills_the_check_and_propagates(tmp_path: Path) -> None:
    check = _hanging_check(tmp_path)
    assert check is not None
    task = asyncio.create_task(_codex_config_check_error(check, timeout=30))
    pid = await _check_pid(tmp_path)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _pid_alive(pid)
