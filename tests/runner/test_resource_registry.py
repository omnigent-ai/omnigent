"""Tests for runner-side SessionResourceRegistry (Phase 2)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from omnigent.debug_logging import record_to_row
from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.os_env import EditEntry, OpResult, OSEnvironment
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner.resource_registry import (
    _TERMINAL_EXIT_OUTPUT_MAX_CHARS,
    _TERMINAL_EXIT_OUTPUT_MAX_LINES,
    CLAUDE_NATIVE_TERMINAL_ROLE,
    CODEX_NATIVE_TERMINAL_ROLE,
    PI_NATIVE_TERMINAL_ROLE,
    SessionResourceRegistry,
    TerminalExitEvent,
    TerminalLifecycle,
    _sanitize_session_id,
    _session_workspace,
    _terminal_exit_diagnostics,
    trim_terminal_output,
)
from omnigent.terminals import TerminalRegistry
from tests._helpers.polling import poll_until_async
from tests.runner.helpers import make_test_terminal_instance

# Stands in for Claude's terminal: answers the OSC 7501 probe, then writes one report per key.
_CLAUDE_OSC_PEER_SCRIPT = (
    "import os,sys,tty\n"
    "tty.setraw(0)\n"
    "if sys.argv[1]=='osc':\n"
    " os.write(1,b'\\x1b]7501;?\\x1b\\\\\\x1b[c')\n"
    " reply=b''\n"
    " while not reply.endswith(b'\\x1b\\\\'): reply+=os.read(0,1)\n"
    " assert reply.startswith(b'\\x1b]7501;?')\n"
    " reply=b''\n"
    " while not reply.endswith(b'c'): reply+=os.read(0,1)\n"
    " os.write(1,b'\\x1b]7501;state=working:app=claude-code\\x07')\n"
    "reports={b'b':b'state=blocked:kind=permission',b'd':b'state=done',"
    "b'i':b'state=idle',b'e':b'state=error',b'w':b'state=working',"
    "b'c':b'state=clear',b'u':b'state=clear',b'x':b'state=idle:id=child',"
    "b'o':b'state=idle:app=other',b'a':b'state=clear:app=other',"
    "b'y':b'state=clear:id=child'}\n"
    "while True:\n"
    " key=os.read(0,1)\n"
    " if key==b'q': break\n"
    " if key==b'r':\n"
    "  open(sys.argv[2],'w').write(str(tuple(os.get_terminal_size())))\n"
    "  continue\n"
    " body=(b'' if key==b'u' else b'app=claude-code:')+reports[key]\n"
    " os.write(1,b'\\x1b]7501;'+body+b'\\x1b\\\\')\n"
)


def _claude_osc_peer(path: Path) -> Path:
    path.write_text(_CLAUDE_OSC_PEER_SCRIPT)
    return path


def _claude_osc_spec(
    tmp_path: Path, peer: Path, protocol: str, sandbox_type: str
) -> TerminalEnvSpec:
    return TerminalEnvSpec(
        command=sys.executable,
        args=[str(peer), protocol, str(tmp_path / "size")],
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(
                type=sandbox_type,
                read_paths=[str(Path(__file__).resolve().parents[2])],
                write_paths=[str(tmp_path)],
            ),
        ),
        keep_alive_after_exit=True,
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["osc", "legacy", "disabled"])
@pytest.mark.parametrize(
    "sandbox_type",
    [
        "none",
        pytest.param(
            "darwin_seatbelt" if sys.platform == "darwin" else "linux_bwrap",
            marks=pytest.mark.skipif(
                shutil.which("sandbox-exec" if sys.platform == "darwin" else "bwrap") is None,
                reason="native sandbox unavailable",
            ),
        ),
    ],
)
async def test_claude_program_status_real_launch_and_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    protocol: str,
    sandbox_type: str,
) -> None:
    """Exercise startup negotiation, raw PTY transport, status arbitration and cleanup."""
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    if protocol == "disabled":
        monkeypatch.setenv("OMNIGENT_FEATURES", "-claude_osc_status")
    config = tmp_path / "claude-config"
    sessions = config / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    peer = _claude_osc_peer(tmp_path / "peer.py")
    terminals = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminals)
    statuses: list[tuple[str, str | None]] = []
    registry.set_session_status_publisher(
        lambda _, state, reason: statuses.append((state, reason))
    )
    spec = _claude_osc_spec(tmp_path, peer, protocol, sandbox_type)

    async def wait_status(state: str, reason: str | None = None) -> None:
        async with asyncio.timeout(10):
            while not statuses or statuses[-1] != (state, reason):
                await asyncio.sleep(0.02)

    try:
        await registry.launch_required_terminal(
            "osc-test", "claude", "main", spec, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
        )
        instance = terminals.get("osc-test", "claude", "main")
        assert instance is not None
        reader = instance.program_status
        if protocol == "disabled":
            assert reader is None
            status_pid = instance.pane_pid_sync()
            assert status_pid is not None
        else:
            assert reader is not None
            async with asyncio.timeout(5):
                while reader.child_pid is None:
                    await asyncio.sleep(0.02)
            status_pid = reader.child_pid
        status_file = sessions / f"{status_pid}.json"
        status_file.write_text(json.dumps({"kind": "interactive", "status": "busy"}))
        await wait_status("running")
        await instance._tmux("resize-window", "-t", "main", "-x", "93", "-y", "37")
        async with asyncio.timeout(5):
            while (
                not (tmp_path / "size").exists() or (tmp_path / "size").read_text() != "(93, 37)"
            ):
                assert await instance.is_alive(), instance.last_exit_text()
                await instance.send(keys="r")
                await asyncio.sleep(0.05)
        if protocol != "osc":
            assert reader is None or reader.status is None
            status_file.write_text(json.dumps({"kind": "interactive", "status": "idle"}))
            await wait_status("idle")
            return
        assert reader is not None
        async with asyncio.timeout(5):
            while reader.status is None:
                await asyncio.sleep(0.02)
        assert reader.status.state == "working"
        # A contradictory file and a quiet screen must not override live OSC.
        status_file.write_text(json.dumps({"kind": "interactive", "status": "idle"}))
        # Real elapsed time, longer than the pane idle threshold.
        # custom-lint: disable-next=sleep-then-assert -- the fixed window is what's tested
        await asyncio.sleep(1.4)
        assert statuses == [("running", None)]
        # Child and other-app reports are ignored. The permission report arrives on the
        # same stream after them, so once it is published they have all been read.
        for key in ("x", "o", "a", "y", "b"):
            await instance.send(keys=key)
        await wait_status("running", "permission prompt")
        assert statuses == [("running", None), ("running", "permission prompt")]
        for source in ("reconnect", "forwarded_idle"):
            before = len(statuses)
            if source == "reconnect":
                registry.resync_session_statuses()
            else:
                registry.note_external_session_status("osc-test", "idle")
            async with asyncio.timeout(5):
                while len(statuses) == before:
                    await asyncio.sleep(0.02)
            assert statuses[-1] == ("running", "permission prompt")
        for key, state in (("d", "idle"), ("w", "running"), ("i", "idle")):
            await instance.send(keys=key)
            await wait_status(state)
        for key in ("c", "u"):
            await instance.send(keys=key)
            await wait_status("idle")  # Falls back to the still-unchanged status file.
            await instance.send(keys="w")
            await wait_status("running")
        # An error report publishes nothing; the StopFailure hook owns the failed edge.
        await instance.send(keys="e")
        async with asyncio.timeout(5):
            while reader.status is None or reader.status.state != "error":
                await asyncio.sleep(0.02)
        # Receiving the report does not mean the background poller has observed it yet.
        await poll_until_async(
            lambda: any(
                getattr(record, "event_name", None) == "claude_program_error_observed"
                for record in caplog.records
            ),
            timeout_s=5,
        )
        assert statuses[-1] == ("running", None)
        observations = [
            record_to_row(record, source="runner")
            for record in caplog.records
            if getattr(record, "event_name", None) == "claude_program_error_observed"
        ]
        assert len(observations) == 1
        assert observations[0]["session_id"] == "osc-test"
        assert observations[0]["attributes"]["terminal_instance_id"] == instance.diagnostic_id
        assert (
            observations[0]["attributes"]["terminal_launch_id"]
            == instance.lifecycle_trace.launch_id
        )
        reader.path.unlink()
        reader.path.mkdir()  # Make snapshot replacement fail while the command keeps running.
        await instance.send(keys="b")
        await wait_status("idle")
        assert await instance.is_alive()
        reader.path.rmdir()
        await instance.send(keys="q")
        async with asyncio.timeout(5):
            while await instance.is_alive():
                await asyncio.sleep(0.05)
    finally:
        await terminals.shutdown()
    assert reader.status is None
    assert not reader.path.exists()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
@pytest.mark.asyncio
async def test_claude_osc_error_without_a_hook_fails_the_turn_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn whose StopFailure hook never reaches the runner still ends, failed once."""
    monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    terminals = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminals)
    statuses: list[tuple[str, str | None]] = []
    registry.set_session_status_publisher(
        lambda _, state, reason: statuses.append((state, reason))
    )
    spec = _claude_osc_spec(tmp_path, _claude_osc_peer(tmp_path / "peer.py"), "osc", "none")
    try:
        await registry.launch_required_terminal(
            "osc-test", "claude", "main", spec, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
        )
        instance = terminals.get("osc-test", "claude", "main")
        assert instance is not None and instance.program_status is not None
        poller = registry._status_pollers["osc-test"]
        raw_status = Mock(wraps=poller._on_status)
        monkeypatch.setattr(poller, "_on_status", raw_status)
        async with asyncio.timeout(5):
            while instance.program_status.child_pid is None:
                await asyncio.sleep(0.02)
        await instance.send(keys="w")
        await _wait_for_status_edge(statuses, ("running", None))
        await instance.send(keys="e")
        # No hook edge arrives, so the runner's grace period is what ends the turn.
        await _wait_for_status_edge(statuses, ("failed", None), timeout=15)
        await asyncio.sleep(0.5)
        assert statuses.count(("failed", None)) == 1
        assert raw_status.call_args_list.count(call("failed", None)) == 1
    finally:
        await terminals.shutdown()


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
@pytest.mark.asyncio
async def test_claude_osc_error_is_not_failed_again_once_the_hook_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forwarded StopFailure edge ends the turn, so the grace period adds no second failure."""
    monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    terminals = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminals)
    statuses: list[tuple[str, str | None]] = []
    registry.set_session_status_publisher(
        lambda _, state, reason: statuses.append((state, reason))
    )
    spec = _claude_osc_spec(tmp_path, _claude_osc_peer(tmp_path / "peer.py"), "osc", "none")
    try:
        await registry.launch_required_terminal(
            "osc-test", "claude", "main", spec, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
        )
        instance = terminals.get("osc-test", "claude", "main")
        assert instance is not None and instance.program_status is not None
        poller = registry._status_pollers["osc-test"]
        raw_status = Mock(wraps=poller._on_status)
        monkeypatch.setattr(poller, "_on_status", raw_status)
        async with asyncio.timeout(5):
            while instance.program_status.child_pid is None:
                await asyncio.sleep(0.02)
        await instance.send(keys="w")
        await _wait_for_status_edge(statuses, ("running", None))
        await instance.send(keys="e")
        async with asyncio.timeout(5):
            while instance.program_status.status is None or (
                instance.program_status.status.state != "error"
            ):
                await asyncio.sleep(0.02)
        # The StopFailure edge reaches the runner as a terminal edge, clearing the turn.
        registry.note_external_session_status("osc-test", "failed")
        await asyncio.sleep(6.0)  # Past the production grace period.
        assert statuses[-1] == ("running", None)
        assert ("failed", None) not in statuses
        assert call("failed", None) not in raw_status.call_args_list
    finally:
        await terminals.shutdown()


async def _wait_for_status_edge(
    statuses: list[tuple[str, str | None]], edge: tuple[str, str | None], *, timeout: float = 10
) -> None:
    async with asyncio.timeout(timeout):
        while not statuses or statuses[-1] != edge:
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "features", "expected"),
    [
        (CLAUDE_NATIVE_TERMINAL_ROLE, None, "claude-code"),
        (CLAUDE_NATIVE_TERMINAL_ROLE, "", "claude-code"),
        (CLAUDE_NATIVE_TERMINAL_ROLE, "usage_page", "claude-code"),
        (CLAUDE_NATIVE_TERMINAL_ROLE, "claude_osc_status", "claude-code"),
        (CLAUDE_NATIVE_TERMINAL_ROLE, "-claude_osc_status", None),
        (PI_NATIVE_TERMINAL_ROLE, None, None),
        (CODEX_NATIVE_TERMINAL_ROLE, None, None),
        (None, None, None),
    ],
)
async def test_program_status_respects_flag_and_claude_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
    features: str | None,
    expected: str | None,
) -> None:
    monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    if features is not None:
        monkeypatch.setenv("OMNIGENT_FEATURES", features)
    monkeypatch.setenv("OMNIGENT_CLAUDE_OSC_STATUS", "1")
    terminals = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminals)
    # Like server release flags, the runner keeps one startup snapshot.
    monkeypatch.setenv(
        "OMNIGENT_FEATURES", "-claude_osc_status" if expected else "claude_osc_status"
    )
    seen = []

    async def launch(**kwargs):
        seen.append(kwargs.get("program_status_app"))
        return make_test_terminal_instance("agent", "main", tmp_path)

    monkeypatch.setattr(terminals, "launch", launch)
    await registry.launch_required_terminal(
        "flag-test", "agent", "main", TerminalEnvSpec(), resource_role=role
    )
    assert seen == [expected]


@dataclass
class _FakeOSEnvironment(OSEnvironment):
    """Minimal concrete OSEnvironment for registry tests."""

    _closed: bool = False

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> OpResult:
        del path, offset, limit
        return {}

    async def write(self, path: str, content: str) -> OpResult:
        del path, content
        return {}

    async def edit(
        self,
        path: str,
        *,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: Sequence[EditEntry] | None = None,
    ) -> OpResult:
        del path, old_text, new_text, edits
        return {}

    async def shell(
        self,
        command: str,
        timeout: int | None = None,
    ) -> OpResult:
        del command, timeout
        return {}

    def close(self) -> None:
        self._closed = True


def _agent_spec_with_sandbox_none(cwd: Path) -> SimpleNamespace:
    """
    Return an agent-like object with an explicit sandbox-free OS env.

    :param cwd: Working directory for the OS environment.
    :returns: Object exposing an ``os_env`` attribute.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


