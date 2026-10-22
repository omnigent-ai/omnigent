"""Local sessions tests for Codex session."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest

from omnigent._runner_startup import RunnerStartupProgress
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    write_bridge_state,
)


def test_local_run_prints_resume_hint_after_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Local ``omnigent codex`` prints a copyable resume hint on exit.

    This exercises the run-level call site, not just the formatter:
    a regression that prepares and attaches successfully but forgets
    to echo the final ``--resume`` prompt fails on the captured stderr.
    """
    spec_path = tmp_path / "codex.yaml"
    spec_path.write_text("name: codex-native-ui\nprompt: hi\n", encoding="utf-8")
    opened: list[tuple[str, str, bool]] = []

    class _Proc:
        """Stub for the local server subprocess."""

        def poll(self) -> None:
            """
            Pretend the fake local server is alive.

            :returns: None.
            """

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        """
        Return a minimal server handle without starting a process.

        :param args: Positional startup arguments.
        :param kwargs: Keyword startup arguments.
        :returns: Fake local server handle.
        """
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def fake_prepare(**kwargs: object) -> codex_native.PreparedCodexTerminal:
        """
        Return prepared Codex terminal details without launching Codex.

        :param kwargs: Terminal preparation keyword arguments.
        :returns: Prepared fake terminal.
        """
        del kwargs
        return codex_native.PreparedCodexTerminal(
            session_id="conv_codex_fresh",
            terminal_id=codex_native.codex_terminal_resource_id(),
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=False,
        )

    async def fake_attach_with_forwarder(**kwargs: object) -> None:
        """
        Simulate a completed Codex attach session.

        :param kwargs: Attach keyword arguments.
        :returns: None.
        """
        del kwargs

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 23456)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(codex_native, "_prepare_codex_terminal", fake_prepare)
    monkeypatch.setattr(codex_native, "_attach_with_forwarder", fake_attach_with_forwarder)
    monkeypatch.setattr(
        codex_native,
        "open_conversation_link_if_enabled",
        lambda **kwargs: opened.append(
            (
                kwargs["base_url"],
                kwargs["conversation_id"],
                kwargs["enabled"],
            )
        ),
    )

    codex_native._run_with_local_server(
        spec_path,
        session_id=None,
        resume_picker=False,
        codex_args=(),
        command="codex",
        model=None,
        prompt=None,
        auto_open_conversation=True,
    )

    captured = capsys.readouterr()
    web_ui = "Web UI: http://127.0.0.1:23456/c/conv_codex_fresh"
    resume_hint = "Resume with: omnigent codex --resume conv_codex_fresh"
    assert web_ui in captured.err
    assert resume_hint in captured.err
    assert captured.err.index(web_ui) < captured.err.index(resume_hint)
    assert opened == [("http://127.0.0.1:23456", "conv_codex_fresh", True)]


