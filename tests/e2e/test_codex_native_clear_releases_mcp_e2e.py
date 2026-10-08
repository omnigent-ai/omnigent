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
from collections.abc import Iterator
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

pytestmark = [
    pytest.mark.skipif(
        shutil.which("codex") is None or shutil.which("tmux") is None,
        reason="requires real Codex and tmux binaries",
    ),
    pytest.mark.skipif(
        _SYSTEM_CODEX_CONFIG.exists() and bool(_SYSTEM_CODEX_CONFIG.read_text().strip()),
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

    The runner copies the host ``CODEX_HOME`` config into each session, so it is
    restarted with the stub config and restarted again with its previous
    environment afterwards, keeping the override out of other e2e modules.
    """
    codex_home = tmp_path_factory.mktemp("codex-home")
    log = _write_stub_mcp_config(codex_home)
    runner_env = e2e_conftest._live_runner_state["env"]
    previous = runner_env.get("CODEX_HOME")
    runner_env["CODEX_HOME"] = str(codex_home)
    restart_live_runner_process(live_server, live_runner_id)
    try:
        yield log
    finally:
        if previous is None:
            runner_env.pop("CODEX_HOME", None)
        else:
            runner_env["CODEX_HOME"] = previous
        restart_live_runner_process(live_server, live_runner_id)


def _alive(pid: int) -> bool:
    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    return False


def _mcp_children(app_pid: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not _alive(app_pid):
        return rows
    for child in psutil.Process(app_pid).children(recursive=True):
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
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


def _stub_pids(app_pid: int) -> set[int]:
    return {row["pid"] for row in _mcp_children(app_pid) if row["kind"] == "stub"}


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
        self.app_pid = 0
        self.socket = ""
        self.target = ""

    @classmethod
    def start(
        cls,
        client: httpx.Client,
        runner_id: str,
        mock_url: str,
        workspace: Path,
        stub_log: Path,
    ) -> _Journey:
        journey = cls(client, mock_url, workspace, stub_log)
        spec = yaml.safe_load(
            _materialize_codex_agent_spec(workspace, model=journey.model).read_text()
        )
        spec["name"] = f"codex-clear-{uuid.uuid4().hex[:8]}"
        spec["executor"]["auth"] = {
            "type": "api_key",
            "api_key": "mock-key",
            "base_url": f"{mock_url}/v1",
        }
        spec["spawn"] = False
        spec["os_env"]["cwd"] = str(workspace)
        create = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"codex-native-ui.yaml": yaml.safe_dump(spec).encode()}),
            metadata={
                "workspace": str(workspace),
                "labels": {
                    WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
                    UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                },
            },
        )
        assert create.is_success, create.text
        session_id = create.json()["session_id"]
        journey.session_ids.append(session_id)
        journey.bridge_dir = bridge_dir_for_bridge_id(session_id)
        binding = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
        assert binding.is_success, binding.text
        terminal = client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            timeout=120,
        )
        assert terminal.status_code == 200, terminal.text[:1000]
        metadata = terminal.json()["metadata"]
        journey.socket, journey.target = metadata["tmux_socket"], metadata["tmux_target"]
        state = _wait_for(
            lambda: read_bridge_state(journey.bridge_dir), "Codex thread creation", timeout=120
        )
        journey.app_pid = _app_server_pid(state.socket_path)
        journey.ws_url = state.socket_path
        _wait_for(
            lambda: len(_stub_pids(journey.app_pid)) >= len(_STUB_SERVERS),
            "stub MCP wrappers",
            timeout=60,
        )
        journey.wait_pane_text("Ask Codex")
        set_fallback_mock_llm(mock_url, journey.model, "MOCK_FALLBACK")
        journey.record(
            "start",
            session_id=session_id,
            thread_id=state.thread_id,
            app_server_pid=journey.app_pid,
        )
        return journey

    @property
    def state(self) -> CodexNativeBridgeState:
        state = read_bridge_state(self.bridge_dir)
        assert state is not None
        return state

    def record(self, step: str, **fields: Any) -> None:
        fields["mcp_children"] = _mcp_children(self.app_pid)
        with contextlib.suppress(Exception):
            fields["loaded_threads"] = _loaded_threads(self.ws_url)
        self.evidence["steps"].append({"step": step, "t": time.time(), **fields})

    def pane(self) -> str:
        return _tmux(self.socket, "capture-pane", "-p", "-t", self.target, "-S", "-80")

    def wait_pane_text(self, text: str, timeout: float = 60.0) -> None:
        _wait_for(lambda: text in self.pane(), f"TUI to display {text!r}", timeout=timeout)

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
        known = _stub_pids(self.app_pid)
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
            lambda: len(_stub_pids(self.app_pid) - known) >= len(_STUB_SERVERS),
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

    def wait_stubs_gone(self, pids: set[int], timeout: float) -> set[int]:
        deadline = time.monotonic() + timeout
        remaining = {pid for pid in pids if _alive(pid)}
        while remaining and time.monotonic() < deadline:
            time.sleep(1.0)
            remaining = {pid for pid in pids if _alive(pid)}
        self.record(
            "release_window",
            waited_s=round(timeout - max(0.0, deadline - time.monotonic()), 1),
            remaining=sorted(remaining),
        )
        return remaining

    def wait_app_server_gone(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _alive(self.app_pid):
            time.sleep(0.5)
        self.record("teardown_window", app_server_alive=_alive(self.app_pid))
        return not _alive(self.app_pid)

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
        for pid in sorted(
            {self.app_pid, *(row["pid"] for row in _mcp_children(self.app_pid))}, reverse=True
        ):
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                psutil.Process(pid).kill()


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
    started = _Journey.start(
        http_client, live_runner_id, mock_llm_server_url, tmp_path, stub_mcp_log
    )
    yield started
    started.finish()


def test_clear_releases_previous_thread_mcp_processes(journey: _Journey) -> None:
    """Both retired threads' stub MCP wrappers exit after two ``/clear`` rotations."""
    journey.run_turn()
    generations = [_stub_pids(journey.app_pid)]
    for _ in range(2):
        journey.clear()
        generations.append(_stub_pids(journey.app_pid) - set().union(*generations))
    retired = set().union(*generations[:-1])
    remaining = journey.wait_stubs_gone(retired, timeout=_RELEASE_WINDOW_S)
    assert not remaining, (
        f"{len(remaining)} stub MCP wrapper(s) from the two retired Codex "
        f"threads are still running {_RELEASE_WINDOW_S:.0f}s after /clear "
        f"(pids {sorted(remaining)}; "
        f"generations {[sorted(g) for g in generations]}; "
        f"threads still loaded in the app server: {_loaded_threads(journey.ws_url)})"
    )


def test_deleting_rotated_session_closes_app_server(journey: _Journey) -> None:
    """Deleting the session that owns the terminal after ``/clear`` stops its app server."""
    journey.run_turn()
    rotated = journey.clear()
    delete = journey.client.delete(f"/v1/sessions/{rotated.session_id}", timeout=60)
    assert delete.status_code == 200, delete.text[:500]
    assert journey.wait_app_server_gone(_TEARDOWN_WINDOW_S), (
        f"codex app-server pid {journey.app_pid} is still running "
        f"{_TEARDOWN_WINDOW_S:.0f}s after deleting the rotated session "
        f"{rotated.session_id}; MCP wrappers alive: {_mcp_children(journey.app_pid)}"
    )


def test_clear_before_first_turn_releases_mcp_processes(journey: _Journey) -> None:
    """Without a prior turn the forwarder never subscribed, so Codex retires the old thread."""
    first_generation = _stub_pids(journey.app_pid)
    journey.clear()
    remaining = journey.wait_stubs_gone(first_generation, timeout=_RELEASE_WINDOW_S)
    assert not remaining, (
        f"stub MCP wrappers {sorted(remaining)} survived a /clear issued before any turn"
    )