def _seed_terminal(
    registry: TerminalRegistry,
    conversation_id: str,
    name: str,
    session_key: str,
    tmp_path: Path,
    *,
    os_env: OSEnvironment | None = None,
) -> None:
    """Seed a running terminal in the registry."""
    slot = registry._by_conversation.setdefault(conversation_id, {})
    slot[(name, session_key)] = TerminalInstance(
        name=name,
        session_key=session_key,
        socket_path=tmp_path / f"{name}-{session_key}.sock",
        private_dir=tmp_path / f"{name}-{session_key}",
        os_env=os_env,
        running=True,
    )


def test_list_resources_includes_default_env() -> None:
    """Registry always includes the logical default environment."""
    reg = SessionResourceRegistry()
    page = reg.list_resources("conv_1")

    ids = [r.id for r in page.data]
    assert DEFAULT_ENVIRONMENT_ID in ids
    default = page.data[0]
    assert default.type == "environment"
    assert default.metadata["role"] == "primary"


def test_list_resources_includes_terminals(tmp_path: Path) -> None:
    """Registry includes running terminals from the TerminalRegistry."""
    tr = TerminalRegistry()
    _seed_terminal(tr, "conv_1", "bash", "s1", tmp_path)
    reg = SessionResourceRegistry(terminal_registry=tr)

    page = reg.list_resources("conv_1")
    ids = [r.id for r in page.data]
    assert "terminal_bash_s1" in ids


def test_list_resources_filters_by_type(tmp_path: Path) -> None:
    """Registry filters by resource_type when specified."""
    tr = TerminalRegistry()
    _seed_terminal(tr, "conv_1", "bash", "s1", tmp_path)
    reg = SessionResourceRegistry(terminal_registry=tr)

    env_page = reg.list_resources("conv_1", resource_type="environment")
    assert all(r.type == "environment" for r in env_page.data)

    term_page = reg.list_resources("conv_1", resource_type="terminal")
    assert all(r.type == "terminal" for r in term_page.data)


@pytest.mark.asyncio
async def test_terminal_resource_role_is_private_and_cleared_on_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Terminal role markers stay private and follow close lifecycle.

    The Codex ensure route relies on this internal marker to distinguish a
    runner-owned Codex TUI from a generic ``codex/main`` terminal. If the
    marker leaks into public resource metadata, or if close leaves the marker
    behind, stale generic terminals can be misclassified on a later ensure.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    registry.set_client_surface("conv_codex", "desktop")

    async def _fake_launch(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        **kwargs: object,
    ) -> TerminalInstance:
        """
        Register a fake terminal instead of starting tmux.

        :param conversation_id: Owning session id, e.g. ``"conv_codex"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :param spec: Terminal spec passed by the caller.
        :param kwargs: Additional launch kwargs.
        :returns: The fake terminal instance.
        """
        del kwargs
        assert spec.env == {"OMNIGENT_CLIENT_SURFACE": "desktop", "KEEP_ME": "present"}
        terminal_registry._by_conversation.setdefault(conversation_id, {})[
            (terminal_name, session_key)
        ] = instance
        return instance

    async def _fake_close(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
    ) -> bool:
        """
        Remove the fake terminal from the registry.

        :param conversation_id: Owning session id, e.g. ``"conv_codex"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :returns: ``True`` when the fake terminal existed.
        """
        slot = terminal_registry._by_conversation.get(conversation_id, {})
        return slot.pop((terminal_name, session_key), None) is not None

    monkeypatch.setattr(terminal_registry, "launch", _fake_launch)
    monkeypatch.setattr(terminal_registry, "close", _fake_close)

    view = await registry.launch_auxiliary_terminal(
        "conv_codex",
        "codex",
        "main",
        TerminalEnvSpec(
            command="codex",
            args=["--remote", "ws://127.0.0.1:1234"],
            env={"OMNIGENT_CLIENT_SURFACE": "cli", "KEEP_ME": "present"},
        ),
        resource_role=CODEX_NATIVE_TERMINAL_ROLE,
    )

    assert registry.terminal_resource_role("conv_codex", view.id) == CODEX_NATIVE_TERMINAL_ROLE
    assert "command" not in view.metadata
    assert "args" not in view.metadata

    with caplog.at_level(logging.INFO, logger="omnigent.runner.resource_registry"):
        closed = await registry.close_terminal("conv_codex", view.id)

    assert closed is True
    assert registry.terminal_resource_role("conv_codex", view.id) is None
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_close_requested"
    )
    assert record.session_id == "conv_codex"
    assert record.attributes["terminal_id"] == view.id
    assert record.attributes["terminal_instance_id"] == instance.diagnostic_id
    await registry.cleanup_session("conv_codex")
    assert registry.client_surface_env("conv_codex") == {"OMNIGENT_CLIENT_SURFACE": "unknown"}


@pytest.mark.asyncio
async def test_terminal_resource_role_moves_on_transfer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Private terminal role markers follow terminal transfer.

    Native Codex can rotate ownership between Omnigent sessions. If the role stays
    on the old session id, a warm reattach to the new session would look like
    a generic terminal and be replaced incorrectly.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("codex", "main", tmp_path)

    async def _fake_launch(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        **kwargs: object,
    ) -> TerminalInstance:
        """
        Register a fake terminal instead of starting tmux.

        :param conversation_id: Owning session id, e.g. ``"conv_old"``.
        :param terminal_name: Terminal name, e.g. ``"codex"``.
        :param session_key: Terminal session key, e.g. ``"main"``.
        :param spec: Terminal spec passed by the caller.
        :param kwargs: Additional launch kwargs.
        :returns: The fake terminal instance.
        """
        del spec, kwargs
        terminal_registry._by_conversation.setdefault(conversation_id, {})[
            (terminal_name, session_key)
        ] = instance
        return instance

    async def _no_status_link(_link: str) -> None:
        """
        Avoid tmux calls while transfer updates the conversation link.

        :param _link: New conversation link.
        :returns: None.
        """

    monkeypatch.setattr(terminal_registry, "launch", _fake_launch)
    monkeypatch.setattr(instance, "set_conversation_link", _no_status_link)

    view = await registry.launch_auxiliary_terminal(
        "conv_old",
        "codex",
        "main",
        TerminalEnvSpec(command="codex", args=["--remote", "ws://127.0.0.1:1234"]),
        resource_role=CODEX_NATIVE_TERMINAL_ROLE,
    )

    moved = await registry.transfer_terminal("conv_old", "conv_new", view.id)

    assert moved is not None
    assert registry.terminal_resource_role("conv_old", view.id) is None
    assert registry.terminal_resource_role("conv_new", view.id) == CODEX_NATIVE_TERMINAL_ROLE


@pytest.mark.asyncio
async def test_terminal_lifecycle_cannot_change_after_observe(tmp_path: Path) -> None:
    """A terminal cannot silently switch between auxiliary and required lifecycle."""
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("worker", "main", tmp_path)

    await registry.observe_auxiliary_terminal("conv_lifecycle", "worker", "main", instance)

    with pytest.raises(RuntimeError, match="already observed as auxiliary"):
        await registry.observe_required_terminal("conv_lifecycle", "worker", "main", instance)