def test_local_run_resume_hint_follows_native_new_rotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    The resume hint names the session a native ``/new`` rotated into.

    Codex ``/new`` starts a fresh thread and the forwarder rotates
    Omnigent ownership to a new conversation, recording it in bridge
    state. A regression that echoes the launch-time ``prepared`` id
    instead hands the user a command that resumes the session they
    already cleared away from.
    """
    spec_path = tmp_path / "codex.yaml"
    spec_path.write_text("name: codex-native-ui\nprompt: hi\n", encoding="utf-8")
    bridge_dir = tmp_path / "bridge"

    class _Proc:
        """Stub for the local server subprocess."""

        def poll(self) -> None:
            """
            Pretend the fake local server is alive.

            :returns: None.
            """

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        """
        Return a minimal server handle without starting a process.

        :param args: Positional startup arguments.
        :param kwargs: Keyword startup arguments.
        :returns: Fake local server handle.
        """
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def fake_prepare(**kwargs: object) -> codex_native.PreparedCodexTerminal:
        """
        Return prepared Codex terminal details without launching Codex.

        :param kwargs: Terminal preparation keyword arguments.
        :returns: Prepared fake terminal.
        """
        del kwargs
        return codex_native.PreparedCodexTerminal(
            session_id="conv_codex_first",
            terminal_id=codex_native.codex_terminal_resource_id(),
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=bridge_dir,
            thread_id="thread_first",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=False,
        )

    async def fake_attach_with_forwarder(**kwargs: object) -> None:
        """
        Simulate a session where the user ran a native ``/new``.

        :param kwargs: Attach keyword arguments.
        :returns: None.
        """
        del kwargs
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id="conv_codex_rotated",
                socket_path="ws://127.0.0.1:9876",
                thread_id="thread_rotated",
                codex_home=str(bridge_dir / "codex-home"),
            ),
        )

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 23456)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(codex_native, "_prepare_codex_terminal", fake_prepare)
    monkeypatch.setattr(codex_native, "_attach_with_forwarder", fake_attach_with_forwarder)
    monkeypatch.setattr(
        codex_native,
        "open_conversation_link_if_enabled",
        lambda **kwargs: None,
    )

    codex_native._run_with_local_server(
        spec_path,
        session_id=None,
        resume_picker=False,
        codex_args=(),
        command="codex",
        model=None,
        prompt=None,
        auto_open_conversation=False,
    )

    captured = capsys.readouterr()
    assert "Resume with: omnigent codex --resume conv_codex_rotated" in captured.err
    assert "--resume conv_codex_first" not in captured.err


def test_local_resume_does_not_print_redundant_resume_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    ``omnigent codex --resume`` does not echo another resume prompt.

    This prevents the hint from becoming always-on noise after a user
    has already chosen the conversation id to resume.
    """
    spec_path = tmp_path / "codex.yaml"
    spec_path.write_text("name: codex-native-ui\nprompt: hi\n", encoding="utf-8")

    class _Proc:
        """Stub for the local server subprocess."""

        def poll(self) -> None:
            """
            Pretend the fake local server is alive.

            :returns: None.
            """

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        """
        Return a minimal server handle without starting a process.

        :param args: Positional startup arguments.
        :param kwargs: Keyword startup arguments.
        :returns: Fake local server handle.
        """
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def fake_prepare(**kwargs: object) -> codex_native.PreparedCodexTerminal:
        """
        Return prepared Codex terminal details without launching Codex.

        :param kwargs: Terminal preparation keyword arguments.
        :returns: Prepared fake terminal.
        """
        del kwargs
        return codex_native.PreparedCodexTerminal(
            session_id="conv_codex_existing",
            terminal_id=codex_native.codex_terminal_resource_id(),
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=False,
        )

    async def fake_attach_with_forwarder(**kwargs: object) -> None:
        """
        Simulate a completed Codex attach session.

        :param kwargs: Attach keyword arguments.
        :returns: None.
        """
        del kwargs

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 23457)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr(codex_native, "_prepare_codex_terminal", fake_prepare)
    monkeypatch.setattr(codex_native, "_attach_with_forwarder", fake_attach_with_forwarder)

    codex_native._run_with_local_server(
        spec_path,
        session_id="conv_codex_existing",
        resume_picker=False,
        codex_args=(),
        command="codex",
        model=None,
        prompt=None,
    )

    captured = capsys.readouterr()
    assert "Web UI: http://127.0.0.1:23457/c/conv_codex_existing" in captured.err
    assert "Resume with:" not in captured.err


def test_run_codex_native_does_not_require_local_codex_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The wrapper no longer owns the Codex process.

    A missing local ``codex`` binary must not fail preflight because the
    daemon-spawned runner resolves and starts Codex. If the old local
    preflight comes back, this test raises before ``fake_remote`` records
    the command.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    remote_called = False

    def fake_which(command: str) -> str | None:
        """
        Return fake executable paths.

        :param command: Command name passed to ``shutil.which``.
        :returns: Fake absolute path for tmux, otherwise ``None``.
        """
        if command == "tmux":
            return "/usr/bin/tmux"
        return None

    def fake_remote(
        base_url: str,
        spec_path: Path,
        *,
        session_id: str | None,
        resume_picker: bool,
        codex_args: tuple[str, ...],
        model: str | None,
        prompt: str | None,
        auto_open_conversation: bool,
    ) -> None:
        """
        Record that the remote daemon path was selected.

        :returns: None.
        """
        nonlocal remote_called
        del (
            base_url,
            spec_path,
            session_id,
            resume_picker,
            codex_args,
            model,
            prompt,
            auto_open_conversation,
        )
        remote_called = True

    monkeypatch.setattr(codex_native.shutil, "which", fake_which)
    monkeypatch.setattr(codex_native, "_run_with_remote_server", fake_remote)

    codex_native.run_codex_native(
        server="http://localhost:8000",
        session_id=None,
        codex_args=(),
        command="codex",
    )

    assert remote_called is True


