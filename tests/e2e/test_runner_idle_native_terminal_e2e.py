"""E2E regression: the runner idle watchdog must not shut down mid-native-turn.

Reported journey: a runner can shut down for inactivity
while a native terminal turn is still actively producing output. Native
terminal turns are not represented in the runner's ``_active_turns`` map after
terminal delivery takes over, and the runner's ``has_active_work()`` consults
neither the native pane's ``running`` status nor the outbound terminal
activity clock. So a single long-running Codex-native turn can reach
``runner.idle_timeout_s`` and the runner logs::

    runner idle timeout reached after <N>s with no active work; shutting down

and exits, killing the still-active native terminal turn.

This drives the real product path end-to-end: a dedicated ``omnigent server`` +
runner is spawned with a short ``runner.idle_timeout_s``; the real ``codex``
CLI is booted in the session terminal (the same lane as
``tests/e2e_ui/messages/test_native_codex_render_parity.py``) against the
in-process mock LLM. One web-UI message is sent whose mock response *blocks*,
so the Codex turn stays running well past the idle window without any further
user input. The bug: the runner logs the idle-timeout shutdown and exits while
that turn is live. The fix keeps the runner alive while the native pane is
running.

Runs against the mock LLM (no real provider, no Codex OAuth), so it needs only
the ``codex`` CLI and ``tmux`` on PATH; it skips cleanly otherwise.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec
from omnigent.runner.identity import token_bound_runner_id

pytestmark = [
    pytest.mark.nightly,
    pytest.mark.timeout(300),
    pytest.mark.skipif(
        shutil.which("codex") is None or shutil.which("tmux") is None,
        reason="native Codex terminal e2e needs the `codex` CLI and `tmux` on PATH",
    ),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Short enough to make the watchdog fire quickly once the turn goes quiet on
# the inbound tunnel; large enough that Codex boot activity doesn't trip it
# before the turn is even live.
_IDLE_TIMEOUT_S = 12
# Distinctive marker so the mock's blocking response routes to THIS turn's
# request regardless of Codex's other internal LLM calls.
_MARKER = "HOLD_THE_NATIVE_TURN_PAST_IDLE"
_CODEX_MOCK_MODEL = "gpt-4o"

_HEALTH_TIMEOUT_S = 90.0
_TERMINAL_READY_TIMEOUT_S = 120.0
# Window to watch after the turn is live: covers the idle expiry plus margin.
_WATCH_S = _IDLE_TIMEOUT_S + 25.0
_IDLE_SHUTDOWN_LOG = "idle timeout reached"


def _free_port() -> int:
    """Return an unused localhost TCP port.

    :returns: A free port number.
    """
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_mock_ready(base_url: str, *, timeout: float = 15.0) -> None:
    """Block until the mock LLM server answers ``/stats``.

    :param base_url: Mock server base URL.
    :param timeout: Max seconds to wait.
    :raises RuntimeError: If the mock never becomes ready.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/stats", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"mock LLM server not ready at {base_url}")