@pytest.mark.asyncio
async def test_auxiliary_terminal_exit_publishes_resource_exit_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Auxiliary terminal exit is reported with auxiliary lifecycle metadata."""
    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("sidecar", "s1", tmp_path)
    instance.command = "worker-cli"
    instance.args = ["--verbose"]
    instance.launch_cwd = str(tmp_path)
    instance._remember_pane_snapshot("\x1b[31mstartup failed\x1b[0m\nretry login")
    terminal_registry._by_conversation.setdefault("conv_exit", {})[("sidecar", "s1")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()
    callbacks: dict[str, object] = {}

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s
        callbacks["on_exit"] = on_exit
        callbacks["replace"] = replace

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    registry.set_terminal_exit_publisher(_publish_exit)

    await registry.observe_auxiliary_terminal("conv_exit", "sidecar", "s1", instance)
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert [event.lifecycle for event in exits] == [TerminalLifecycle.AUXILIARY]
    assert exits[0].terminal_id == "terminal_sidecar_s1"
    assert exits[0].command == "worker-cli"
    assert exits[0].args_count == 1
    assert exits[0].cwd == str(tmp_path)
    assert exits[0].last_output == "startup failed\nretry login"
    assert terminal_registry.get("conv_exit", "sidecar", "s1") is None
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_exit_observed"
    )
    assert record.session_id == "conv_exit"
    assert record.attributes["terminal_lifecycle"] == "auxiliary"
    assert record.attributes["terminal_instance_id"] == instance.diagnostic_id
    assert record.attributes["session_status_before_exit"] == "unknown"
    assert record.attributes["superseded"] is False
    # A non-Codex terminal keeps the guarantee: no pane contents, no cwd in
    # the lifecycle-event attributes.
    assert record.attributes["terminal_last_output"] is None
    assert "startup failed" not in str(record.attributes)
    assert str(tmp_path) not in str(record.attributes)


@pytest.mark.asyncio
async def test_auxiliary_codex_exit_persists_redacted_final_screen(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A Codex (auxiliary) exit records a redacted final-screen excerpt on the event.

    The exit publisher drops ``last_output`` for auxiliary terminals, so the
    ``terminal_exit_observed`` debug event is the only durable path to the final
    screen; it must carry a credential-redacted excerpt.
    """
    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("codex", "main", tmp_path)
    instance._remember_pane_snapshot(
        "gateway ready\napi_key=sk-supersecretvalue1234\n> Ask Codex to do anything"
    )
    terminal_registry._by_conversation.setdefault("conv_codex", {})[("codex", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()
    callbacks: dict[str, object] = {}

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    registry.set_terminal_exit_publisher(_publish_exit)

    await registry.observe_auxiliary_terminal(
        "conv_codex", "codex", "main", instance, resource_role=CODEX_NATIVE_TERMINAL_ROLE
    )
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    # The published event is dropped for auxiliary terminals and still carries
    # the raw screen; the persisted debug event must carry a redacted excerpt.
    assert exits[0].lifecycle == TerminalLifecycle.AUXILIARY
    assert "sk-supersecretvalue1234" in (exits[0].last_output or "")
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_exit_observed"
    )
    excerpt = record.attributes["terminal_last_output"]
    assert "gateway ready" in excerpt
    assert "Ask Codex to do anything" in excerpt
    assert "sk-supersecretvalue1234" not in excerpt
    assert "[REDACTED]" in excerpt


async def _observe_native_agent_terminal_and_capture(
    registry: SessionResourceRegistry,
    terminal_registry: TerminalRegistry,
    instance: object,
    session_id: str,
) -> dict[str, object]:
    """Observe *instance* as the native agent terminal, capturing its watcher.

    Returns the captured ``on_idle`` / ``on_activity`` / ``on_exit`` callbacks
    so a test can drive the PTY-status edges directly.
    """
    callbacks: dict[str, object] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        callbacks["on_idle"] = on_idle
        callbacks["on_activity"] = on_activity
        callbacks["on_exit"] = on_exit
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[attr-defined]
    # A status publisher is required for the native agent terminal's watcher to
    # wire its running/idle edges (and thus record the PTY status).
    registry.set_session_status_publisher(lambda _sid, _status, _reason=None: None)
    await registry.observe_required_terminal(
        session_id,
        instance.name,  # type: ignore[attr-defined]
        instance.session_key,  # type: ignore[attr-defined]
        instance,
        resource_role=CLAUDE_NATIVE_TERMINAL_ROLE,
    )
    return callbacks


class _FakeStatusPoller:
    """Controllable stand-in for the claude-native status-file poller.

    Lets a test flip :attr:`active` (file resolved and therefore owning the
    session's status, vs. the PTY watcher falling back) and fire status edges
    through the registry's callback, without touching a real
    ``sessions/<pid>.json``.
    """

    def __init__(self, on_status: object) -> None:
        self._on_status = on_status
        self.active = False
        self.blocked_on: str | None = None
        self.ticks = 0
        self.retired = False
        self.resyncs = 0

    def tick(self) -> None:
        self.ticks += 1

    def retire(self) -> None:
        self.retired = True
        self.active = False

    def resync(self) -> None:
        self.resyncs += 1

    def emit(self, status: str, blocked_on: str | None = None) -> None:
        """Simulate the file reporting a new status."""
        self._on_status(status, blocked_on)


async def _observe_native_with_fake_poller(
    tmp_path: Path,
    session_id: str,
    activity_publisher: Callable[[str, str], None] | None = None,
) -> tuple[dict[str, object], list[str], list[_FakeStatusPoller], SessionResourceRegistry]:
    """Observe a claude-native terminal with an injected fake poller.

    :returns: ``(callbacks, statuses, pollers, registry)`` — the wired watcher
        callbacks, the list the status publisher appends to, the
        single-element list holding the injected poller (so the test can
        drive ``active`` / ``running_level`` / ``emit``), and the registry
        itself (so the test can post external status edges).
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(session_id, {})[("claude", "main")] = instance
    statuses: list[str] = []
    pollers: list[_FakeStatusPoller] = []
    registry.set_session_status_publisher(
        lambda _sid, status, _reason=None: statuses.append(status)
    )
    if activity_publisher is not None:
        registry.set_terminal_activity_publisher(activity_publisher)

    def _fake_build(*, session_id: str, instance: object, on_status: object) -> _FakeStatusPoller:
        del session_id, instance
        poller = _FakeStatusPoller(on_status)
        pollers.append(poller)
        return poller

    registry._build_claude_native_status_poller = _fake_build  # type: ignore[method-assign]

    callbacks: dict[str, object] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        callbacks["on_idle"] = on_idle
        callbacks["on_activity"] = on_activity
        callbacks["on_exit"] = on_exit
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[attr-defined]
    await registry.observe_required_terminal(
        session_id, "claude", "main", instance, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
    )
    return callbacks, statuses, pollers, registry


async def _observe_native_with_real_poller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_id: str,
) -> tuple[Callable[[], None], list[str], SessionResourceRegistry, Path]:
    """Observe a Claude terminal with a real poller and caller-written status file."""
    config_dir = tmp_path / "claude"
    status_file = config_dir / "sessions" / "123.json"
    status_file.parent.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(session_id, {})[("claude", "main")] = instance
    watcher = Mock()
    monkeypatch.setattr(instance, "start_idle_watcher_thread", watcher)
    monkeypatch.setattr(instance, "pane_pid_sync", lambda: 123)
    statuses: list[str] = []
    registry.set_session_status_publisher(
        lambda _sid, status, _reason=None: statuses.append(status)
    )
    await registry.observe_required_terminal(
        session_id, "claude", "main", instance, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
    )
    return watcher.call_args.kwargs["on_tick"], statuses, registry, status_file


@pytest.mark.asyncio
async def test_claude_native_wires_status_poller_tick(tmp_path: Path) -> None:
    """The claude-native watcher is wired with an ``on_tick`` that drives
    the status-file poller."""
    callbacks, _statuses, pollers, _registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_tick"
    )
    assert len(pollers) == 1
    on_tick = callbacks["on_tick"]
    assert callable(on_tick)
    on_tick()
    on_tick()
    assert pollers[0].ticks == 2


@pytest.mark.asyncio
async def test_pane_publishes_no_status_while_the_file_owns_it(tmp_path: Path) -> None:
    """An active file poller is the only status source; the pane publishes none.

    Claude redraws its prompt after a turn and blinks a cursor, so the pane
    keeps changing once the file has already said ``idle``. Letting both
    publish is what made that redraw contradict the file and needed a
    freshness window to arbitrate — so while the file is readable it decides,
    and the pane's edges are dropped.
    """
    callbacks, statuses, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_file_owns"
    )
    poller = pollers[0]
    poller.active = True

    initial_activity = registry.session_activity_epoch("conv_file_owns")
    poller.emit("running")
    callbacks["on_activity"]()  # pane redraws mid-turn — no second edge
    await asyncio.sleep(0)
    assert statuses == ["running"]

    poller.emit("idle")
    callbacks["on_activity"]()  # post-turn prompt redraw is not a new turn
    callbacks["on_idle"]()  # nor does a quiet pane re-assert idle
    await asyncio.sleep(0)
    assert statuses == ["running", "idle"]

    assert registry.session_activity_epoch("conv_file_owns") > initial_activity


@pytest.mark.asyncio
async def test_parked_pane_stays_running_then_recovers_on_pane_death(tmp_path: Path) -> None:
    """A dialog keeps the session running; a dead pane still ends it.

    While Claude is parked on a prompt the pane is quiet but the turn is not
    over, and only the file knows that — so the quiet pane must not publish
    ``idle``. But a killed Claude leaves that ``waiting`` record behind, so
    pane death retires the poller and the PTY side owns the outcome. Without
    that, the session would spin forever.
    """
    callbacks, statuses, pollers, _registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_parked"
    )
    poller = pollers[0]
    poller.active = True

    poller.emit("running", "permission prompt")
    callbacks["on_idle"]()  # pane quiet under the dialog — turn is NOT over
    await asyncio.sleep(0)
    assert statuses == ["running"]

    # Claude is killed at the prompt. Its file survives holding ``waiting``.
    callbacks["on_exit"]()
    assert poller.retired is True
    assert poller.active is False

    # The pane now owns status again, so the session can settle.
    callbacks["on_idle"]()
    await asyncio.sleep(0)
    assert statuses == ["running", "idle"]


@pytest.mark.asyncio
async def test_hook_status_resyncs_watcher_dedup(tmp_path: Path) -> None:
    """A forwarder's hook-derived edge rebases the shared dedup baseline.

    ``Stop`` → ``idle`` is posted to the server by the claude-native forwarder,
    bypassing this watcher. Adopting it as the baseline is what makes the pair
    idempotent: the file's own ``idle`` lands on the same edge and is collapsed,
    so the two agree regardless of which arrives first.
    """
    callbacks, statuses, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_resync"
    )
    poller = pollers[0]
    on_activity = callbacks["on_activity"]
    assert callable(on_activity)
    on_activity()
    assert registry.session_activity_epoch("conv_resync") == 0
    assert not registry.session_turn_is_active("conv_resync")
    poller.active = True

    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running"]
    assert registry.session_activity_epoch("conv_resync") > 0
    assert registry.session_turn_is_active("conv_resync")

    # The forwarder posts Stop → idle straight to the server.
    registry.note_external_session_status("conv_resync", "idle")
    assert not registry.session_turn_is_active("conv_resync")

    # The file catches up moments later with the same edge — deduped away, so
    # the user sees one idle rather than a flicker.
    poller.emit("idle")
    await asyncio.sleep(0)
    assert statuses == ["running"]

    # Next turn: the file reports work again and must publish.
    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running", "running"]
    del callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening_event", ["none", "failed-hook", "idle-hook", "new-turn"])
async def test_queued_fallback_failure_preserves_newer_turn_state(
    tmp_path: Path, intervening_event: str
) -> None:
    """A pending watcher failure must not replace a hook or end a newer turn."""
    session_id = "conv_queued_failure"
    _, statuses, pollers, registry = await _observe_native_with_fake_poller(tmp_path, session_id)
    poller = pollers[0]
    poller.emit("running")
    await asyncio.sleep(0)
    poller.emit("failed")
    if intervening_event != "none":
        registry.note_external_session_status(
            session_id, "idle" if intervening_event == "idle-hook" else "failed"
        )
    if intervening_event == "new-turn":
        registry.note_session_turn_started(session_id)
    await asyncio.sleep(0)
    assert statuses == (["running", "failed"] if intervening_event == "none" else ["running"])
    assert registry.session_turn_is_active(session_id) == (intervening_event == "new-turn")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason"),
    [("running", None), ("running", "permission prompt"), ("idle", None), ("failed", None)],
)
@pytest.mark.parametrize("replacement", [False, True], ids=["removed", "replaced"])
@pytest.mark.parametrize("queued_first", [False, True], ids=["late-read", "queued-read"])
async def test_old_terminal_status_cannot_change_its_successor(
    tmp_path: Path,
    status: str,
    reason: str | None,
    replacement: bool,
    queued_first: bool,
) -> None:
    """A stale watcher must not publish or change activity after its terminal leaves."""
    session_id = "conv_terminal_replaced"
    _, statuses, pollers, registry = await _observe_native_with_fake_poller(tmp_path, session_id)
    poller = pollers[0]
    poller.emit("running")
    await asyncio.sleep(0)
    if queued_first:
        poller.emit(status, reason)
    terminals = registry._terminal_registry
    assert terminals is not None
    if replacement:
        terminals._by_conversation[session_id][("claude", "main")] = make_test_terminal_instance(
            "claude", "main", tmp_path / "replacement"
        )
        registry.note_session_turn_started(session_id)
    else:
        terminals._by_conversation[session_id].pop(("claude", "main"))
        registry.note_external_session_status(session_id, "idle")
    current_epoch = registry.session_activity_epoch(session_id)
    if not queued_first:
        poller.emit(status, reason)
    await asyncio.sleep(0)
    assert statuses == ["running"]
    assert registry.session_turn_is_active(session_id) is replacement
    assert registry.session_activity_epoch(session_id) == current_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_change", ["none", "removed", "replaced"])
@pytest.mark.parametrize("queued_first", [False, True], ids=["late-read", "queued-read"])
async def test_terminal_activity_and_ticks_require_the_observed_instance(
    tmp_path: Path, terminal_change: str, queued_first: bool
) -> None:
    """Old watcher callbacks cannot wake a removed session or inspect its replacement."""
    session_id = "conv_activity_replaced"
    activity: list[str] = []
    callbacks, _, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, session_id, lambda _session_id, terminal_id: activity.append(terminal_id)
    )
    poller = pollers[0]
    poller.active = True
    on_activity, on_tick = callbacks["on_activity"], callbacks["on_tick"]
    assert callable(on_activity) and callable(on_tick)
    if queued_first:
        on_activity()
    terminals = registry.terminal_registry
    if terminal_change == "replaced":
        terminals._by_conversation[session_id][("claude", "main")] = make_test_terminal_instance(
            "claude", "main", tmp_path / "replacement"
        )
    elif terminal_change == "removed":
        terminals._by_conversation[session_id].pop(("claude", "main"))
    if not queued_first:
        on_activity()
    on_tick()
    await asyncio.sleep(0)
    assert activity == (["terminal_claude_main"] if terminal_change == "none" else [])
    assert poller.ticks == (1 if terminal_change == "none" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poll_before_stop", [False, True], ids=["busy-read-after-stop", "busy-read-before-stop"]
)
async def test_queued_turn_republishes_running_after_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poll_before_stop: bool
) -> None:
    """A prior turn's Stop must not leave a queued turn reported as idle."""
    session_id = "conv_queued_status"
    on_tick, statuses, registry, status_file = await _observe_native_with_real_poller(
        tmp_path, monkeypatch, session_id
    )
    record = {
        "pid": 123,
        "sessionId": "claude-queued-status",
        "kind": "interactive",
        "status": "busy",
        "statusUpdatedAt": 1000,
    }
    status_file.write_text(json.dumps(record), encoding="utf-8")
    initial_mtime_ns = status_file.stat().st_mtime_ns

    on_tick()
    await asyncio.sleep(0)
    assert statuses == ["running"]
    assert registry.session_turn_is_active(session_id)

    # The queued turn overwrites idle before the real poller gets another tick.
    for status, updated_at in [("idle", 2000), ("busy", 3000)]:
        status_file.write_text(
            json.dumps({**record, "status": status, "statusUpdatedAt": updated_at}),
            encoding="utf-8",
        )
    next_mtime_ns = initial_mtime_ns + 1_000_000_000
    os.utime(status_file, ns=(next_mtime_ns, next_mtime_ns))
    if poll_before_stop:
        on_tick()
        await asyncio.sleep(0)
        assert statuses == ["running"]

    # The forwarder posts the previous turn's Stop directly to the server.
    registry.note_external_session_status(session_id, "idle")
    assert not registry.session_turn_is_active(session_id)

    on_tick()
    await asyncio.sleep(0)
    assert statuses == ["running", "running"]
    assert registry.session_turn_is_active(session_id)

    on_tick()
    await asyncio.sleep(0)
    assert statuses == ["running", "running"]

    # The queued turn's own Stop and idle file still produce one completion.
    registry.note_external_session_status(session_id, "idle")
    status_file.write_text(
        json.dumps({**record, "status": "idle", "statusUpdatedAt": 4000}),
        encoding="utf-8",
    )
    final_mtime_ns = next_mtime_ns + 1_000_000_000
    os.utime(status_file, ns=(final_mtime_ns, final_mtime_ns))
    on_tick()
    await asyncio.sleep(0)
    assert statuses == ["running", "running"]
    assert not registry.session_turn_is_active(session_id)


@pytest.mark.asyncio
async def test_failed_hook_does_not_republish_stale_running_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed turn stays failed while its busy status file catches up."""
    session_id = "conv_failed_status"
    on_tick, statuses, registry, status_file = await _observe_native_with_real_poller(
        tmp_path, monkeypatch, session_id
    )
    status_file.write_text(
        json.dumps({"kind": "interactive", "sessionId": "claude-failed-status", "status": "busy"}),
        encoding="utf-8",
    )
    on_tick()
    await asyncio.sleep(0)
    assert statuses == ["running"]

    registry.note_external_session_status(session_id, "failed")
    on_tick()
    await asyncio.sleep(0)
    assert statuses == ["running"]
    assert not registry.session_turn_is_active(session_id)


@pytest.mark.asyncio
async def test_reconnect_resync_republishes_a_running_session(tmp_path: Path) -> None:
    """A server restart mid-turn must not strand the session on a stale status.

    The tunnel reconnecting means the *listener* restarted and lost its status
    cache. This runner did not, so its dedup baseline still asserts ``running``
    was delivered — and Claude's file is written only when its value *changes*,
    so nothing re-asserts on its own. Without the resync the session would show
    no spinner and no stop button for the rest of the turn.
    """
    _callbacks, statuses, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_restart"
    )
    poller = pollers[0]
    poller.active = True

    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running"]

    # Mid-turn, the file's value is unchanged, so a re-read publishes nothing:
    # this is exactly what leaves the restarted server on a stale status.
    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running"]

    registry.resync_session_statuses()
    assert poller.resyncs == 1

    # The same value now republishes, so the fresh server learns the truth.
    poller.emit("running")
    await asyncio.sleep(0)
    assert statuses == ["running", "running"]


@pytest.mark.asyncio
async def test_reconnect_resync_keeps_the_exit_classification_memo(tmp_path: Path) -> None:
    """The resync clears published edges, not the exit memo.

    ``_last_session_status`` decides whether a terminal exit reads as a clean
    shutdown or a mid-turn crash. It tracks what the PANE last did, not what the
    server has heard, so a reconnect must leave it alone — clearing it would make
    a crash right after a reconnect look like a tidy exit.
    """
    _callbacks, _statuses, pollers, registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_memo"
    )
    pollers[0].active = True
    pollers[0].emit("running")
    await asyncio.sleep(0)

    registry.resync_session_statuses()

    assert registry._take_session_status_memo("conv_memo") == "running"


@pytest.mark.asyncio
async def test_pty_edges_drive_status_when_poller_inactive(tmp_path: Path) -> None:
    """With no file (poller inactive), the PTY pane edges remain the status
    source — the fallback path for old Claude versions."""
    callbacks, statuses, pollers, _registry = await _observe_native_with_fake_poller(
        tmp_path, "conv_fallback"
    )
    # Poller stays inactive (file never resolved).
    assert pollers[0].active is False

    callbacks["on_activity"]()  # → running
    callbacks["on_idle"]()  # → idle
    # Status edges publish via loop.call_soon_threadsafe; let them drain.
    await asyncio.sleep(0)
    assert statuses == ["running", "idle"]


@pytest.mark.asyncio
async def test_required_terminal_exit_while_idle_is_clean_shutdown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A required terminal that exits after going idle is not a failure.

    The native agent terminal is long-lived: it goes ``idle`` when its turn
    finishes. A pane exit observed while idle means the work was already
    delivered and the process simply shut down, so the exit event must carry
    ``session_was_idle=True`` — the runner uses that to avoid flipping the chat
    to ``failed`` (the spurious-"failed"-session bug).

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    terminal_registry._by_conversation.setdefault("conv_idle", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_idle"
    )

    initial_activity = registry.session_activity_epoch("conv_idle")
    # The agent worked, then its turn completed (pane became quiet → idle).
    on_activity = callbacks["on_activity"]
    on_idle = callbacks["on_idle"]
    assert callable(on_activity) and callable(on_idle)
    on_activity()
    on_idle()
    # Then the pane disappeared (e.g. Claude Code exited cleanly).
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].lifecycle == TerminalLifecycle.REQUIRED
    assert exits[0].session_was_idle is True
    assert registry.session_activity_epoch("conv_idle") == initial_activity
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_exit_observed"
    )
    assert record.session_id == "conv_idle"
    assert record.attributes["terminal_lifecycle"] == "required"
    assert record.attributes["session_status_before_exit"] == "idle"


@pytest.mark.asyncio
async def test_required_terminal_exit_after_omnigent_reap_is_expected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pane Omnigent killed to fail its turn exits as an expected close.

    The delivery-timeout reap ends the pane while its turn is still running,
    so the exit would otherwise look like a crash and fail the turn a second
    time. The reap leaves a marker in the pane's private dir before the kill;
    the exit event must carry it so the runner does not report the exit.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest fixture that stubs the tmux subprocess.
    """
    from omnigent.harnesses.claude_native import bridge as claude_bridge

    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path)
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.private_dir.mkdir(parents=True)
    bridge_dir = tmp_path / "bridge"
    claude_bridge.write_tmux_target(
        bridge_dir, socket_path=instance.private_dir / "tmux.sock", tmux_target="main"
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    terminal_registry._by_conversation.setdefault("conv_reap", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_reap"
    )
    on_activity = callbacks["on_activity"]
    on_exit = callbacks["on_exit"]
    assert callable(on_activity) and callable(on_exit)
    on_activity()
    claude_bridge.kill_session(bridge_dir, expected_close=True)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].lifecycle == TerminalLifecycle.REQUIRED
    assert exits[0].session_was_idle is False
    assert exits[0].expected_close is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_error", "omnigent_killed", "closed_outside"),
    [
        # A live tmux whose only session was killed.
        ("no current target", False, True),
        ("no current target", True, False),
        # The tmux server itself was killed, e.g. by ``killall tmux``.
        ("no server running on /tmp/omnigent-terminal-x/tmux.sock", False, True),
        ("no server running on /tmp/omnigent-terminal-x/tmux.sock", True, False),
    ],
)
async def test_required_terminal_exit_names_a_session_killed_outside_omnigent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    probe_error: str,
    omnigent_killed: bool,
    closed_outside: bool,
) -> None:
    """A Claude session tmux reports killed is outside Omnigent unless Omnigent killed it.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest fixture that stubs the tmux subprocess.
    :param caplog: Captures the exit-observed telemetry row.
    :param probe_error: The has-session failure tmux gave once the session was gone.
    :param omnigent_killed: Whether the stop path's ``kill_session`` ended the pane.
    :param closed_outside: Expected ``closed_outside_omnigent`` on the exit.
    """
    from omnigent.harnesses.claude_native import bridge as claude_bridge

    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path)
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.private_dir.mkdir(parents=True)
    instance.keep_alive_after_exit = True
    instance._last_session_probe_error = f"tmux command failed (rc=1): {probe_error}"
    bridge_dir = tmp_path / "bridge"
    claude_bridge.write_tmux_target(
        bridge_dir, socket_path=instance.private_dir / "tmux.sock", tmux_target="main"
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    terminal_registry._by_conversation.setdefault("conv_killed", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_killed"
    )
    on_activity = callbacks["on_activity"]
    on_exit = callbacks["on_exit"]
    assert callable(on_activity) and callable(on_exit)
    on_activity()
    if omnigent_killed:
        claude_bridge.kill_session(bridge_dir)
    with caplog.at_level(logging.INFO):
        on_exit()
        await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].expected_close is False
    assert exits[0].closed_outside_omnigent is closed_outside
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_exit_observed"
    )
    assert record.attributes["terminal_closed_outside_omnigent"] == str(closed_outside).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_before_exit", [False, True], ids=["current", "superseded"])
