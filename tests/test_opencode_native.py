"""Unit tests for the ``omni opencode`` launcher helpers (``opencode_native.py``).

Covers the pure spec/payload/tmux helpers plus the httpx-backed session and
terminal helpers over a fake ``AsyncClient`` — the daemon/tmux attach plumbing
itself stays for the live host e2e.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import httpx
import pytest
import yaml

from omnigent.opencode_native import (
    LaunchedOpenCodeTerminal,
    PreparedOpenCodeTerminal,
    _create_opencode_session,
    _direct_tmux_unavailable_reason,
    _ensure_opencode_terminal_on_runner,
    _fetch_opencode_session,
    _find_running_opencode_terminal,
    _launched_opencode_terminal_from_payload,
    _materialize_opencode_agent_spec,
    _resolve_session_id_for_resume,
    opencode_terminal_resource_id,
)


class _FakeClient:
    """Async httpx stand-in returning one preset response per call."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append(("POST", url, kwargs))
        return self._response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append(("GET", url, kwargs))
        return self._response


# ── _materialize_opencode_agent_spec ────────────────────────────────────────


def test_materialize_spec_defaults_no_model(tmp_path: Path, monkeypatch) -> None:
    # Pin the host shells so the declared terminals are deterministic.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    spec = yaml.safe_load(_materialize_opencode_agent_spec(tmp_path).read_text())
    assert spec["executor"] == {"harness": "opencode-native"}
    assert spec["spawn"] is True
    # One terminal per installed shell, $SHELL first — a non-empty block gates
    # the relay's sys_terminal_* advertisement.
    assert list(spec["terminals"]) == ["zsh", "bash", "fish"]
    assert spec["terminals"]["zsh"]["command"] == "zsh"


def test_materialize_spec_pins_model(tmp_path: Path) -> None:
    spec = yaml.safe_load(
        _materialize_opencode_agent_spec(tmp_path, model="anthropic/claude-opus-4").read_text()
    )
    assert spec["executor"] == {"harness": "opencode-native", "model": "anthropic/claude-opus-4"}


def test_terminal_resource_id_is_deterministic() -> None:
    assert opencode_terminal_resource_id() == opencode_terminal_resource_id()


# ── _launched_opencode_terminal_from_payload ────────────────────────────────


def test_launched_terminal_parses_tmux_metadata() -> None:
    launched = _launched_opencode_terminal_from_payload(
        {"id": "term_1", "metadata": {"tmux_socket": "/tmp/s.sock", "tmux_target": "sess:0.0"}}
    )
    assert launched.terminal_id == "term_1"
    assert launched.tmux_socket == Path("/tmp/s.sock")
    assert launched.tmux_target == "sess:0.0"


def test_launched_terminal_without_metadata_has_no_tmux() -> None:
    launched = _launched_opencode_terminal_from_payload({"id": "term_1"})
    assert launched.tmux_socket is None and launched.tmux_target is None


def test_launched_terminal_missing_id_raises() -> None:
    with pytest.raises(click.ClickException):
        _launched_opencode_terminal_from_payload({"metadata": {}})
    with pytest.raises(click.ClickException):
        _launched_opencode_terminal_from_payload("not-a-dict")


# ── _direct_tmux_unavailable_reason ─────────────────────────────────────────


def _prepared(socket: Path | None, target: str | None) -> PreparedOpenCodeTerminal:
    return PreparedOpenCodeTerminal(
        session_id="conv_1",
        terminal_id="term_1",
        tmux_socket=socket,
        tmux_target=target,
        reattached=False,
    )


def test_tmux_reason_missing_socket() -> None:
    assert "tmux socket" in (_direct_tmux_unavailable_reason(_prepared(None, "t")) or "")


def test_tmux_reason_missing_target() -> None:
    assert "tmux target" in (_direct_tmux_unavailable_reason(_prepared(Path("/x"), None)) or "")