def test_record_launch_for_fresh_session_persists_current_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh Codex sessions record the cwd used for future resumes.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.harnesses.codex_native.state import read_launch_state

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))

    codex_native._record_launch_for_fresh_session("conv_abc")

    state = read_launch_state("conv_abc")
    assert state is not None
    assert state.working_directory == str(workspace.resolve())


def test_align_working_directory_with_session_matching_cwd_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Resume from the recorded cwd must not prompt or mutate cwd.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.harnesses.codex_native.state import write_launch_state

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(tmp_path.resolve()))

    def fail_prompt(**_kwargs: object) -> str:
        """
        Fail if a prompt is shown for a matching cwd.

        :returns: Never returns.
        """
        raise AssertionError("matching cwd should not prompt")

    monkeypatch.setattr(codex_native, "_prompt_codex_resume_workspace_action", fail_prompt)

    codex_native._align_working_directory_with_session("conv_abc")

    assert Path.cwd().resolve() == tmp_path.resolve()


def test_align_working_directory_with_session_switches_to_recorded_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Choosing ``switch`` changes cwd before Codex resume continues.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.harnesses.codex_native.state import write_launch_state

    recorded = tmp_path / "recorded"
    current = tmp_path / "current"
    recorded.mkdir()
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(recorded.resolve()))
    monkeypatch.setattr(
        codex_native,
        "_prompt_codex_resume_workspace_action",
        lambda **_kwargs: "switch",
    )

    codex_native._align_working_directory_with_session("conv_abc")

    assert Path.cwd().resolve() == recorded.resolve()


def test_align_working_directory_with_session_missing_recorded_cwd_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Recorded-but-missing cwd fails loud instead of starting Codex wrong.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.harnesses.codex_native.state import write_launch_state

    current = tmp_path / "current"
    missing = tmp_path / "missing"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(missing))

    with pytest.raises(click.ClickException) as excinfo:
        codex_native._align_working_directory_with_session("conv_abc")

    assert "conv_abc" in excinfo.value.message
    assert str(missing) in excinfo.value.message


def test_codex_resume_workspace_options_name_cancel_action(tmp_path: Path) -> None:
    """
    The cwd mismatch prompt names cancellation explicitly.

    A generic ``leave`` option is ambiguous because Codex does not
    continue from the current cwd; the caller cancels resume.

    :param tmp_path: Temporary recorded workspace path.
    :returns: None.
    """
    options = codex_native._codex_resume_workspace_action_options(
        recorded_path=tmp_path,
    )

    assert [(option.action, option.label) for option in options] == [
        ("switch", f"Switch working directory to {tmp_path}"),
        ("cancel", "Cancel resume"),
    ]