async def test_required_terminal_exit_while_running_is_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, replace_before_exit: bool
) -> None:
    """A required terminal that vanishes mid-turn is still a failure.

    When the last PTY-status edge was ``running``, the pane disappeared while
    a turn was in flight — a genuine crash — so the exit event reports
    ``session_was_idle=False`` and the runner keeps failing the session.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    terminal_registry._by_conversation.setdefault("conv_run", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_run"
    )

    on_activity = callbacks["on_activity"]
    assert callable(on_activity)
    on_activity()
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    if replace_before_exit:
        replacement = make_test_terminal_instance("claude", "main", tmp_path / "replacement")
        terminal_registry._by_conversation["conv_run"][("claude", "main")] = replacement
        replacement_callbacks = await _observe_native_agent_terminal_and_capture(
            registry, terminal_registry, replacement, "conv_run"
        )
        registry.note_session_turn_started("conv_run")
        on_exit()
        await registry.wait_for_terminal_exit_cleanup()
        assert exits == []
        assert terminal_registry.get("conv_run", "claude", "main") is replacement
        assert registry.terminal_resource_role("conv_run", "terminal_claude_main") == (
            CLAUDE_NATIVE_TERMINAL_ROLE
        )
        assert registry._terminal_lifecycles[("conv_run", "terminal_claude_main")] == (
            TerminalLifecycle.REQUIRED
        )
        assert registry.session_turn_is_active("conv_run")
        on_exit = replacement_callbacks["on_exit"]
        assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].session_was_idle is False
    record = next(
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "terminal_exit_observed"
        and record.attributes["superseded"] is False
    )
    assert record.attributes["session_status_before_exit"] == "running"


def test_trim_terminal_output_keeps_character_tail() -> None:
    # Over the char budget: keep the last N characters, marked as truncated.
    filler = "\n".join(f"line {i} " + "x" * 150 for i in range(30))
    text = filler + "\n--dangerously-skip-permissions cannot be run for security reasons"
    trimmed = trim_terminal_output(text)
    assert trimmed is not None
    marker, body = trimmed.split("\n", 1)
    assert marker.startswith("... omitted ") and marker.endswith(" earlier character(s) ...")
    assert len(body) == _TERMINAL_EXIT_OUTPUT_MAX_CHARS
    assert body.endswith("for security reasons")


def test_trim_terminal_output_keeps_long_line_before_final_line() -> None:
    # A long error line followed by tmux's short "pane is dead" line must not be
    # dropped wholesale, leaving only the pane-dead line.
    usage = "Usage: codex [OPTIONS] " + "z" * (_TERMINAL_EXIT_OUTPUT_MAX_CHARS + 1000)
    trimmed = trim_terminal_output(f"a\nb\n{usage}\npane is dead (status 2)")
    assert trimmed is not None
    assert trimmed.endswith("z\npane is dead (status 2)")
    assert len(trimmed.split("\n", 1)[1]) == _TERMINAL_EXIT_OUTPUT_MAX_CHARS


def test_trim_terminal_output_skips_blank_pane_rows() -> None:
    # A dead pane's capture has its output at the top, then blank rows down to
    # tmux's "Pane is dead" line; the blank rows must not use up the line budget.
    blank_rows = "\n" * (_TERMINAL_EXIT_OUTPUT_MAX_LINES + 10)
    text = f"Error: failed to get token\n{blank_rows}Pane is dead (status 1)"
    assert trim_terminal_output(text) == "Error: failed to get token\nPane is dead (status 1)"


def test_trim_terminal_output_hard_clips_single_overlong_line() -> None:
    line = "y" * (_TERMINAL_EXIT_OUTPUT_MAX_CHARS + 500)
    trimmed = trim_terminal_output(line)
    assert (
        trimmed
        == f"... omitted 500 earlier character(s) ...\n{'y' * _TERMINAL_EXIT_OUTPUT_MAX_CHARS}"
    )


def test_trim_terminal_output_leaves_short_output_untouched() -> None:
    assert trim_terminal_output("  boom\r\nexit 1\n") == "boom\nexit 1"


def test_terminal_exit_diagnostics_reads_exit_status(tmp_path: Path) -> None:
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    instance.command = "claude"
    instance.args = ["--dangerously-skip-permissions"]
    instance._remember_pane_snapshot("boom")
    # Simulate tmux having reported a dead pane with a captured status.
    instance._remember_exit_status("1 42")
    command, args_count, _cwd, last_output, exit_status = _terminal_exit_diagnostics(instance)
    assert command == "claude"
    assert args_count == 1
    assert last_output == "boom"
    assert exit_status == 42


def test_remember_exit_status_ignores_live_pane() -> None:
    instance = make_test_terminal_instance("claude", "main", tmp_path=Path("/tmp"))
    # Live pane: pane_dead=0, empty status → nothing recorded.
    instance._remember_exit_status("0 ")
    assert instance.last_exit_status() is None


@pytest.mark.asyncio
async def test_required_terminal_exit_without_observed_status_is_failure(tmp_path: Path) -> None:
    """A required terminal that never reported a PTY status fails on exit.

    A boot failure (the process dies before producing any pane activity) leaves
    no recorded status, so the exit defaults to ``session_was_idle=False`` and
    the session still fails — only a positively-observed ``idle`` suppresses the
    failure.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("worker", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_boot", {})[("worker", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()
    callbacks: dict[str, object] = {}

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_activity, on_tick, idle_threshold_s, poll_interval_s, replace
        callbacks["on_exit"] = on_exit

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    registry.set_terminal_exit_publisher(_publish_exit)

    await registry.observe_required_terminal("conv_boot", "worker", "main", instance)
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].session_was_idle is False