def test_tmux_reason_socket_not_reachable(tmp_path: Path) -> None:
    reason = _direct_tmux_unavailable_reason(_prepared(tmp_path / "missing.sock", "t"))
    assert reason is not None and "not reachable" in reason


# ── _resolve_session_id_for_resume ──────────────────────────────────────────


def test_resolve_session_id_passthrough() -> None:
    assert (
        _resolve_session_id_for_resume(
            base_url="http://x", headers={}, session_id="conv_9", resume_picker=False
        )
        == "conv_9"
    )


def test_resolve_session_id_none_without_picker() -> None:
    assert (
        _resolve_session_id_for_resume(
            base_url="http://x", headers={}, session_id=None, resume_picker=False
        )
        is None
    )


# ── httpx-backed session/terminal helpers ───────────────────────────────────


async def test_create_session_returns_id() -> None:
    client = _FakeClient(httpx.Response(200, json={"session_id": "conv_new"}))
    sid = await _create_opencode_session(client, b"bundle", terminal_launch_args=["--foo"])  # type: ignore[arg-type]
    assert sid == "conv_new"
    assert client.requests[0][1] == "/v1/sessions"


async def test_create_session_errors_on_http_failure() -> None:
    client = _FakeClient(httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(click.ClickException):
        await _create_opencode_session(client, b"bundle")  # type: ignore[arg-type]


async def test_create_session_errors_without_session_id() -> None:
    client = _FakeClient(httpx.Response(200, json={}))
    with pytest.raises(click.ClickException):
        await _create_opencode_session(client, b"bundle")  # type: ignore[arg-type]


async def test_fetch_session_returns_payload() -> None:
    client = _FakeClient(httpx.Response(200, json={"id": "conv_1", "title": "t"}))
    assert (await _fetch_opencode_session(client, "conv_1"))["id"] == "conv_1"  # type: ignore[arg-type]


async def test_fetch_session_404_raises() -> None:
    client = _FakeClient(httpx.Response(404, json={"error": "nope"}))
    with pytest.raises(click.ClickException):
        await _fetch_opencode_session(client, "conv_1")  # type: ignore[arg-type]


async def test_ensure_terminal_ok_then_error() -> None:
    ok = _FakeClient(httpx.Response(200, json={}))
    await _ensure_opencode_terminal_on_runner(ok, "conv_1")  # type: ignore[arg-type]
    assert ok.requests[0][0] == "POST"
    bad = _FakeClient(httpx.Response(503, json={"error": "x"}))
    with pytest.raises(click.ClickException):
        await _ensure_opencode_terminal_on_runner(bad, "conv_1")  # type: ignore[arg-type]


async def test_find_terminal_404_returns_none() -> None:
    client = _FakeClient(httpx.Response(404, json={}))
    assert await _find_running_opencode_terminal(client, "conv_1") is None  # type: ignore[arg-type]


async def test_find_terminal_not_running_returns_none() -> None:
    client = _FakeClient(
        httpx.Response(200, json={"id": "term_1", "metadata": {"running": False}})
    )
    assert await _find_running_opencode_terminal(client, "conv_1") is None  # type: ignore[arg-type]


async def test_find_terminal_returns_launched() -> None:
    client = _FakeClient(
        httpx.Response(200, json={"id": "term_1", "metadata": {"tmux_target": "s:0.0"}})
    )
    launched = await _find_running_opencode_terminal(client, "conv_1")  # type: ignore[arg-type]
    assert isinstance(launched, LaunchedOpenCodeTerminal)
    assert launched.tmux_target == "s:0.0"


async def test_find_terminal_offline_runner_returns_none() -> None:
    client = _FakeClient(httpx.Response(409, text="session not bound to a runner"))
    assert await _find_running_opencode_terminal(client, "conv_1") is None  # type: ignore[arg-type]


# ── launcher local-preflight / progress / tmux-reason / wait helpers ─────────


def test_preflight_local_tools_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.opencode_native as on

    monkeypatch.setattr(on.shutil, "which", lambda _x: "/usr/bin/tmux")
    on._preflight_local_tools()  # tmux present → no raise


def test_preflight_local_tools_missing_tmux_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.opencode_native as on

    monkeypatch.setattr(on.shutil, "which", lambda _x: None)
    with pytest.raises(click.ClickException):
        on._preflight_local_tools()


def test_update_startup_progress_handles_none_and_active() -> None:
    from unittest.mock import Mock

    from omnigent.opencode_native import _update_startup_progress

    _update_startup_progress(None, "boot")  # no renderer → no-op branch
    _update_startup_progress(Mock(), "boot")  # active renderer → update branch


def test_tmux_reason_tmux_not_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import omnigent.opencode_native as on

    sock = tmp_path / "s.sock"
    sock.write_text("")
    monkeypatch.setattr(on.shutil, "which", lambda _x: None)
    reason = on._direct_tmux_unavailable_reason(_prepared(sock, "t"))
    assert reason is not None and "tmux is not available" in reason


def test_tmux_reason_none_when_socket_and_tmux_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.opencode_native as on

    sock = tmp_path / "s.sock"
    sock.write_text("")
    monkeypatch.setattr(on.shutil, "which", lambda _x: "/usr/bin/tmux")
    assert on._direct_tmux_unavailable_reason(_prepared(sock, "t")) is None


async def test_wait_for_terminal_returns_when_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.opencode_native as on

    term = on.LaunchedOpenCodeTerminal(terminal_id="t", tmux_socket=None, tmux_target=None)

    async def _fake_find(_client: object, _sid: str) -> on.LaunchedOpenCodeTerminal:
        return term

    monkeypatch.setattr(on, "_find_running_opencode_terminal", _fake_find)
    got = await on._wait_for_opencode_terminal_ready(object(), "conv_1", timeout_s=5)  # type: ignore[arg-type]
    assert got is term


async def test_wait_for_terminal_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.opencode_native as on

    async def _never(_client: object, _sid: str) -> None:
        return None

    monkeypatch.setattr(on, "_find_running_opencode_terminal", _never)
    with pytest.raises(click.ClickException):
        await on._wait_for_opencode_terminal_ready(object(), "conv_1", timeout_s=0)  # type: ignore[arg-type]


# --- Resume workspace alignment (launch.json record + cwd realign) ---
def test_record_launch_for_fresh_session_persists_current_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A fresh session records its launch cwd for later resumes.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    import omnigent.opencode_native as on
    from omnigent.opencode_native_state import read_launch_state

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))

    on._record_launch_for_fresh_session("conv_abc")

    state = read_launch_state("conv_abc")
    assert state is not None
    assert state.working_directory == str(workspace.resolve())