def _clean_child_env(extra: dict[str, str]) -> dict[str, str]:
    """Build a subprocess env with every ambient ``OMNIGENT*`` var stripped.

    Prevents an ambient (omnigent-hosted) runner/host environment from
    hijacking the spawned server/runner — e.g. a leaked
    ``OMNIGENT_PROCESS_LOG_FILE`` redirects the child's logs outside the
    rig's data dir. ``extra`` is merged in last.

    :param extra: Env entries to set for the child.
    :returns: The child environment mapping.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "OMNIGENT" and not key.startswith("OMNIGENT_")
    }
    env.pop("RUNNER_SERVER_URL", None)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env.update(extra)
    return env


class _Rig:
    """A spawned server + runner + mock LLM with a short runner idle timeout."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.mock_url = ""
        self.base_url = ""
        self.runner_id = ""
        self._procs: list[subprocess.Popen[bytes]] = []
        self._handles: list[object] = []
        self._runner_proc: subprocess.Popen[bytes] | None = None

    def runner_log_text(self) -> str:
        """Return the runner's own log text (the watchdog logs here).

        Concatenates every ``runner-*.log`` under the rig's data dir so a
        pre-fork/zygote log can't hide the surviving runner's lines.

        :returns: The runner log contents, or ``""`` if not yet written.
        """
        log_dir = self.tmp / "data" / "logs" / "runner"
        return "\n".join(
            path.read_text(errors="replace") for path in sorted(log_dir.glob("runner-*.log"))
        )

    def runner_exited(self) -> bool:
        """Return whether the runner process has exited.

        :returns: ``True`` once the runner process is no longer running.
        """
        return self._runner_proc is not None and self._runner_proc.poll() is not None

    def _open(self, name: str) -> object:
        handle = open(self.tmp / name, "w")  # noqa: SIM115
        self._handles.append(handle)
        return handle

    def start(self) -> None:
        """Spawn the mock LLM, server, and runner, and wait until online.

        :raises RuntimeError: If the server/runner do not come online.
        """
        mock_port = _free_port()
        self.mock_url = f"http://127.0.0.1:{mock_port}"
        mock_proc = subprocess.Popen(
            [
                sys.executable,
                str(_REPO_ROOT / "tests" / "server" / "integration" / "mock_llm_server.py"),
                str(mock_port),
            ],
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            stdout=self._open("mock_llm.log"),
            stderr=subprocess.STDOUT,
        )
        self._procs.append(mock_proc)
        _wait_mock_ready(self.mock_url)

        # The turn's own request blocks at the mock gate; a permissive fallback
        # keeps Codex's other internal calls answered.
        httpx.post(
            f"{self.mock_url}/mock/configure",
            json={
                "key": _CODEX_MOCK_MODEL,
                "match": _MARKER,
                "responses": [{"text": "held", "block": True}],
            },
            timeout=5,
        ).raise_for_status()
        httpx.post(
            f"{self.mock_url}/mock/set_fallback", json={"text": "ok"}, timeout=5
        ).raise_for_status()

        config_home = self.tmp / "config-home"
        config_home.mkdir(parents=True, exist_ok=True)
        (config_home / "config.yaml").write_text(
            f"""\
runner:
  idle_timeout_s: {_IDLE_TIMEOUT_S}
providers:
  mock-codex:
    kind: key
    default: [openai]
    openai:
      base_url: "{self.mock_url}/v1"
      api_key: "mock-key"
      wire_api: responses
      models:
        default: {_CODEX_MOCK_MODEL}
""",
            encoding="utf-8",
        )
        for sub in ("codex-home", "home", "data", "codex-native-state", "artifacts", "ws"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)

        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        binding_token = secrets.token_urlsafe(32)
        self.runner_id = token_bound_runner_id(binding_token)
        agent_yaml = self.tmp / "hello_world.yaml"
        agent_yaml.write_text(
            "name: hello_world\nprompt: hi\nexecutor:\n"
            "  model: gpt-4o\n  harness: openai-agents\n",
            encoding="utf-8",
        )

        shared = _clean_child_env(
            {
                "OMNIGENT_CONFIG_HOME": str(config_home),
                "OMNIGENT_DATA_DIR": str(self.tmp / "data"),
                "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(self.tmp / "codex-native-state"),
                "CODEX_HOME": str(self.tmp / "codex-home"),
                "HOME": str(self.tmp / "home"),
            }
        )
        server_env = {**shared, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
        runner_env = {
            **shared,
            "OMNIGENT_RUNNER_ID": self.runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": self.base_url,
        }

        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{self.tmp / 'test.db'}",
                "--artifact-location",
                str(self.tmp / "artifacts"),
                "--agent",
                str(agent_yaml),
            ],
            env=server_env,
            stdout=self._open("server.log"),
            stderr=subprocess.STDOUT,
        )
        self._procs.append(server_proc)
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=self._open("runner.log"),
            stderr=subprocess.STDOUT,
        )
        self._procs.append(runner_proc)
        self._runner_proc = runner_proc

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                raise RuntimeError("server or runner exited during startup")
            try:
                if httpx.get(f"{self.base_url}/health", timeout=2).status_code == 200:
                    status = httpx.get(
                        f"{self.base_url}/v1/runners/{self.runner_id}/status", timeout=2
                    )
                    if status.status_code == 200 and status.json().get("online") is True:
                        return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("spawned server/runner did not come online in time")

    def close(self) -> None:
        """Tear down every spawned process and file handle."""
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{self.mock_url}/gate/release", timeout=3)
        for proc in reversed(self._procs):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        for handle in self._handles:
            with contextlib.suppress(OSError):
                handle.close()  # type: ignore[attr-defined]