@pytest.mark.asyncio
async def test_required_terminal_exit_after_new_turn_is_failure(tmp_path: Path) -> None:
    """A crash right after a new turn starts (before the watcher's first
    ``running`` edge) is a failure, not a stale clean shutdown.

    ``note_session_turn_started`` resets the prior turn's ``idle`` memo so the
    turn-boundary window can't silently swallow a real crash.

    :param tmp_path: Temporary directory for fake terminal paths.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_turn", {})[("claude", "main")] = instance
    exits: list[TerminalExitEvent] = []
    exit_published = asyncio.Event()

    def _publish_exit(event: TerminalExitEvent) -> None:
        exits.append(event)
        exit_published.set()

    registry.set_terminal_exit_publisher(_publish_exit)
    callbacks = await _observe_native_agent_terminal_and_capture(
        registry, terminal_registry, instance, "conv_turn"
    )

    # The previous turn finished (idle), then a new message starts a turn and
    # the pane crashes before the watcher emits its next ``running`` edge.
    on_idle = callbacks["on_idle"]
    assert callable(on_idle)
    on_idle()
    registry.note_session_turn_started("conv_turn")
    on_exit = callbacks["on_exit"]
    assert callable(on_exit)
    on_exit()
    await asyncio.wait_for(exit_published.wait(), timeout=1.0)

    assert len(exits) == 1
    assert exits[0].session_was_idle is False
    assert not registry.session_turn_is_active("conv_turn")


@pytest.mark.asyncio
async def test_cleanup_session_clears_status_memo(tmp_path: Path) -> None:
    """``cleanup_session`` drops the session's PTY-status memo.

    :param tmp_path: Temporary directory (unused beyond registry construction).
    """
    del tmp_path
    registry = SessionResourceRegistry()
    registry.note_session_turn_started("conv_cleanup")
    assert "conv_cleanup" in registry._last_session_status
    assert registry.session_activity_epoch("conv_cleanup") > 0
    assert registry.session_turn_is_active("conv_cleanup")

    await registry.cleanup_session("conv_cleanup")

    assert "conv_cleanup" not in registry._last_session_status
    assert registry.session_activity_epoch("conv_cleanup") == 0
    assert not registry.session_turn_is_active("conv_cleanup")


@pytest.mark.asyncio
async def test_transfer_terminal_moves_status_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transferring a terminal moves its PTY-status memo to the new owner.

    Fakes the launch (no real tmux/process) and the conversation-link update,
    mirroring ``test_terminal_resource_role_moves_on_transfer``.

    :param tmp_path: Temporary directory for fake terminal paths.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("codex", "main", tmp_path)

    async def _fake_launch(
        conversation_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        **kwargs: object,
    ) -> TerminalInstance:
        del spec, kwargs
        terminal_registry._by_conversation.setdefault(conversation_id, {})[
            (terminal_name, session_key)
        ] = instance
        return instance

    async def _no_status_link(_link: str) -> None:
        """Avoid tmux calls while transfer updates the conversation link."""

    monkeypatch.setattr(terminal_registry, "launch", _fake_launch)
    monkeypatch.setattr(instance, "set_conversation_link", _no_status_link)

    view = await registry.launch_auxiliary_terminal(
        "conv_src",
        "codex",
        "main",
        TerminalEnvSpec(command="codex", args=["--remote", "ws://127.0.0.1:1234"]),
        resource_role=CODEX_NATIVE_TERMINAL_ROLE,
    )
    registry.note_session_turn_started("conv_src")

    moved = await registry.transfer_terminal("conv_src", "conv_dst", view.id)

    assert moved is not None
    assert "conv_src" not in registry._last_session_status
    assert registry._last_session_status.get("conv_dst") == "running"
    assert not registry.session_turn_is_active("conv_src")
    assert registry.session_turn_is_active("conv_dst")
    assert registry.session_activity_epoch("conv_dst") > 0


def test_get_resource_finds_default() -> None:
    """get_resource finds the default environment."""
    reg = SessionResourceRegistry()
    resource = reg.get_resource("conv_1", DEFAULT_ENVIRONMENT_ID)
    assert resource is not None
    assert resource.type == "environment"


def test_get_resource_returns_none_for_unknown() -> None:
    """get_resource returns None for unknown ids."""
    reg = SessionResourceRegistry()
    assert reg.get_resource("conv_1", "nonexistent") is None


def test_resolve_environment_creates_primary_lazily(
    tmp_path: Path,
) -> None:
    """resolve_environment lazily creates the primary OSEnvironment."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        assert not reg.has_primary_env("conv_1")

        agent_spec = _agent_spec_with_sandbox_none(tmp_path / "conv_1" / "workspace")
        env = reg.resolve_environment("conv_1", DEFAULT_ENVIRONMENT_ID, agent_spec)
        assert env is not None
        assert reg.has_primary_env("conv_1")

        env2 = reg.resolve_environment("conv_1", DEFAULT_ENVIRONMENT_ID, agent_spec)
        assert env2 is env
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