def test_record_launch_for_fresh_session_swallows_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A failed launch-state write warns rather than breaking the launch.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace.
    :returns: None.
    """
    import omnigent.opencode_native as on

    monkeypatch.chdir(tmp_path)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(on, "write_launch_state", _raise)

    # Best-effort recording: a write failure must not propagate.
    on._record_launch_for_fresh_session("conv_abc")


def test_align_no_recorded_state_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Resume with no recorded launch state neither prompts nor moves cwd.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    import omnigent.opencode_native as on

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))

    def _fail_prompt(**_kwargs: object) -> str:
        raise AssertionError("absent launch state should not prompt")

    monkeypatch.setattr(on, "_prompt_opencode_resume_workspace_action", _fail_prompt)

    on._align_working_directory_with_session("conv_missing")

    assert Path.cwd().resolve() == tmp_path.resolve()


def test_align_matching_cwd_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Resume from the recorded cwd must not prompt or move cwd.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    import omnigent.opencode_native as on
    from omnigent.opencode_native_state import write_launch_state

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(tmp_path.resolve()))

    def _fail_prompt(**_kwargs: object) -> str:
        raise AssertionError("matching cwd should not prompt")

    monkeypatch.setattr(on, "_prompt_opencode_resume_workspace_action", _fail_prompt)

    on._align_working_directory_with_session("conv_abc")

    assert Path.cwd().resolve() == tmp_path.resolve()


def test_align_switches_to_recorded_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Choosing ``switch`` changes cwd to the recorded directory.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    import omnigent.opencode_native as on
    from omnigent.opencode_native_state import write_launch_state

    recorded = tmp_path / "recorded"
    current = tmp_path / "current"
    recorded.mkdir()
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(recorded.resolve()))
    monkeypatch.setattr(
        on,
        "_prompt_opencode_resume_workspace_action",
        lambda **_kwargs: "switch",
    )

    on._align_working_directory_with_session("conv_abc")

    assert Path.cwd().resolve() == recorded.resolve()


def test_align_cancel_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Choosing ``cancel`` aborts the resume without moving cwd.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    import omnigent.opencode_native as on
    from omnigent.opencode_native_state import write_launch_state

    recorded = tmp_path / "recorded"
    current = tmp_path / "current"
    recorded.mkdir()
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(recorded.resolve()))
    monkeypatch.setattr(
        on,
        "_prompt_opencode_resume_workspace_action",
        lambda **_kwargs: "cancel",
    )

    with pytest.raises(click.ClickException) as excinfo:
        on._align_working_directory_with_session("conv_abc")

    assert "cancel" in excinfo.value.message.lower()
    assert Path.cwd().resolve() == current.resolve()


def test_align_missing_recorded_cwd_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A recorded-but-missing cwd fails loud instead of resuming wrong.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    import omnigent.opencode_native as on
    from omnigent.opencode_native_state import write_launch_state

    current = tmp_path / "current"
    missing = tmp_path / "missing"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("OMNIGENT_OPENCODE_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(missing))

    def _fail_prompt(**_kwargs: object) -> str:
        raise AssertionError("missing recorded dir should raise before prompting")

    monkeypatch.setattr(on, "_prompt_opencode_resume_workspace_action", _fail_prompt)

    with pytest.raises(click.ClickException) as excinfo:
        on._align_working_directory_with_session("conv_abc")

    assert "conv_abc" in excinfo.value.message
    assert str(missing.resolve()) in excinfo.value.message


def test_prompt_offers_switch_and_cancel_choices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The cwd-mismatch prompt offers ``switch``/``cancel`` and defaults to switch.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary recorded/current paths.
    :returns: None.
    """
    import omnigent.opencode_native as on

    captured: dict[str, Any] = {}

    def _fake_prompt(text: str, **kwargs: Any) -> str:
        captured["text"] = text
        captured["kwargs"] = kwargs
        return "switch"

    monkeypatch.setattr(click, "prompt", _fake_prompt)

    result = on._prompt_opencode_resume_workspace_action(
        recorded_path=tmp_path / "recorded",
        current=tmp_path / "current",
    )

    assert result == "switch"
    choice = captured["kwargs"]["type"]
    assert isinstance(choice, click.Choice)
    assert list(choice.choices) == ["switch", "cancel"]
    assert captured["kwargs"]["default"] == "switch"