@pytest.fixture
def idle_rig() -> Iterator[_Rig]:
    """Spawn the short-idle-timeout server/runner rig for one test.

    :returns: The started :class:`_Rig`.
    """
    tmp = Path(tempfile.mkdtemp(prefix="idle-native-turn-"))
    rig = _Rig(tmp)
    try:
        rig.start()
        yield rig
    finally:
        rig.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _create_codex_native_session(rig: _Rig) -> str:
    """Register + bind a real ``codex-native`` session on the rig's runner.

    Uses the exact terminal-first spec ``omnigent codex`` ships, so the runner
    auto-launches Codex in the session terminal on bind.

    :param rig: The started rig.
    :returns: The created session/conversation id.
    """
    with tempfile.TemporaryDirectory() as spec_dir:
        yaml_text = _materialize_codex_agent_spec(
            Path(spec_dir), model=_CODEX_MOCK_MODEL
        ).read_text()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    metadata = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
        },
        "workspace": str(rig.tmp / "ws"),
    }
    create = httpx.post(
        f"{rig.base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("codex-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    httpx.patch(
        f"{rig.base_url}/v1/sessions/{session_id}",
        json={"runner_id": rig.runner_id},
        timeout=10,
    ).raise_for_status()
    return session_id


def _wait_terminal_ready(client: httpx.Client, session_id: str) -> None:
    """Poll until the session's Codex terminal resource is registered.

    :param client: HTTP client pointed at the rig server.
    :param session_id: The session to wait on.
    :raises AssertionError: If the terminal never registers in time.
    """
    expected = terminal_resource_id("codex", "main")
    deadline = time.monotonic() + _TERMINAL_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources", timeout=5)
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if any(r.get("id") == expected and r.get("type") == "terminal" for r in data):
                return
        time.sleep(0.5)
    raise AssertionError(f"Codex terminal resource {expected!r} never registered for {session_id}")


def _gate_pending(mock_url: str) -> bool:
    """Return whether a turn is currently blocked at the mock gate.

    A pending gate means Codex's ``/v1/responses`` call is open and awaiting
    release — i.e. the native turn is genuinely live.

    :param mock_url: Mock server base URL.
    :returns: ``True`` while a gate is pending.
    """
    try:
        resp = httpx.get(f"{mock_url}/gate/pending", timeout=2)
        return resp.status_code == 200 and bool(resp.json().get("pending"))
    except httpx.HTTPError:
        return False


def test_runner_stays_alive_during_active_native_turn(idle_rig: _Rig) -> None:
    """A live Codex-native turn keeps the runner alive past its idle window."""
    rig = idle_rig
    session_id = _create_codex_native_session(rig)

    with httpx.Client(base_url=rig.base_url) as client:
        _wait_terminal_ready(client, session_id)

        client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"{_MARKER} keep working for a long time"}
                    ],
                },
            },
            timeout=30,
        ).raise_for_status()

        # The turn must be genuinely live before we judge the watchdog: wait
        # for Codex's request to block at the mock gate. While waiting, keep
        # polling the session's resources through the server — the relayed GET
        # refreshes the runner's idle clock like any user traffic, so a slow
        # Codex turn start can't idle the runner out before the turn is live
        # and produce a false bug verdict.
        turn_live_deadline = time.monotonic() + 60
        while time.monotonic() < turn_live_deadline and not _gate_pending(rig.mock_url):
            if rig.runner_exited():
                break
            with contextlib.suppress(httpx.HTTPError):
                client.get(f"/v1/sessions/{session_id}/resources", timeout=5)
            time.sleep(0.5)
        assert _gate_pending(rig.mock_url), (
            "Codex-native turn never went live at the mock gate "
            f"(runner_exited={rig.runner_exited()}); "
            f"runner log tail:\n{rig.runner_log_text()[-2000:]}"
        )

        # Watch across the idle window. On the bug, the runner logs the
        # idle-timeout shutdown and exits while the turn is still blocked.
        watch_deadline = time.monotonic() + _WATCH_S
        while time.monotonic() < watch_deadline:
            if _IDLE_SHUTDOWN_LOG in rig.runner_log_text() or rig.runner_exited():
                break
            time.sleep(1.0)

    # The idle-shutdown reason is logged just before the process exits; give
    # the file a moment to flush so the failure message can name the line.
    log_deadline = time.monotonic() + 5.0
    while time.monotonic() < log_deadline and _IDLE_SHUTDOWN_LOG not in rig.runner_log_text():
        time.sleep(0.5)
    log_text = rig.runner_log_text()
    idle_lines = [ln for ln in log_text.splitlines() if _IDLE_SHUTDOWN_LOG in ln]
    assert not idle_lines and not rig.runner_exited(), (
        "runner idle watchdog shut down during an active native terminal turn "
        f"(idle_timeout_s={_IDLE_TIMEOUT_S}); the Codex turn was still live at the "
        f"mock gate. Offending log line(s):\n" + "\n".join(idle_lines)
    )