def test_resolve_environment_default_pins_none_sandbox_when_no_agent_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default env (no agent_spec) must pin ``sandbox.type="none"``.

    Regression test for the resource-endpoint default: the default
    env must work on hosts without a usable sandbox backend. The
    pre-fix code left the default ``OSEnvSpec`` with
    ``sandbox=None``, so it routed through the Linux platform
    default (which would raise when no backend was available). We
    stub ``shutil.which`` to report ``bwrap`` IS present so that, if
    the default env wrongly routed through the platform default, it
    would resolve to an active ``linux_bwrap`` policy — the assertion
    below proves it pins ``none`` instead.

    :param tmp_path: Per-test workspace dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr(
        "omnigent.inner.sandbox.shutil.which",
        lambda name: "/usr/bin/bwrap",
    )
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))

    reg = SessionResourceRegistry()

    env = reg.resolve_environment("conv_no_spec", DEFAULT_ENVIRONMENT_ID)

    # backend_type="none" + active=False prove sandbox=none was
    # pinned, not picked by the platform-default fallback.
    assert env.sandbox.backend_type == "none"
    assert env.sandbox.active is False


def test_resolve_environment_uses_agent_spec_os_env(
    tmp_path: Path,
) -> None:
    """resolve_environment uses agent_spec.os_env when available."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()

        class _FakeSpec:
            os_env = OSEnvSpec(
                type="caller_process",
                cwd=str(tmp_path / "custom-cwd"),
                sandbox=OSEnvSandboxSpec(type="none"),
            )

        (tmp_path / "custom-cwd").mkdir()
        env = reg.resolve_environment(
            "conv_spec",
            DEFAULT_ENVIRONMENT_ID,
            _FakeSpec(),
        )
        assert env is not None
        assert str(env.cwd).endswith("custom-cwd")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


def test_resolve_environment_raises_for_unknown_env_id() -> None:
    """resolve_environment raises ValueError for unknown ids."""
    reg = SessionResourceRegistry()
    with pytest.raises(ValueError, match="not found"):
        reg.resolve_environment("conv_1", "env_nonexistent_foo")


def test_resolve_terminal_environment(tmp_path: Path) -> None:
    """resolve_environment resolves terminal environment ids."""
    tr = TerminalRegistry()
    terminal_env = _FakeOSEnvironment(
        spec=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
        cwd=tmp_path,
    )
    _seed_terminal(
        tr,
        "conv_1",
        "bash",
        "s1",
        tmp_path,
        os_env=terminal_env,
    )
    reg = SessionResourceRegistry(terminal_registry=tr)

    env = reg.resolve_environment("conv_1", "env_terminal_bash_s1")
    assert env is terminal_env


@pytest.mark.asyncio
async def test_cleanup_session_closes_primary_env(
    tmp_path: Path,
) -> None:
    """cleanup_session closes the primary env and cleans terminals."""
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        reg.resolve_environment(
            "conv_1",
            DEFAULT_ENVIRONMENT_ID,
            _agent_spec_with_sandbox_none(tmp_path / "conv_1" / "workspace"),
        )
        assert reg.has_primary_env("conv_1")

        await reg.cleanup_session("conv_1")
        assert not reg.has_primary_env("conv_1")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


@pytest.mark.asyncio
async def test_cleanup_session_primary_close_is_off_loop_and_cancellation_safe() -> None:
    """Primary environment cleanup finishes after repeated cancellation."""
    reg = SessionResourceRegistry()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    heartbeat_ran_while_blocked = asyncio.Event()

    class _BlockingEnvironment(_FakeOSEnvironment):
        def close(self) -> None:
            started.set()
            assert release.wait(timeout=2.0)
            self._closed = True
            finished.set()

    environment = _BlockingEnvironment(
        spec=OSEnvSpec(type="caller_process", cwd="/tmp", sandbox=OSEnvSandboxSpec(type="none")),
        cwd=Path("/tmp"),
    )
    reg._primary_envs["conv_blocked_cleanup"] = environment

    async def heartbeat() -> None:
        while not finished.is_set():
            if started.is_set():
                heartbeat_ran_while_blocked.set()
                return
            await asyncio.sleep(0.01)

    heartbeat_task: asyncio.Task[None] | None = None
    cleanup_task: asyncio.Task[None] | None = None
    watchdog = threading.Thread(
        target=lambda: (release.wait(timeout=2.0), release.set()),
        name="test-resource-cleanup-watchdog",
        daemon=True,
    )
    watchdog.start()
    try:
        heartbeat_task = asyncio.create_task(heartbeat())
        cleanup_task = asyncio.create_task(reg.cleanup_session("conv_blocked_cleanup"))
        await asyncio.wait_for(heartbeat_ran_while_blocked.wait(), timeout=1.0)
        cleanup_task.cancel()
        await asyncio.sleep(0)
        cleanup_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup_task
    finally:
        release.set()
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
        await asyncio.gather(cleanup_task, heartbeat_task, return_exceptions=True)
        watchdog.join(timeout=1.0)

    assert finished.is_set()
    assert environment._closed
    assert not reg.has_primary_env("conv_blocked_cleanup")
    assert heartbeat_ran_while_blocked.is_set()


# ── Phase 4: cleanup endpoint tests ─────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_endpoint_returns_confirmation(
    tmp_path: Path,
) -> None:
    """DELETE /v1/sessions/{id}/resources returns cleanup confirmation."""
    import httpx

    from omnigent.runner import create_runner_app

    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(tmp_path)
    try:
        reg = SessionResourceRegistry()
        reg.resolve_environment(
            "conv_cleanup",
            DEFAULT_ENVIRONMENT_ID,
            _agent_spec_with_sandbox_none(tmp_path / "conv_cleanup" / "workspace"),
        )
        from tests.runner.helpers import NullServerClient

        app = create_runner_app(
            resource_registry=reg,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://runner",
        ) as client:
            resp = await client.delete(
                "/v1/sessions/conv_cleanup/resources",
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "conv_cleanup"
        assert body["cleaned"] is True
        assert not reg.has_primary_env("conv_cleanup")
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


@pytest.mark.asyncio
async def test_cleanup_idempotent_for_unknown_session() -> None:
    """DELETE /v1/sessions/{id}/resources is safe for unknown sessions."""
    import httpx

    from omnigent.runner import create_runner_app
    from tests.runner.helpers import NullServerClient

    reg = SessionResourceRegistry()
    app = create_runner_app(
        resource_registry=reg,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runner",
    ) as client:
        resp = await client.delete(
            "/v1/sessions/conv_unknown/resources",
        )
    assert resp.status_code == 200
    assert resp.json()["cleaned"] is True


# ── os_env gate: list_resources ──────────────────────────────────────────────


def test_list_resources_suppresses_default_env_when_spec_has_no_os_env() -> None:
    """list_resources omits the default environment when agent_spec.os_env is None.

    Agents without an os_env block have no primary filesystem environment,
    so the resource listing must not advertise one.
    """

    reg = SessionResourceRegistry()
    spec = SimpleNamespace(os_env=None)

    page = reg.list_resources("conv_no_env", agent_spec=spec)

    ids = [r.id for r in page.data]
    # Default environment must be absent — the spec has no os_env so there
    # is no primary filesystem environment to expose.  If this assertion
    # fails, the gate was not applied and the UI would show a "Working
    # folder" panel that can never return any files.
    assert DEFAULT_ENVIRONMENT_ID not in ids, (
        f"Default environment should be suppressed when os_env is None, but found ids: {ids}"
    )


def test_list_resources_includes_default_env_when_spec_has_os_env(
    tmp_path: Path,
) -> None:
    """list_resources keeps the default environment when agent_spec.os_env is set."""
    reg = SessionResourceRegistry()
    spec = _agent_spec_with_sandbox_none(tmp_path / "workspace")

    page = reg.list_resources("conv_with_env", agent_spec=spec)

    ids = [r.id for r in page.data]
    # Default environment must be present — the spec has an os_env configured
    # so a primary filesystem environment exists and must be advertised.
    assert DEFAULT_ENVIRONMENT_ID in ids, (
        f"Default environment should be present when os_env is set, but found ids: {ids}"
    )


def test_list_resources_includes_default_env_when_no_spec() -> None:
    """list_resources preserves legacy behaviour when agent_spec is None.

    Callers that do not pass an agent_spec (dev/standalone mode) must still
    see the default environment so the filesystem API is usable.
    """
    reg = SessionResourceRegistry()

    page = reg.list_resources("conv_legacy", agent_spec=None)

    ids = [r.id for r in page.data]
    # agent_spec=None is the legacy path; default env must always be present
    # so pre-existing callers that do not supply a spec are unaffected.
    assert DEFAULT_ENVIRONMENT_ID in ids, (
        f"Default environment should be present when agent_spec=None, but found ids: {ids}"
    )


# ── os_env gate: resolve_environment ────────────────────────────────────────


def test_resolve_environment_raises_when_spec_has_no_os_env() -> None:
    """resolve_environment raises ValueError when agent_spec.os_env is None.

    The registry must not silently fall back to a synthetic default
    environment when the spec explicitly has no os_env configured —
    that would create an environment the agent cannot use.
    """
    reg = SessionResourceRegistry()
    spec = SimpleNamespace(os_env=None)

    with pytest.raises(ValueError, match="no os_env"):
        reg.resolve_environment("conv_no_env", DEFAULT_ENVIRONMENT_ID, spec)


# ── Runner workspace overrides agent spec cwd ────────────────────────────


def test_compute_default_env_root_runner_workspace_overrides_relative_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is set and the agent spec has a relative
    cwd (``"."``), the runner workspace wins.

    This is the common case for CLI-launched sessions: the user's
    terminal cwd flows through ``OMNIGENT_RUNNER_WORKSPACE`` and
    the agent's relative cwd resolves against it.
    """
    workspace = tmp_path / "user-project"
    workspace.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_rel", spec)

    assert root == str(workspace.resolve())


