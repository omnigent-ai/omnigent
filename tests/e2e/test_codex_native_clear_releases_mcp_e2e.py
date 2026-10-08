"""Codex-native /clear must release the retired thread's MCP processes: the
forwarder keeps its thread/resume subscription, so each /clear leaks a generation
of stdio MCP wrappers, and rotation leaves the runner registries on the old id."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
import yaml

from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.codex_native.app_server import CodexAppServerClient
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    bridge_dir_for_bridge_id,
    read_bridge_state,
)
from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e import conftest as e2e_conftest
from tests.e2e.conftest import (
    configure_mock_llm,
    restart_live_runner_process,
    set_fallback_mock_llm,
)
from tests.e2e.test_codex_native_terminal_recovery_e2e import _app_server_pid, _tmux, _wait_for
from tests.e2e.test_host_codex_native_e2e import (
    _poll_for_assistant_marker,
    _send_user_text,
    _wait_for_codex_turn_idle,
)

_SYSTEM_CODEX_CONFIG = Path("/etc/codex/managed_config.toml")
_STUB_MODULE = Path(__file__).resolve().parent / "_mcp_stub_server.py"
_STUB_NAME = _STUB_MODULE.name
_STUB_SERVERS = ("stub_a", "stub_b")
# Codex unloads an idle, unsubscribed thread about 60s after its last subscriber leaves.
_RELEASE_WINDOW_S = 120.0
_TEARDOWN_WINDOW_S = 30.0
_TUI_READY_TIMEOUT_S = 120.0
# Codex renders a randomised placeholder, so readiness is the composer prompt itself.
_COMPOSER_PROMPTS = ("›", "»")
_COMPOSER_DISABLED = {"Input disabled.", "Shutting down...", "Answer the questions to continue."}

# A process identity: pid plus creation time, so a reused pid is never mistaken
# for the process that held it.
_ProcessId = tuple[int, float]


def _system_codex_config_in_use() -> bool:
    try:
        return bool(_SYSTEM_CODEX_CONFIG.read_text().strip())
    except FileNotFoundError:
        return False
    except OSError:
        return True


pytestmark = [
    pytest.mark.skipif(
        shutil.which("codex") is None or shutil.which("tmux") is None,
        reason="requires real Codex and tmux binaries",
    ),
    pytest.mark.skipif(
        _system_codex_config_in_use(),
        reason="requires isolated Codex system config to keep model requests local",
    ),
]


def _write_stub_mcp_config(codex_home: Path) -> Path:
    """Write a Codex config launching the stub MCP servers; return their shared log."""
    log = codex_home / "mcp_stub.log"
    log.write_text("")
    python = json.dumps(sys.executable)
    sections = [
        f"[mcp_servers.{name}]\n"
        f"command = {python}\n"
        f"args = [{json.dumps(str(_STUB_MODULE))}, {json.dumps(name)}, {json.dumps(str(log))}]\n"
        for name in _STUB_SERVERS
    ]
    (codex_home / "config.toml").write_text("\n".join(sections))
    return log


@pytest.fixture(scope="module")
def stub_mcp_log(
    live_server: str, live_runner_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """Run the live runner with a ``CODEX_HOME`` that launches the stub MCP servers.
    The runner copies the host ``CODEX_HOME`` config into each session, so it is restarted
    with the stub config and restored afterwards to keep the override out of other modules."""
    codex_home = tmp_path_factory.mktemp("codex-home")
    log = _write_stub_mcp_config(codex_home)
    runner_env = e2e_conftest._live_runner_state["env"]
    previous = runner_env.get("CODEX_HOME")
    try:
        runner_env["CODEX_HOME"] = str(codex_home)
        restart_live_runner_process(live_server, live_runner_id)
        yield log
    finally:
        if previous is None:
            runner_env.pop("CODEX_HOME", None)
        else:
            runner_env["CODEX_HOME"] = previous
        restart_live_runner_process(live_server, live_runner_id)


def _alive(identity: _ProcessId) -> bool:
    pid, created = identity
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        proc = psutil.Process(pid)
        return proc.create_time() == created and proc.status() != psutil.STATUS_ZOMBIE
    return False


def _mcp_children(app: _ProcessId | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if app is None or not _alive(app):
        return rows
    for child in psutil.Process(app[0]).children(recursive=True):
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            cmdline = " ".join(child.cmdline())
            if _STUB_NAME in cmdline:
                kind = "stub"
            elif "serve-mcp" in cmdline:
                kind = "omnigent-mcp"
            else:
                continue
            rows.append(
                {
                    "pid": child.pid,
                    "pgid": os.getpgid(child.pid),
                    "kind": kind,
                    "created": child.create_time(),
                }
            )
    return rows


def _stubs(app: _ProcessId | None) -> set[_ProcessId]:
    return {(row["pid"], row["created"]) for row in _mcp_children(app) if row["kind"] == "stub"}


def _loaded_threads(ws_url: str) -> list[str]:
    async def probe() -> list[str]:
        client = CodexAppServerClient(ws_url=ws_url, client_name="omnigent-e2e-probe")
        await client.connect()
        try:
            response = await client.request("thread/loaded/list", {})
        finally:
            with contextlib.suppress(Exception):
                await client.close()
        return list(response.get("result", {}).get("data", []))

    return asyncio.run(probe())


def _runner_log_lines(needles: tuple[str, ...]) -> list[str]:
    handle = e2e_conftest._live_runner_state.get("log_handle")
    path = Path(getattr(handle, "name", ""))
    if not path.is_file():
        return []
    return [
        line[:300]
        for line in path.read_text(errors="replace").splitlines()
        if any(n in line for n in needles)
    ]


class _Journey:
    """One codex-native session: TUI in tmux, stub MCP servers, census helpers."""

    def __init__(
        self, client: httpx.Client, mock_url: str, workspace: Path, stub_log: Path
    ) -> None:
        self.client = client
        self.mock_url = mock_url
        self.workspace = workspace
        self.stub_log = stub_log
        self.model = f"mock-codex-clear-{uuid.uuid4().hex[:8]}"
        self.session_ids: list[str] = []
        self.evidence: dict[str, Any] = {"model": self.model, "steps": []}
        self.app: _ProcessId | None = None
        self.bridge_dir: Path | None = None
        self.ws_url = ""
        self.socket = ""
        self.target = ""

    def start(self, runner_id: str) -> None:
        spec = yaml.safe_load(
            _materialize_codex_agent_spec(self.workspace, model=self.model).read_text()
        )
        spec["name"] = f"codex-clear-{uuid.uuid4().hex[:8]}"
        spec["executor"]["auth"] = {
            "type": "api_key",
            "api_key": "mock-key",
            "base_url": f"{self.mock_url}/v1",
        }
        spec["spawn"] = False
        spec["os_env"]["cwd"] = str(self.workspace)
        create = post_session_bundle(
            self.client.post,
            "/v1/sessions",
            bundle_files({"codex-native-ui.yaml": yaml.safe_dump(spec).encode()}),
            metadata={
                "workspace": str(self.workspace),
                "labels": {
                    WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
                    UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                },
            },
        )
        assert create.is_success, create.text
        session_id = create.json()["session_id"]
        self.session_ids.append(session_id)
        self.bridge_dir = bridge_dir_for_bridge_id(session_id)
        binding = self.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
        assert binding.is_success, binding.text
        terminal = self.client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            timeout=120,
        )
        assert terminal.status_code == 200, terminal.text[:1000]
        metadata = terminal.json()["metadata"]
        self.socket, self.target = metadata["tmux_socket"], metadata["tmux_target"]
        state = _wait_for(
            lambda: read_bridge_state(self.bridge_dir), "Codex thread creation", timeout=120
        )
        app_pid = _app_server_pid(state.socket_path)
        self.app = (app_pid, psutil.Process(app_pid).create_time())
        self.ws_url = state.socket_path
        _wait_for(
            lambda: len(_stubs(self.app)) >= len(_STUB_SERVERS),
            "stub MCP wrappers",
            timeout=60,
        )
        self.wait_tui_interactive(timeout=_TUI_READY_TIMEOUT_S)
        set_fallback_mock_llm(self.mock_url, self.model, "MOCK_FALLBACK")
        self.record(
            "start",
            session_id=session_id,
            thread_id=state.thread_id,
            app_server_pid=app_pid,
        )

    @property
    def state(self) -> CodexNativeBridgeState:
        assert self.bridge_dir is not None, "journey not started"
        state = read_bridge_state(self.bridge_dir)
        assert state is not None
        return state

    def record(self, step: str, **fields: Any) -> None:
        fields["mcp_children"] = _mcp_children(self.app)
        if self.ws_url:
            with contextlib.suppress(Exception):
                fields["loaded_threads"] = _loaded_threads(self.ws_url)
        self.evidence["steps"].append({"step": step, "t": time.time(), **fields})

    def pane(self) -> str:
        return _tmux(self.socket, "capture-pane", "-p", "-t", self.target, "-S", "-80")

    def wait_pane_text(self, text: str, timeout: float = 60.0) -> None:
        self._wait_pane(lambda pane: text in pane, f"display {text!r}", timeout)

    def wait_tui_interactive(self, timeout: float) -> None:
        def accepting_input(pane: str) -> bool:
            composers = [
                line.lstrip()[1:].strip()
                for line in pane.splitlines()
                if line.lstrip().startswith(_COMPOSER_PROMPTS)
            ]
            return bool(composers) and composers[-1] not in _COMPOSER_DISABLED

        self._wait_pane(accepting_input, "show an enabled composer", timeout)

    def _wait_pane(self, check: Callable[[str], bool], what: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = self.pane()
            if check(pane):
                return
            time.sleep(0.2)
        raise AssertionError(
            f"Timed out waiting for the TUI to {what} after {timeout:.0f}s; pane:\n{pane[-1500:]}"
        )

    def run_turn(self) -> str:
        marker = f"READY_{uuid.uuid4().hex[:8]}"
        configure_mock_llm(self.mock_url, [{"text": marker}] * 3, key=self.model)
        session_id = self.state.session_id
        _send_user_text(self.client, session_id=session_id, text="Reply with the ready marker")
        _poll_for_assistant_marker(self.client, session_id=session_id, marker=marker, timeout=90)
        _wait_for_codex_turn_idle(
            self.client, session_id=session_id, bridge_dir=self.bridge_dir, timeout=60
        )
        self.wait_pane_text(marker)
        self.record("turn", marker=marker)
        return marker

    def clear(self) -> CodexNativeBridgeState:
        before = self.state
        known = _stubs(self.app)
        _tmux(self.socket, "send-keys", "-t", self.target, "/clear")
        time.sleep(0.5)
        _tmux(self.socket, "send-keys", "-t", self.target, "Enter")

        def rotated() -> CodexNativeBridgeState | None:
            state = read_bridge_state(self.bridge_dir)
            return state if state is not None and state.thread_id != before.thread_id else None

        after = _wait_for(rotated, "forwarder rotation onto the new Codex thread", timeout=60)
        if after.session_id not in self.session_ids:
            self.session_ids.append(after.session_id)
        _wait_for(
            lambda: len(_stubs(self.app) - known) >= len(_STUB_SERVERS),
            "the new thread's stub MCP wrappers",
            timeout=60,
        )
        self.record(
            "clear",
            old_thread=before.thread_id,
            new_thread=after.thread_id,
            new_session=after.session_id,
        )
        return after

    def wait_stubs_gone(self, stubs: set[_ProcessId], timeout: float) -> set[_ProcessId]:
        deadline = time.monotonic() + timeout
        remaining = {stub for stub in stubs if _alive(stub)}
        while remaining and time.monotonic() < deadline:
            time.sleep(1.0)
            remaining = {stub for stub in stubs if _alive(stub)}
        self.record(
            "release_window",
            waited_s=round(timeout - max(0.0, deadline - time.monotonic()), 1),
            remaining=sorted(remaining),
        )
        return remaining

    def wait_app_server_gone(self, timeout: float) -> bool:
        assert self.app is not None, "journey not started"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _alive(self.app):
            time.sleep(0.5)
        self.record("teardown_window", app_server_alive=_alive(self.app))
        return not _alive(self.app)

    def finish(self) -> None:
        pane_text = ""
        if self.socket:
            with contextlib.suppress(Exception):
                pane_text = self.pane()[-3000:]
        self.evidence["pane"] = pane_text
        self.evidence["runner_log"] = _runner_log_lines(
            (
                *self.session_ids,
                "rotated Omnigent session",
                "Codex native input stopped",
                "did not finish",
            )
        )
        with contextlib.suppress(OSError):
            self.evidence["stub_log"] = [
                json.loads(line) for line in self.stub_log.read_text().splitlines() if line.strip()
            ]
        (self.workspace / "evidence.json").write_text(
            json.dumps(self.evidence, indent=1, default=str)
        )
        print(
            json.dumps(
                {k: v for k, v in self.evidence.items() if k != "pane"}, indent=1, default=str
            )
        )
        for session_id in self.session_ids:
            with contextlib.suppress(Exception):
                self.client.delete(f"/v1/sessions/{session_id}", timeout=30)
        leftovers = {(row["pid"], row["created"]) for row in _mcp_children(self.app)}
        if self.app is not None:
            leftovers.add(self.app)
        for identity in sorted(leftovers, reverse=True):
            if _alive(identity):
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    psutil.Process(identity[0]).kill()


@pytest.fixture
def journey(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    stub_mcp_log: Path,
    tmp_path: Path,
) -> Iterator[_Journey]:
    if mock_llm_server_url is None:
        pytest.skip("requires the local mock model endpoint")
    started = _Journey(http_client, mock_llm_server_url, tmp_path, stub_mcp_log)
    try:
        started.start(live_runner_id)
    except BaseException:
        # A partial start still owns a session, pane and app-server on the shared runner.
        with contextlib.suppress(Exception):
            started.finish()
        raise
    yield started
    started.finish()


def test_clear_releases_previous_thread_mcp_processes(journey: _Journey) -> None:
    """Both retired threads' stub MCP wrappers exit after two ``/clear`` rotations."""
    journey.run_turn()
    generations = [_stubs(journey.app)]
    for _ in range(2):
        journey.clear()
        generations.append(_stubs(journey.app) - set().union(*generations))
    retired = set().union(*generations[:-1])
    remaining = journey.wait_stubs_gone(retired, timeout=_RELEASE_WINDOW_S)
    assert not remaining, (
        f"{len(remaining)} stub MCP wrapper(s) from the two retired Codex "
        f"threads are still running {_RELEASE_WINDOW_S:.0f}s after /clear "
        f"(pids {sorted(pid for pid, _ in remaining)}; "
        f"generations {[sorted(pid for pid, _ in g) for g in generations]}; "
        f"threads still loaded in the app server: {_loaded_threads(journey.ws_url)})"
    )


def test_deleting_rotated_session_closes_app_server(journey: _Journey) -> None:
    """Deleting the session that owns the terminal after ``/clear`` stops its app server."""
    journey.run_turn()
    rotated = journey.clear()
    delete = journey.client.delete(f"/v1/sessions/{rotated.session_id}", timeout=60)
    assert delete.status_code == 200, delete.text[:500]
    assert journey.wait_app_server_gone(_TEARDOWN_WINDOW_S), (
        f"codex app-server {journey.app} is still running "
        f"{_TEARDOWN_WINDOW_S:.0f}s after deleting the rotated session "
        f"{rotated.session_id}; MCP wrappers alive: {_mcp_children(journey.app)}"
    )


def test_clear_before_first_turn_releases_mcp_processes(journey: _Journey) -> None:
    """Without a prior turn the forwarder never subscribed, so Codex retires the old thread."""
    first_generation = _stubs(journey.app)
    journey.clear()
    remaining = journey.wait_stubs_gone(first_generation, timeout=_RELEASE_WINDOW_S)
    assert not remaining, (
        f"stub MCP wrappers {sorted(pid for pid, _ in remaining)} survived a /clear "
        "issued before any turn"
    )