def test_run_with_remote_server_aligns_cwd_before_daemon_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Remote resume aligns cwd before daemon runner preparation samples it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary paths for prepared Codex details.
    :returns: None.
    """
    import omnigent.chat as chat_mod
    import omnigent.cli as cli_mod
    import omnigent.host.identity as identity_mod

    order: list[str] = []

    def fake_ensure_host_daemon(*_args: object, **_kwargs: object) -> None:
        """
        Record daemon startup order.

        :returns: None.
        """
        order.append("ensure-daemon")

    async def fake_prepare_codex_terminal_via_daemon(
        **kwargs: object,
    ) -> codex_native.PreparedCodexTerminal:
        """
        Record terminal preparation order.

        :param kwargs: Preparation keyword arguments.
        :returns: Prepared reattached Codex terminal.
        """
        assert kwargs["host_id"] == "host_local"
        assert kwargs["session_id"] == "conv_abc"
        assert kwargs["workspace"] == str(aligned_dir.resolve())
        assert isinstance(kwargs["startup_progress"], RunnerStartupProgress)
        order.append("prepare")
        return codex_native.PreparedCodexTerminal(
            session_id="conv_abc",
            terminal_id="terminal_codex_main",
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=True,
        )

    async def fake_attach_terminal_resource(**_kwargs: object) -> None:
        """
        Record attach order.

        :returns: None.
        """
        order.append("attach")

    monkeypatch.setattr(chat_mod, "_remote_headers", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chat_mod, "_server_auth", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_mod, "_ensure_host_daemon", fake_ensure_host_daemon)
    monkeypatch.setattr(
        identity_mod,
        "load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    start_dir = tmp_path / "start"
    aligned_dir = tmp_path / "aligned"
    start_dir.mkdir()
    aligned_dir.mkdir()
    monkeypatch.chdir(start_dir)

    monkeypatch.setattr(
        codex_native,
        "_resolve_session_id_for_resume",
        lambda **_kwargs: "conv_abc",
    )

    def fake_align_working_directory(_session_id: str) -> None:
        """
        Simulate resume alignment changing the process cwd.

        :param _session_id: Session id being aligned.
        :returns: None.
        """
        order.append("align")
        os.chdir(aligned_dir)

    monkeypatch.setattr(
        codex_native,
        "_align_working_directory_with_session",
        fake_align_working_directory,
    )
    monkeypatch.setattr(
        codex_native,
        "_prepare_codex_terminal_via_daemon",
        fake_prepare_codex_terminal_via_daemon,
    )
    monkeypatch.setattr(codex_native, "_attach_terminal_resource", fake_attach_terminal_resource)

    codex_native._run_with_remote_server(
        "https://example.com",
        tmp_path / "codex.yaml",
        session_id="conv_abc",
        resume_picker=False,
        codex_args=(),
        model=None,
        prompt=None,
    )

    assert order == ["align", "ensure-daemon", "prepare", "attach"]


def test_run_with_local_server_records_fresh_session_before_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Local fresh sessions persist launch cwd before terminal attach.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary paths for fake server and Codex details.
    :returns: None.
    """
    import omnigent.chat as chat_mod

    order: list[str] = []

    class _FakeServer:
        """
        Minimal local server handle.

        :param proc: Fake server process.
        :param runner_id: Stable runner id.
        """

        proc = object()
        runner_id = "runner_local"

    async def fake_prepare_codex_terminal(**_kwargs: object) -> codex_native.PreparedCodexTerminal:
        """
        Return a freshly prepared Codex terminal.

        :returns: Prepared Codex terminal.
        """
        order.append("prepare")
        return codex_native.PreparedCodexTerminal(
            session_id="conv_fresh",
            terminal_id="terminal_codex_main",
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=False,
        )

    async def fake_attach_with_forwarder(**_kwargs: object) -> None:
        """
        Record attach after launch-state recording.

        :returns: None.
        """
        order.append("attach")

    monkeypatch.setattr(chat_mod, "_find_free_port", lambda: 9876)
    monkeypatch.setattr(chat_mod, "_start_local_server", lambda *_args, **_kwargs: _FakeServer())
    monkeypatch.setattr(chat_mod, "_wait_for_server", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_mod, "_stop_local_server", lambda _server: None)
    monkeypatch.setattr(chat_mod, "_bundle_agent", lambda _path: b"bundle")
    monkeypatch.setattr(codex_native, "_resolve_session_id_for_resume", lambda **_kwargs: None)
    monkeypatch.setattr(codex_native, "_prepare_codex_terminal", fake_prepare_codex_terminal)
    monkeypatch.setattr(codex_native, "_attach_with_forwarder", fake_attach_with_forwarder)
    monkeypatch.setattr(
        codex_native,
        "_record_launch_for_fresh_session",
        lambda session_id: order.append(f"record:{session_id}"),
    )

    codex_native._run_with_local_server(
        tmp_path / "codex.yaml",
        session_id=None,
        resume_picker=False,
        codex_args=(),
        command="/opt/codex/bin/codex",
        model=None,
        prompt=None,
    )

    assert order == ["prepare", "record:conv_fresh", "attach"]
