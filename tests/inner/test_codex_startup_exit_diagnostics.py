"""An app-server that exits during startup must say why, not only that stdout closed."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from omnigent.errors import HarnessTransportClosedError
from omnigent.inner.codex_executor import CodexExecutor

_PROVIDER = "example-provider"
_APP_SERVER_ERROR = f"Error: Model provider `{_PROVIDER}` not found"
_CONFIG_ERROR = "Error: unknown variant `example` for key `example_key` in config.toml"


def _write_scripted_codex(tmp_path: Path, *, mode: str) -> Path:
    """Write a fake ``codex`` that records its argv to ``calls.jsonl``.

    ``mode="config_error"``: ``app-server`` prints a routine warning and the
    generic fallback error, then exits 1 before answering ``initialize``;
    ``debug models`` exits 1 with the underlying config error.
    ``mode="config_ok"``: as above, but ``debug models`` succeeds.
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
            import sys

            with open({str(calls)!r}, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(sys.argv[1:]) + "\\n")
            mode = {mode!r}
            if "--version" in sys.argv:
                print("codex-cli 0.0.0-scripted")
                raise SystemExit(0)
            if sys.argv[1:3] == ["debug", "models"]:
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


def _calls(tmp_path: Path) -> list[list[str]]:
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


async def _run_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str) -> str:
    """Drive one real ``CodexExecutor`` turn and return its transport error."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "src-codex-home"))
    executor = CodexExecutor(
        cwd=str(tmp_path),
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
    return str(raised.value)


@pytest.mark.timeout(120)
async def test_startup_exit_reports_exit_code_stderr_and_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = await _run_turn(tmp_path, monkeypatch, mode="config_error")

    assert message == (
        "Codex app-server closed stdout; exited with code 1 during startup; "
        f"stderr: {_APP_SERVER_ERROR}; codex config check: {_CONFIG_ERROR}"
    )
    # The check must load the config the way the app-server did.
    calls = _calls(tmp_path)
    app_server = next(call for call in calls if call[0] == "app-server")
    check = next(call for call in calls if call[:2] == ["debug", "models"])
    assert f'model_provider="{_PROVIDER}"' in app_server
    assert check[2:] == app_server[1:]


@pytest.mark.timeout(120)
async def test_startup_exit_omits_a_passing_config_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = await _run_turn(tmp_path, monkeypatch, mode="config_ok")

    assert message == (
        f"Codex app-server closed stdout; exited with code 1 during startup; "
        f"stderr: {_APP_SERVER_ERROR}"
    )


@pytest.mark.timeout(120)
async def test_exit_after_startup_keeps_the_plain_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = await _run_turn(tmp_path, monkeypatch, mode="mid_session")

    assert message == "Codex app-server closed stdout"
    assert not any(call[:2] == ["debug", "models"] and "-c" in call for call in _calls(tmp_path))