def test_compute_default_env_root_runner_workspace_overrides_absolute_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is set and the agent spec has an absolute
    cwd, the runner workspace STILL wins.

    This is the new contract under
    designs/SESSION_WORKSPACE_SELECTION.md: an absolute cwd in the
    spec is a session-create-time *boundary*, not a runtime
    override. Host-launched sessions pick a workspace inside the
    boundary and that pick — not the boundary itself — drives the
    runtime cwd. Without this rule, a user picking
    ``~/universe/src/foo`` for an agent declaring ``cwd: ~/universe``
    would be silently relocated up to ``~/universe``.
    """
    workspace = tmp_path / "picked-subdir"
    workspace.mkdir()
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_abs", spec)

    # Workspace wins, NOT spec_cwd.
    assert root == str(workspace.resolve())
    assert root != str(spec_cwd.resolve())


def test_compute_default_env_root_no_runner_workspace_uses_absolute_spec_cwd(
    tmp_path: Path,
) -> None:
    """
    When runner_workspace is NOT set, an absolute spec cwd is used.

    This pins the fallback path so unit tests / pure local runs
    that construct a spec directly without the env var keep
    working as before.
    """
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(runner_workspace=None)
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    root = reg.compute_default_env_root("conv_no_workspace", spec)

    assert root == str(spec_cwd.resolve())


def test_compute_default_env_root_no_os_env_returns_none(tmp_path: Path) -> None:
    """
    When the agent spec has no os_env, return None regardless of
    whether runner_workspace is set.

    Headless agents (no ``os_env``) intentionally don't expose the
    filesystem; the runner_workspace override must not bypass that
    gate. Without this check, host-launched headless agents would
    suddenly grow filesystem access.
    """
    reg = SessionResourceRegistry(
        runner_workspace=tmp_path,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(os_env=None)

    assert reg.compute_default_env_root("conv_headless", spec) is None


def test_resolve_environment_runner_workspace_overrides_absolute_spec_cwd(
    tmp_path: Path,
) -> None:
    """
    Materializing the primary OS environment uses runner_workspace
    over an absolute spec cwd.

    Pairs with the compute_default_env_root tests above to cover
    the eager creation path. _create_primary_env and
    compute_default_env_root must agree on cwd, otherwise the
    filesystem-list endpoint and the agent's actual cwd would
    drift apart.
    """
    workspace = tmp_path / "picked-subdir"
    workspace.mkdir()
    spec_cwd = tmp_path / "agent-spec-cwd"
    spec_cwd.mkdir()
    reg = SessionResourceRegistry(
        runner_workspace=workspace,
        per_session_workspace=False,
    )
    spec = SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(spec_cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        )
    )

    env = reg.resolve_environment("conv_abs_eager", DEFAULT_ENVIRONMENT_ID, spec)

    assert env is not None
    # Compare via realpath because tmp_path on macOS goes through
    # /var → /private/var symlinks.
    assert os.path.realpath(env.cwd) == os.path.realpath(workspace)


@pytest.mark.asyncio
async def test_blocked_reason_survives_pane_redraws(tmp_path: Path) -> None:
    """The parked reason survives the pane redrawing underneath the dialog.

    Claude reports ``waitingFor`` once, when the dialog opens, and the pane
    keeps redrawing while it is up. Because the file owns status outright the
    pane publishes nothing, so there is no bare ``running`` to erase the reason
    — it stands until the file itself drops it.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("claude", "main", tmp_path)
    terminal_registry._by_conversation.setdefault("conv_reason", {})[("claude", "main")] = instance
    edges: list[tuple[str, str | None]] = []
    pollers: list[_FakeStatusPoller] = []
    registry.set_session_status_publisher(
        lambda _sid, status, reason=None: edges.append((status, reason))
    )

    def _fake_build(*, session_id: str, instance: object, on_status: object) -> _FakeStatusPoller:
        del session_id, instance
        poller = _FakeStatusPoller(on_status)
        pollers.append(poller)
        return poller

    registry._build_claude_native_status_poller = _fake_build  # type: ignore[method-assign]

    callbacks: dict[str, object] = {}

    def _capture_watcher(
        on_idle: object | None = None,
        *,
        on_activity: object | None = None,
        on_exit: object | None = None,
        on_tick: object | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        replace: bool = False,
    ) -> None:
        del idle_threshold_s, poll_interval_s, replace
        callbacks["on_idle"] = on_idle
        callbacks["on_activity"] = on_activity
        callbacks["on_exit"] = on_exit
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[attr-defined]
    await registry.observe_required_terminal(
        "conv_reason", "claude", "main", instance, resource_role=CLAUDE_NATIVE_TERMINAL_ROLE
    )

    poller = pollers[0]
    poller.active = True
    poller.blocked_on = "permission prompt"

    poller.emit("running", "permission prompt")
    callbacks["on_activity"]()  # pane redraw under the dialog
    callbacks["on_idle"]()  # and the quiet spells between redraws
    await asyncio.sleep(0)
    assert edges == [("running", "permission prompt")]

    # Dialog answered: the file drops the reason on its own edge.
    poller.blocked_on = None
    poller.emit("running", None)
    await asyncio.sleep(0)
    assert edges == [("running", "permission prompt"), ("running", None)]


@pytest.mark.parametrize(
    "raw,expected,why",
    [
        ("conv_abc123", "conv_abc123", "an ordinary id is untouched"),
        ("a" * 32, "a" * 32, "a uuid4().hex id is untouched"),
        ("a/b", "a_b", "a POSIX separator cannot survive"),
        ("a\\b", "a_b", "a Windows separator cannot survive either"),
        ("../..", ".._..", "separators go, leaving no traversal component"),
        ("..", "__", "a bare parent reference never survives"),
        (".", "_", "a bare self reference never survives"),
        ("", "_", "an empty id still yields a usable component"),
        ("a\x00b", "a_b", "a NUL cannot reach os.path.join"),
        ("a b", "a_b", "whitespace is normalized rather than quoted downstream"),
    ],
)
def test_sanitize_session_id_yields_one_safe_component(raw: str, expected: str, why: str) -> None:
    """``_sanitize_session_id`` must return a single, non-traversing path component.

    The id reaches the filesystem as a directory name under the runner
    workspace, so anything that could act as a separator or a parent reference
    has to be neutralized here. Uses an allowlist: the previous denylist
    stopped ``/`` and ``..`` but let a backslash through.
    """
    got = _sanitize_session_id(raw)

    assert got == expected, why
    # The invariants that actually matter, restated independently of the
    # table above so a wrong `expected` cannot make this vacuous.
    assert got, "must never be empty — it becomes a path component"
    assert "/" not in got and "\\" not in got, "must be a single component"
    assert set(got) != {"."}, "must not be '.' or '..'"


def test_sanitize_session_id_keeps_traversal_out_of_the_workspace_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hostile id cannot walk the session workspace out of the runner root.

    The unit test above pins the component; this pins the property callers
    actually depend on — that the joined path stays under the root.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))

    resolved = Path(_session_workspace("../../../../etc")).resolve()

    assert resolved.is_relative_to(tmp_path.resolve()), f"escaped the root: {resolved}"


# ── native bridge-dir reaping: live-session regression ──────────────────────


@pytest.mark.asyncio
async def test_cleanup_session_preserves_live_native_bridge_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cleanup_session must NOT delete a live session's native bridge dir.

    Bridge-dir deletion is deliberately kept OUT of cleanup_session because
    the in-place agent-switch reset (reset_session_state) reuses
    cleanup_session while the session — and its bridge — lives on. Wiring a
    reap in here would rmtree a live session's ``bridge.json`` +
    ``permission_hook.json`` and break approval routing until cold launch.
    This guard fails if any such deletion is ever wired back in.

    :param tmp_path: Pytest temp dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import omnigent.harnesses.claude_native.bridge as claude_bridge

    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "claude-native")
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)

    # A live session's bridge dir: current-process owner.pid + the token and
    # permission-hook files that must survive an agent-switch reset.
    bridge_dir = claude_bridge.prepare_bridge_dir("conv_live", workspace=tmp_path)
    permission_hook = bridge_dir / "permission_hook.json"
    permission_hook.write_text("{}", encoding="utf-8")
    assert (bridge_dir / "bridge.json").exists()
    assert (bridge_dir / "owner.pid").exists()

    registry = SessionResourceRegistry()
    await registry.cleanup_session("conv_live")

    assert bridge_dir.exists()
    assert (bridge_dir / "bridge.json").exists()
    assert permission_hook.exists()


@pytest.mark.asyncio
async def test_claude_native_logs_input_ready_once_from_existing_snapshot(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "parent")
    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    callbacks, _, pollers, registry = await _observe_native_with_fake_poller(tmp_path, "child")
    instance = registry.terminal_registry.get("child", "claude", "main")
    assert instance is not None
    on_tick = callbacks["on_tick"]
    assert callable(on_tick)
    with monkeypatch.context() as patch:
        patch.setattr(
            "omnigent.harnesses.claude_native.bridge.claude_pane_text_ready",
            Mock(side_effect=RuntimeError("readiness observation failed")),
        )
        watcher_continues = instance._fire_watch_callback(on_tick, "tick")
        assert watcher_continues
        assert pollers[0].ticks == 1
    instance._remember_pane_snapshot("Sign in to Claude")
    on_tick()
    assert not any(getattr(r, "event_name", None) == "native_input_ready" for r in caplog.records)
    instance._remember_pane_snapshot("────────────────────\n❯ \n────────────────────")
    on_tick()
    on_tick()
    events = [r for r in caplog.records if getattr(r, "event_name", None) == "native_input_ready"]
    assert len(events) == 1
    assert events[0].session_id == "child"
    assert events[0].attributes["harness"] == "claude-native"
    assert events[0].attributes["terminal_instance_id"] == instance.diagnostic_id


_AUTO_MODE_BILLING_NOTICE_PANE = """────────────────────────────────────────
  We're changing auto mode to no longer charge for classifier requests in Claude Code.
  However, this session isn't eligible because your requests go through
  gateway.example.com, which isn't compatible with this update.
  Nothing breaks: auto mode keeps working, and its classifier requests are billed as before.
  To fix it and access the new version of auto mode, ask your gateway to implement:
  https://code.claude.com/docs/en/auto-mode-classifier-billing
  Enter to continue · Esc to cancel
"""


