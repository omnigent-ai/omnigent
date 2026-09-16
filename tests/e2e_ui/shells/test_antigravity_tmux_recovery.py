"""Live browser journey through the server, runner, tmux, and real Antigravity.

Run after signing in with ``agy``::

    OMNIGENT_E2E_ANTIGRAVITY=1 uv run --no-sync pytest \
        tests/e2e_ui/shells/test_antigravity_tmux_recovery.py -v \
        --video=on --output=/tmp/antigravity-e2e

Only tmux probe failures are injected. Antigravity and its model are real.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import psutil
import pytest
from playwright.sync_api import Page, expect

from omnigent._wrapper_labels import (
    ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.antigravity_native.bridge import bridge_dir_for_bridge_id, read_tmux_info
from omnigent.harnesses.antigravity_native.main import _materialize_antigravity_agent_spec
from omnigent.onboarding.gemini_auth import gemini_auth_has_credential
from omnigent.runner.identity import token_bound_runner_id
from tests.e2e_ui.conftest import _find_free_port
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") != "1",
        reason="opt in with OMNIGENT_E2E_ANTIGRAVITY=1; requires a real agy login",
    ),
    pytest.mark.timeout(600),
]

_ROOT = Path(__file__).resolve().parents[3]


def _wait_until(check: Callable[[], bool], message: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, message
        time.sleep(0.2)


@dataclass
class AntigravitySession:
    base_url: str
    session_id: str
    runner: subprocess.Popen[bytes]
    directory: Path
    tmux: str

    @property
    def bridge_dir(self) -> Path:
        return bridge_dir_for_bridge_id(self.session_id)

    def pane(self) -> dict[str, str]:
        info = read_tmux_info(self.bridge_dir)
        assert info is not None, "runner did not publish the Antigravity tmux pane"
        return info

    def tmux_command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.tmux, "-S", self.pane()["socket_path"], *args],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def terminals(self) -> list[dict]:
        response = httpx.get(
            f"{self.base_url}/v1/sessions/{self.session_id}/resources/terminals", timeout=10
        )
        response.raise_for_status()
        return response.json()["data"]

    def harness_process(self) -> psutil.Process:
        for process in psutil.Process(self.runner.pid).children(recursive=True):
            with contextlib.suppress(psutil.NoSuchProcess):
                args = process.cmdline()
                if self.session_id in args and "antigravity-native" in args:
                    return process
        raise AssertionError("runner has no Antigravity harness subprocess for this session")


def _write_tmux_shim(directory: Path, real_tmux: str) -> Path:
    shim_dir = directory / "bin"
    shim_dir.mkdir()
    target = shlex.quote(str(directory / "outage-socket"))
    probes = shlex.quote(str(directory / "failed-probes"))
    shim = shim_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        'socket=""\nprobe=""\nprevious=""\n'
        'for arg in "$@"; do\n'
        '  if [ "$previous" = "-S" ]; then socket="$arg"; fi\n'
        '  case "$arg" in capture-pane|has-session) probe="$arg" ;; esac\n'
        '  previous="$arg"\n'
        "done\n"
        f'if [ -n "$probe" ] && [ -f {target} ] && [ "$socket" = "$(cat {target})" ]; then\n'
        f'  echo "$probe" >> {probes}\n'
        '  echo "error connecting to socket (Connection timed out)" >&2\n'
        "  exit 1\n"
        "fi\n"
        f'exec {shlex.quote(real_tmux)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o700)
    return shim_dir


def _create_session(base_url: str, directory: Path) -> str:
    spec = _materialize_antigravity_agent_spec(directory)
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        archive.add(spec, arcname=spec.name)
    response = httpx.post(
        f"{base_url}/v1/sessions",
        data={
            "metadata": json.dumps(
                {
                    "labels": {
                        WRAPPER_LABEL_KEY: ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
                        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                    },
                    "workspace": str(directory),
                }
            )
        },
        files={"bundle": ("agent.tar.gz", bundle.getvalue(), "application/gzip")},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["session_id"]


@contextlib.contextmanager
def _antigravity_stack(directory: Path) -> Iterator[AntigravitySession]:
    tmux = shutil.which("tmux")
    assert tmux is not None, "install tmux before running this test"
    shim_dir = _write_tmux_shim(directory, tmux)
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    base_url = f"http://127.0.0.1:{_find_free_port()}"
    env = {
        **os.environ,
        "PYTHONPATH": str(_ROOT),
        "OMNIGENT_CONFIG_HOME": str(directory / "config"),
    }
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER_", "HARNESS_ANTIGRAVITY_NATIVE_")):
            del env[key]
    session: AntigravitySession | None = None
    with contextlib.ExitStack() as stack:
        stack.callback(lambda: shutil.rmtree(session.bridge_dir, True) if session else None)
        server_log = stack.enter_context((directory / "server.log").open("w"))
        runner_log = stack.enter_context((directory / "runner.log").open("w"))

        def spawn(args: list[str], process_env: dict[str, str], log) -> subprocess.Popen[bytes]:
            process = subprocess.Popen(
                args, cwd=directory, env=process_env, stdout=log, stderr=subprocess.STDOUT
            )

            def stop() -> None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

            stack.callback(stop)
            return process

        server = spawn(
            [
                sys.executable,
                "-m",
                "omnigent",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                base_url.rsplit(":", 1)[1],
                "--database-uri",
                f"sqlite:///{directory / 'test.db'}",
                "--artifact-location",
                str(directory / "artifacts"),
            ],
            {**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
            server_log,
        )
        runner = spawn(
            [sys.executable, "-m", "omnigent.runner._entry"],
            {
                **env,
                "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": base_url,
            },
            runner_log,
        )

        def ready() -> bool:
            assert server.poll() is None and runner.poll() is None, f"see logs in {directory}"
            try:
                response = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                return response.status_code == 200 and response.json()["online"] is True
            except httpx.HTTPError:
                return False

        try:
            _wait_until(ready, f"server/runner did not become ready; see {directory}", 120)
            session_id = _create_session(base_url, directory)
            session = AntigravitySession(base_url, session_id, runner, directory, tmux)
            response = httpx.patch(
                f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id}, timeout=30
            )
            response.raise_for_status()
            yield session
        finally:
            (directory / "outage-socket").unlink(missing_ok=True)
            if session is not None:
                pane = read_tmux_info(session.bridge_dir)
                with contextlib.suppress(httpx.HTTPError):
                    httpx.delete(f"{base_url}/v1/sessions/{session.session_id}", timeout=10)
                if pane is not None:
                    subprocess.run(
                        [tmux, "-S", pane["socket_path"], "kill-server"],
                        capture_output=True,
                        timeout=10,
                    )


@pytest.fixture
def antigravity_session(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[AntigravitySession]:
    assert not request.config.getoption("--ui-base-url"), "this test requires its own server"
    assert shutil.which("agy"), "install agy before running this test"
    assert gemini_auth_has_credential(), "run `agy` and sign in, then rerun this test"
    request.getfixturevalue("built_spa")
    with _antigravity_stack(tmp_path) as session:
        yield session


def test_antigravity_survives_probe_outage_then_detects_exit(
    page: Page, antigravity_session: AntigravitySession
) -> None:
    session = antigravity_session
    page.add_init_script(_BLOCK_LOOPBACK_DIALS)
    page.goto(f"{session.base_url}/c/{session.session_id}?view=terminal")
    main_terminal = page.get_by_test_id("main-terminal-view")
    terminal = main_terminal.get_by_test_id("terminal-view")
    expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)

    def send_and_expect_reply() -> None:
        token = f"AGY_E2E_{uuid.uuid4().hex}"
        page.get_by_test_id("view-mode-chat").click()
        composer = page.get_by_placeholder("Send a message…")
        composer.fill(f"Reply with exactly {token}. Do not use tools or modify files.")
        page.get_by_role("button", name="Send", exact=True).click()
        reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
        expect(reply.filter(has_text=token)).to_have_count(1, timeout=180_000)
        expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)
        page.get_by_test_id("view-mode-terminal").click()
        expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)

    send_and_expect_reply()
    harness_process = session.harness_process()
    original_terminals = {item["id"] for item in session.terminals()}
    assert original_terminals, "no terminal resource registered"
    pane = session.pane()
    pane_pid = session.tmux_command(
        "display-message", "-p", "-t", pane["tmux_target"], "#{pane_pid}"
    )
    assert pane_pid.returncode == 0, pane_pid.stderr
    pane_process = psutil.Process(int(pane_pid.stdout.strip()))
    agy_executable = Path(shutil.which("agy") or "agy").resolve()
    agy_processes = [
        process
        for process in [pane_process, *pane_process.children(recursive=True)]
        if Path(process.exe()).resolve() == agy_executable
    ]
    assert len(agy_processes) == 1, "expected one real agy client in the terminal"
    agy_process = agy_processes[0]

    outage = session.directory / "outage-socket"
    outage.write_text(pane["socket_path"], encoding="utf-8")
    outage_started = time.monotonic()
    probes = session.directory / "failed-probes"
    try:
        # Outlast three watcher ticks even if other callers also probe tmux.
        _wait_until(
            lambda: (
                probes.exists()
                and probes.read_text().splitlines().count("has-session") >= 4
                and time.monotonic() - outage_started >= 8
            ),
            "runner stopped retrying inconclusive tmux probes",
        )
        assert session.tmux_command("has-session", "-t", pane["tmux_target"]).returncode == 0
        assert agy_process.is_running()
        assert harness_process.is_running()
        assert {item["id"] for item in session.terminals()} == original_terminals
        expect(terminal).to_have_attribute("data-state", "connected")
        expect(page.get_by_text("The harness is not running.", exact=True)).to_have_count(0)
    finally:
        outage.unlink(missing_ok=True)

    send_and_expect_reply()
    assert session.pane() == pane, "recovery replaced the original terminal"
    assert agy_process.is_running(), "recovery restarted Antigravity"
    assert session.harness_process() == harness_process, "recovery replaced the harness"
    assert session.tmux_command("kill-server").returncode == 0

    expect(main_terminal.get_by_text("The harness is not running.", exact=True)).to_be_visible(
        timeout=60_000
    )
    expect(main_terminal.get_by_role("button", name="Resume session", exact=True)).to_be_enabled()
    expect(terminal).to_have_count(0)
    _wait_until(lambda: not session.terminals(), "dead terminal resource was not removed")
    _wait_until(lambda: not agy_process.is_running(), "Antigravity process survived terminal exit")
    _wait_until(lambda: not harness_process.is_running(), "runner did not release the harness")
    response = httpx.get(f"{session.base_url}/v1/sessions/{session.session_id}", timeout=10)
    response.raise_for_status()
    assert response.json()["status"] == "idle"
    assert session.runner.poll() is None, "terminal exit killed the runner"