# --- _run_with_remote_server control-flow (daemon/server mocked) ---
def test_run_with_remote_server_aligns_cwd_before_daemon_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Remote resume aligns cwd before the daemon prepare samples it.

    Drives the real ``_run_with_remote_server`` with the daemon/server
    mocked: asserts ``align`` runs first and the aligned cwd is the
    workspace handed to prepare.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary start/aligned dirs.
    :returns: None.
    """
    import os
    from types import SimpleNamespace

    import omnigent.chat as chat_mod
    import omnigent.cli as cli_mod
    import omnigent.host.identity as identity_mod
    import omnigent.opencode_native as on
    from omnigent._runner_startup import RunnerStartupProgress

    start_dir = tmp_path / "start"
    aligned_dir = tmp_path / "aligned"
    start_dir.mkdir()
    aligned_dir.mkdir()
    monkeypatch.chdir(start_dir)

    order: list[str] = []

    def fake_align(_session_id: str) -> None:
        order.append("align")
        os.chdir(aligned_dir)

    async def fake_prepare(**kwargs: Any) -> PreparedOpenCodeTerminal:
        assert kwargs["host_id"] == "host_local"
        assert kwargs["session_id"] == "conv_abc"
        assert kwargs["workspace"] == str(aligned_dir.resolve())
        assert isinstance(kwargs["startup_progress"], RunnerStartupProgress)
        order.append("prepare")
        return PreparedOpenCodeTerminal(
            session_id="conv_abc",
            terminal_id="term_main",
            tmux_socket=None,
            tmux_target=None,
            reattached=True,
        )

    async def fake_attach(_prepared: object) -> None:
        order.append("attach")

    monkeypatch.setattr(chat_mod, "_remote_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(
        cli_mod, "_ensure_host_daemon", lambda *_a, **_k: order.append("ensure-daemon")
    )
    monkeypatch.setattr(
        identity_mod,
        "load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    monkeypatch.setattr(on, "_resolve_session_id_for_resume", lambda **_k: "conv_abc")
    monkeypatch.setattr(on, "_align_working_directory_with_session", fake_align)
    monkeypatch.setattr(on, "_prepare_opencode_terminal_via_daemon", fake_prepare)
    monkeypatch.setattr(on, "_attach_terminal_resource", fake_attach)
    monkeypatch.setattr(on, "open_conversation_link_if_enabled", lambda **_k: None)

    on._run_with_remote_server(
        "http://server",
        tmp_path / "spec.yaml",
        session_id="conv_abc",
        resume_picker=False,
        opencode_args=(),
    )

    assert order == ["align", "ensure-daemon", "prepare", "attach"]


def test_run_with_remote_server_records_launch_after_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A fresh remote session records its launch cwd after prepare (no align).

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace.
    :returns: None.
    """
    from types import SimpleNamespace

    import omnigent.chat as chat_mod
    import omnigent.cli as cli_mod
    import omnigent.host.identity as identity_mod
    import omnigent.opencode_native as on

    monkeypatch.chdir(tmp_path)
    order: list[str] = []

    async def fake_prepare(**_k: Any) -> PreparedOpenCodeTerminal:
        order.append("prepare")
        return PreparedOpenCodeTerminal(
            session_id="conv_new",
            terminal_id="term_main",
            tmux_socket=None,
            tmux_target=None,
            reattached=False,
        )

    async def fake_attach(_prepared: object) -> None:
        order.append("attach")

    def fake_record(session_id: str) -> None:
        assert session_id == "conv_new"
        order.append("record")

    def fail_align(_session_id: str) -> None:
        raise AssertionError("create path must not align cwd")

    monkeypatch.setattr(chat_mod, "_remote_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(chat_mod, "_bundle_agent", lambda _spec: b"bundle")
    monkeypatch.setattr(
        cli_mod, "_ensure_host_daemon", lambda *_a, **_k: order.append("ensure-daemon")
    )
    monkeypatch.setattr(
        identity_mod,
        "load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    monkeypatch.setattr(on, "_resolve_session_id_for_resume", lambda **_k: None)
    monkeypatch.setattr(on, "_align_working_directory_with_session", fail_align)
    monkeypatch.setattr(on, "_prepare_opencode_terminal_via_daemon", fake_prepare)
    monkeypatch.setattr(on, "_attach_terminal_resource", fake_attach)
    monkeypatch.setattr(on, "_record_launch_for_fresh_session", fake_record)
    monkeypatch.setattr(on, "open_conversation_link_if_enabled", lambda **_k: None)
    monkeypatch.setattr(on, "echo_native_resume_hint", lambda **_k: None)

    on._run_with_remote_server(
        "http://server",
        tmp_path / "spec.yaml",
        session_id=None,
        resume_picker=False,
        opencode_args=(),
    )

    assert order == ["ensure-daemon", "prepare", "record", "attach"]