async def _observe_native_capturing_tick(
    tmp_path: Path,
    session_id: str,
    terminal_name: str,
    resource_role: str,
    env: dict[str, str],
) -> tuple[object, TerminalInstance]:
    """Observe a native terminal and return its watcher ``on_tick`` callback.

    :returns: ``(on_tick, instance)`` for driving the input-ready probe.
    """
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    registry.set_session_status_publisher(lambda *_args: None)
    registry.set_terminal_exit_publisher(lambda *_args: None)
    instance = make_test_terminal_instance(terminal_name, "main", tmp_path)
    instance.env = env
    terminal_registry._by_conversation.setdefault(session_id, {})[(terminal_name, "main")] = (
        instance
    )
    callbacks: dict[str, object] = {}

    def _capture_watcher(*_args: object, on_tick: object | None = None, **_kwargs: object) -> None:
        callbacks["on_tick"] = on_tick

    instance.start_idle_watcher_thread = _capture_watcher  # type: ignore[method-assign]
    await registry.observe_required_terminal(
        session_id, terminal_name, "main", instance, resource_role=resource_role
    )
    assert callable(callbacks["on_tick"])
    return callbacks["on_tick"], instance


def _input_ready_events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == "native_input_ready"]


@pytest.mark.asyncio
async def test_pi_native_logs_input_ready_once_extension_marks_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from omnigent.harnesses.pi_native import bridge as pi_bridge

    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    bridge_dir = tmp_path / "pi-bridge"
    bridge_dir.mkdir()
    # A marker from the previous Pi process must not survive the relaunch.
    (bridge_dir / "input_ready").write_text("", encoding="utf-8")
    _extension, config = pi_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_pi",
        server_url="http://ap.example",
        conversation_url="http://ap.example/c/conv_pi",
    )
    on_tick, instance = await _observe_native_capturing_tick(
        tmp_path,
        "conv_pi",
        "pi",
        PI_NATIVE_TERMINAL_ROLE,
        {pi_bridge.PI_NATIVE_CONFIG_ENV_VAR: str(config)},
    )

    on_tick()  # type: ignore[operator]
    assert _input_ready_events(caplog) == []

    # What the extension's session_start writes once its inbox poller is armed.
    (bridge_dir / "input_ready").write_text("", encoding="utf-8")
    on_tick()  # type: ignore[operator]
    on_tick()  # type: ignore[operator]

    events = _input_ready_events(caplog)
    assert len(events) == 1
    assert events[0].session_id == "conv_pi"
    assert events[0].attributes["harness"] == "pi-native"
    assert events[0].attributes["terminal_instance_id"] == instance.diagnostic_id


@pytest.mark.asyncio
async def test_codex_native_logs_input_ready_once_thread_is_bound_to_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from omnigent.harnesses.codex_native import bridge as codex_bridge

    caplog.set_level(logging.INFO, logger="omnigent.runner.resource_registry")
    bridge_dir = tmp_path / "codex-bridge"
    bridge_dir.mkdir()
    codex_home = codex_bridge.codex_home_for_bridge_dir(bridge_dir)

    def _bind(session_id: str) -> None:
        codex_bridge.write_bridge_state(
            bridge_dir,
            codex_bridge.CodexNativeBridgeState(
                session_id=session_id,
                socket_path="ws://127.0.0.1:1",
                thread_id=f"thread_{session_id}",
                codex_home=str(codex_home),
            ),
        )

    # Thread-switch shape: the terminal moved to conv_new before the forwarder
    # rebinds bridge state, so conv_old's thread must not count as ready.
    _bind("conv_old")
    on_tick, _instance = await _observe_native_capturing_tick(
        tmp_path,
        "conv_new",
        "codex",
        CODEX_NATIVE_TERMINAL_ROLE,
        {"CODEX_HOME": str(codex_home)},
    )
    on_tick()  # type: ignore[operator]
    assert _input_ready_events(caplog) == []

    _bind("conv_new")
    on_tick()  # type: ignore[operator]
    on_tick()  # type: ignore[operator]

    events = _input_ready_events(caplog)
    assert len(events) == 1
    assert events[0].session_id == "conv_new"
    assert events[0].attributes["harness"] == "codex-native"


_CLAUDE_READY_PANE = "────────────────────\n❯ \n────────────────────"


@pytest.mark.asyncio
async def test_claude_native_acknowledges_billing_notice_after_input_was_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.harnesses.claude_native.bridge as claude_bridge

    callbacks, _, pollers, registry = await _observe_native_with_fake_poller(tmp_path, "child")
    instance = registry.terminal_registry.get("child", "claude", "main")
    assert instance is not None
    on_tick = callbacks["on_tick"]
    assert callable(on_tick)
    bridge_dir = tmp_path / "child-bridge"
    resolve_bridge = Mock(return_value=bridge_dir)
    acknowledge = Mock(return_value=False)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_conversation_id", resolve_bridge)
    monkeypatch.setattr(claude_bridge, "acknowledge_auto_mode_billing_notice", acknowledge)
    send_keys = Mock(side_effect=AssertionError("watcher must delegate guarded input"))
    monkeypatch.setattr(claude_bridge, "_run_tmux", send_keys)

    instance._remember_pane_snapshot(_CLAUDE_READY_PANE)
    on_tick()
    resolve_bridge.assert_not_called()
    acknowledge.assert_not_called()

    # A mid-turn notice still gets checked after the one-time readiness edge.
    # Repeated cached matches may already be stale; the bridge rechecks them.
    instance._remember_pane_snapshot(_AUTO_MODE_BILLING_NOTICE_PANE)
    on_tick()
    on_tick()

    assert acknowledge.call_count == 2
    acknowledge.assert_called_with(
        bridge_dir,
        expected_socket_path=str(instance.socket_path),
        expected_tmux_target=instance.tmux_target,
    )
    resolve_bridge.assert_called_with("child")
    send_keys.assert_not_called()
    assert pollers[0].ticks == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("transfer", [False, True])
@pytest.mark.parametrize("replace_during_tick", [False, True])
async def test_claude_native_billing_acknowledgement_uses_original_launch_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transfer: bool,
    replace_during_tick: bool,
) -> None:
    import omnigent.harnesses.claude_native.bridge as claude_bridge

    callbacks, _, pollers, registry = await _observe_native_with_fake_poller(tmp_path, "source")
    instance = registry.terminal_registry.get("source", "claude", "main")
    assert instance is not None
    original_bridge = tmp_path / "cli-direct-bridge"
    instance.args = ["--settings", str(original_bridge / "claude-settings.json")]
    resolve_conversation_bridge = Mock(side_effect=AssertionError("wrong bridge identity"))
    acknowledge = Mock(return_value=True)
    monkeypatch.setattr(
        claude_bridge, "bridge_dir_for_conversation_id", resolve_conversation_bridge
    )
    monkeypatch.setattr(claude_bridge, "acknowledge_auto_mode_billing_notice", acknowledge)

    if transfer:
        moved = await registry.transfer_terminal("source", "target", "terminal_claude_main")
        assert moved is not None
        assert registry.terminal_registry.get("target", "claude", "main") is instance
    on_tick = callbacks["on_tick"]
    assert callable(on_tick)
    if replace_during_tick:
        poller = pollers[-1]
        original_tick = poller.tick

        def replace_after_read() -> None:
            original_tick()
            registry.terminal_registry._by_conversation["target" if transfer else "source"][
                ("claude", "main")
            ] = make_test_terminal_instance("claude", "main", tmp_path / "replacement")

        monkeypatch.setattr(poller, "tick", replace_after_read)
    instance._remember_pane_snapshot(_AUTO_MODE_BILLING_NOTICE_PANE)
    on_tick()

    if replace_during_tick:
        acknowledge.assert_not_called()
        resolve_conversation_bridge.assert_not_called()
        assert poller.ticks == 1
        return
    acknowledge.assert_called_once_with(
        original_bridge,
        expected_socket_path=str(instance.socket_path),
        expected_tmux_target=instance.tmux_target,
    )
    resolve_conversation_bridge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pane",
    [
        "",
        _CLAUDE_READY_PANE,
        "────────────────────\nDo you want to run this command?\nEnter to select · Esc to cancel",
        _AUTO_MODE_BILLING_NOTICE_PANE + _CLAUDE_READY_PANE,
    ],
)
async def test_claude_native_ignores_other_panes_without_attempting_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pane: str,
) -> None:
    import omnigent.harnesses.claude_native.bridge as claude_bridge

    callbacks, _, _, registry = await _observe_native_with_fake_poller(tmp_path, "child")
    instance = registry.terminal_registry.get("child", "claude", "main")
    assert instance is not None
    on_tick = callbacks["on_tick"]
    assert callable(on_tick)
    resolve_bridge = Mock()
    acknowledge = Mock()
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_conversation_id", resolve_bridge)
    monkeypatch.setattr(claude_bridge, "acknowledge_auto_mode_billing_notice", acknowledge)

    instance._remember_pane_snapshot(pane)
    on_tick()

    resolve_bridge.assert_not_called()
    acknowledge.assert_not_called()


@pytest.mark.asyncio
async def test_claude_native_billing_acknowledgement_error_preserves_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import omnigent.harnesses.claude_native.bridge as claude_bridge

    callbacks, _, pollers, registry = await _observe_native_with_fake_poller(tmp_path, "child")
    instance = registry.terminal_registry.get("child", "claude", "main")
    assert instance is not None
    on_tick = callbacks["on_tick"]
    assert callable(on_tick)
    acknowledge = Mock(side_effect=RuntimeError("capture unavailable"))
    monkeypatch.setattr(claude_bridge, "acknowledge_auto_mode_billing_notice", acknowledge)
    caplog.set_level(logging.DEBUG, logger="omnigent.runner.resource_registry")

    instance._remember_pane_snapshot(_AUTO_MODE_BILLING_NOTICE_PANE)
    assert instance._fire_watch_callback(on_tick, "tick")
    instance._remember_pane_snapshot(_CLAUDE_READY_PANE)
    assert instance._fire_watch_callback(on_tick, "tick")

    assert pollers[0].ticks == 2
    acknowledge.assert_called_once()
    failure = next(
        r for r in caplog.records if "billing notice acknowledgement failed" in r.message
    )
    assert failure.session_id == "child"
    assert any(getattr(r, "event_name", None) == "native_input_ready" for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [None, PI_NATIVE_TERMINAL_ROLE])
async def test_non_claude_terminal_never_acknowledges_billing_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
) -> None:
    import omnigent.harnesses.claude_native.bridge as claude_bridge

    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = make_test_terminal_instance("shell", "main", tmp_path)
    instance._remember_pane_snapshot(_AUTO_MODE_BILLING_NOTICE_PANE)
    start_watcher = Mock()
    monkeypatch.setattr(instance, "start_idle_watcher_thread", start_watcher)
    acknowledge = Mock()
    monkeypatch.setattr(claude_bridge, "acknowledge_auto_mode_billing_notice", acknowledge)
    registry.set_session_status_publisher(lambda _sid, _status, _reason=None: None)
    registry.set_terminal_activity_publisher(lambda _sid, _rid: None)

    await registry.observe_required_terminal(
        "child", "shell", "main", instance, resource_role=role
    )
    start_watcher.assert_called_once()
    on_tick = start_watcher.call_args.kwargs.get("on_tick")
    if on_tick is not None:
        on_tick()

    acknowledge.assert_not_called()
