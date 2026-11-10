"""A foreground host's terminal hangup must let an in-flight turn finish.

``omnigent host`` runs in the foreground of a real terminal (a pty), a session
on that host has a turn waiting on the model, and the terminal closes. The
kernel delivers SIGHUP to the host, which dies by it. The reply should still
reach the session before the runner exits. SIGTERM and Ctrl-C controls stop
the held turn for both direct and zygote runners, without request replay.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pexpect
import pytest
import yaml

from omnigent.inner import _proc
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.live_server import isolated_local_server, isolated_process_env
from tests._helpers.mock_llm import advertised_tool_names
from tests._helpers.native_session import create_native_session
from tests._helpers.session import (
    bind_session_runner,
    bundle_files,
    post_session_bundle,
    send_user_text,
)
from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    poll_session_until_terminal,
    release_mock_gate,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text
from tests.e2e.test_host_codex_native_e2e import _poll_for_assistant_marker

_HOST_ONLINE_TIMEOUT_S = 120.0
_RUNNER_ONLINE_TIMEOUT_S = 90.0
_GATE_TIMEOUT_S = 60.0
_TURN_TIMEOUT_S = 120.0
_REPLY = "HANGUP_SURVIVOR_REPLY"
_AGENT_YAML = """\
spec_version: 1
name: hangup-turn-agent
prompt: You are a terse test assistant. Reply exactly as asked.
executor:
  model: {model}
  config:
    harness: openai-agents
"""


class _ForegroundHostTerminal:
    """``omnigent host`` as the session leader of a pty, drained like a terminal."""

    def __init__(
        self,
        root: Path,
        base_url: str,
        mock_url: str,
        *,
        use_zygote: bool,
        providers: dict | None = None,
        environment: dict[str, str | None] | None = None,
    ) -> None:
        home = root / "home"
        config = home / ".omnigent"
        config.mkdir(parents=True)
        self.host_id = uuid.uuid4().hex
        name = f"hangup-{self.host_id[:8]}"
        (config / "config.yaml").write_text(
            yaml.safe_dump(
                {"host": {"host_id": self.host_id, "name": name}, "providers": providers or {}}
            )
        )
        self.log_path = root / "host.log"
        env = isolated_process_env(
            home,
            {
                "OPENAI_BASE_URL": f"{mock_url}/v1",
                "OPENAI_API_KEY": "mock-key",
                "OMNIGENT_RUNNER_ZYGOTE": str(int(use_zygote)),
                **(environment or {}),
            },
            None,
        )
        env.update(
            OMNIGENT_CONFIG_HOME=str(config),
            OMNIGENT_HOST_ID=self.host_id,
            OMNIGENT_HOST_NAME=name,
            TERM="xterm-256color",
        )
        env[PROCESS_LOG_FILE_ENV_VAR] = str(self.log_path)
        self.child = pexpect.spawn(
            runner_executable(),
            ["-m", "omnigent", "host", "--server", base_url, "--non-interactive", "--no-open"],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd() or str(root),
            encoding="utf-8",
            timeout=5,
            dimensions=(40, 160),
        )
        # A spawn that succeeds but whose post-spawn setup raises would leak the
        # host subprocess, so tear it down before propagating the error.
        try:
            self.transcript = (root / "terminal.txt").open("w", encoding="utf-8")
            self.child.logfile_read = self.transcript
            self._stop = threading.Event()
            self._drain = threading.Thread(target=self._pump, name="host-pty-drain", daemon=True)
            self._drain.start()
        except BaseException:
            with contextlib.suppress(Exception):
                self.child.close(force=True)
            with contextlib.suppress(Exception):
                self.transcript.close()
            raise

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                self.child.read_nonblocking(4096, timeout=0.2)
            except pexpect.TIMEOUT:
                continue
            except (pexpect.EOF, OSError, ValueError):
                return

    def hang_up(self) -> None:
        """Close the terminal: the kernel sends SIGHUP to the host's foreground group."""
        self._stop.set()
        self._drain.join(timeout=5)
        assert not self._drain.is_alive(), "pty drain thread did not stop before fd close"
        # Close through ptyprocess so it marks the fd closed; a bare os.close()
        # leaves its fileobj owning the number and GC would reclose a reused fd.
        ptyproc = self.child.ptyproc
        ptyproc.fileobj.close()
        ptyproc.fd = -1
        ptyproc.closed = True
        self.child.child_fd = -1

    def wait_exit(self, timeout: float) -> tuple[int | None, int | None]:
        deadline = time.monotonic() + timeout
        while self.child.isalive() and time.monotonic() < deadline:
            time.sleep(0.1)
        return self.child.exitstatus, self.child.signalstatus

    def close(self) -> None:
        self._stop.set()
        self._drain.join(timeout=5)
        try:
            if self.child.isalive():
                with contextlib.suppress(Exception):
                    self.child.kill(9)
                self.wait_exit(timeout=5)
            if self.child.child_fd != -1:
                with contextlib.suppress(Exception):
                    self.child.close(force=True)
        finally:
            self.transcript.close()


def _pgid_or_none(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None


def _pid_running(pid: int, expected_create_time: float | None = None) -> bool:
    if expected_create_time is not None and _proc.process_create_time(pid) != expected_create_time:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in {"Z", "X"}


def _wait_until(predicate: Callable[[], object], timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.mark.skipif(sys.platform != "linux", reason="pty hangup semantics; /proc liveness")
@pytest.mark.min_runner_version("0.21.0")
@pytest.mark.timeout(420)
@pytest.mark.parametrize("use_zygote", [True, False], ids=["zygote", "direct"])
@pytest.mark.parametrize("exit_action", ["hangup", "sigterm", "ctrl-c"])
@pytest.mark.parametrize("harness", ["openai-agents", "claude-native", "codex-native"])
def test_terminal_hangup_lets_in_flight_turn_finish(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    use_zygote: bool,
    exit_action: str,
    harness: str,
) -> None:
    if harness != "openai-agents" and any(
        shutil.which(binary) is None for binary in (harness.removesuffix("-native"), "tmux")
    ):
        pytest.skip("requires the native CLI and tmux")
    if harness == "codex-native" and Path("/etc/codex/managed_config.toml").exists():
        pytest.skip("requires isolated Codex system config to keep model requests local")
    if harness == "claude-native" and Path("/etc/claude-code/managed-settings.json").exists():
        pytest.skip("requires isolated Claude system settings to keep model requests local")
    mock_url = isolated_mock_llm_server_url
    queue = f"hangup-turn-{uuid.uuid4().hex[:8]}"
    model = {"claude-native": "claude-opus-4-8", "codex-native": "gpt-5.4"}.get(harness, queue)
    prompt = f"Hangup probe {queue}: reply with exactly {_REPLY}."
    required_tools = ["Bash"] if harness == "claude-native" else []
    configure_mock_llm(
        mock_url,
        [{"text": _REPLY, "block": True}],
        key=queue,
        match=prompt,
        required_tools=required_tools or None,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    providers = {}
    environment = {}
    if harness == "claude-native":
        claude_config = tmp_path / "claude-config"
        claude_config.mkdir()
        (claude_config / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
                }
            )
        )
        providers = {
            "hangup-mock": {
                "kind": "key",
                "default": ["anthropic"],
                "anthropic": {
                    "base_url": mock_url,
                    "api_key": "mock-key",
                    "models": {"default": model},
                },
            }
        }
        environment = {
            "CLAUDE_CONFIG_DIR": str(claude_config),
            "ANTHROPIC_AUTH_TOKEN": "mock-key",
            "ANTHROPIC_BASE_URL": mock_url,
            "CLAUDE_CODE_OAUTH_TOKEN": None,
            "CLAUDE_CODE_USE_GATEWAY": None,
            "CLAUDE_CODE_USE_BEDROCK": None,
            "CLAUDE_CODE_USE_VERTEX": None,
            "ANTHROPIC_BEDROCK_BASE_URL": None,
            "ANTHROPIC_CUSTOM_HEADERS": None,
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
        }
    elif harness == "codex-native":
        codex_config = tmp_path / "codex-config"
        codex_config.mkdir()
        environment = {"CODEX_HOME": str(codex_config)}
        providers = {
            "hangup-mock": {
                "kind": "key",
                "default": ["openai"],
                "openai": {
                    "base_url": f"{mock_url}/v1",
                    "api_key": "mock-key",
                    "models": {"default": model},
                },
            }
        }
    runner_pid: int | None = None
    zygote_pid: int | None = None
    runner_create_time: float | None = None
    zygote_create_time: float | None = None
    native_pid: int | None = None
    native_create_time: float | None = None
    native_socket: str | None = None
    native_target: str | None = None
    with (
        isolated_local_server(tmp_path / "server") as base_url,
        httpx.Client(
            base_url=base_url,
            trust_env=False,
            timeout=30,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        terminal = _ForegroundHostTerminal(
            tmp_path / "host",
            base_url,
            mock_url,
            use_zygote=use_zygote,
            providers=providers,
            environment=environment,
        )
        try:

            def _host_online() -> bool:
                try:
                    rows = client.get("/v1/hosts").json().get("hosts", [])
                except (httpx.HTTPError, ValueError):
                    return False  # server still warming up; let _wait_until retry
                return any(
                    r.get("host_id") == terminal.host_id and r.get("status") == "online"
                    for r in rows
                )

            assert _wait_until(_host_online, _HOST_ONLINE_TIMEOUT_S), (
                f"host never came online\n{terminal.log_path.read_text(errors='replace')[-4000:]}"
            )
            if harness == "openai-agents":
                bundle = bundle_files({"config.yaml": _AGENT_YAML.format(model=model).encode()})
                created = post_session_bundle(client.post, "/v1/sessions", bundle)
                created.raise_for_status()
                session_id = str(created.json()["session_id"])
            else:
                session_id = str(
                    create_native_session(
                        client,
                        base_url,
                        harness=harness.removesuffix("-native"),
                        metadata={"workspace": str(workspace), "title": "Terminal hangup probe"},
                        **({"model": model} if harness == "codex-native" else {}),
                    )["session_id"]
                )
            launch = client.post(
                f"/v1/hosts/{terminal.host_id}/runners",
                json={"session_id": session_id, "workspace": str(workspace)},
                timeout=90.0,
            )
            assert launch.status_code == 200, launch.text
            runner_id = launch.json()["runner_id"]

            def _runner_online() -> bool:
                try:
                    status = client.get(f"/v1/runners/{runner_id}/status").json()
                except (httpx.HTTPError, ValueError):
                    return False  # runner still registering; let _wait_until retry
                return status.get("online") is True

            assert _wait_until(_runner_online, _RUNNER_ONLINE_TIMEOUT_S), (
                "host-launched runner never came online"
            )
            bind_session_runner(client.patch, base_url, session_id, runner_id)
            host_log = terminal.log_path.read_text(errors="replace")
            launched = re.search(r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)", host_log)
            assert launched is not None, host_log[-4000:]
            runner_pid = int(launched.group(1))
            runner_create_time = _proc.process_create_time(runner_pid)
            forked = re.search(r"zygote pid=(\d+), runner pid=", host_log)
            zygote_pid = int(forked.group(1)) if forked else None
            assert (zygote_pid is not None) == use_zygote, "requested runner launch path not used"
            zygote_create_time = _proc.process_create_time(zygote_pid) if zygote_pid else None
            host_pgid = _pgid_or_none(terminal.child.pid)
            print(
                f"host pid={terminal.child.pid} pgid={host_pgid} zygote={zygote_pid} "
                f"(pgid={_pgid_or_none(zygote_pid)}) runner={runner_pid} "
                f"(pgid={_pgid_or_none(runner_pid)}) session={session_id} runner_id={runner_id}"
            )

            if harness != "openai-agents":
                ensured = client.post(
                    f"/v1/sessions/{session_id}/resources/terminals",
                    json={
                        "terminal": harness.removesuffix("-native"),
                        "session_key": "main",
                        "ensure_native_terminal": True,
                    },
                    timeout=90,
                )
                ensured.raise_for_status()
                metadata = ensured.json()["metadata"]
                native_socket, native_target = metadata["tmux_socket"], metadata["tmux_target"]
                native_pid = int(
                    subprocess.check_output(
                        [
                            "tmux",
                            "-S",
                            native_socket,
                            "display-message",
                            "-p",
                            "-t",
                            native_target,
                            "#{pane_pid}",
                        ],
                        text=True,
                        timeout=5,
                    ).strip()
                )
                native_create_time = _proc.process_create_time(native_pid)
                assert _pid_running(native_pid)

            if harness == "openai-agents":
                response_id = send_user_message_to_session(
                    client, session_id=session_id, content=prompt
                )
            else:
                send_user_text(client, session_id=session_id, text=prompt)
                response_id = None

            def _gate_pending() -> bool:
                try:
                    body = httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False).json()
                except (httpx.HTTPError, ValueError):
                    return False  # mock server still warming up; let _wait_until retry
                return bool(body.get("pending"))

            reached_model = _wait_until(_gate_pending, _GATE_TIMEOUT_S)
            pane = ""
            if not reached_model and native_socket is not None:
                pane = subprocess.run(
                    [
                        "tmux",
                        "-S",
                        native_socket,
                        "capture-pane",
                        "-p",
                        "-S",
                        "-80",
                        "-t",
                        native_target,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                ).stdout

            def probe_requests() -> list[dict]:
                return [
                    request
                    for request in get_mock_requests(mock_url)
                    if prompt in json.dumps(request)
                    and set(required_tools) <= advertised_tool_names([request])
                ]

            assert reached_model, (
                f"the turn never reached the model\n{pane}\n"
                f"{terminal.log_path.read_text(errors='replace')[-4000:]}"
            )
            assert len(probe_requests()) == 1
            before = client.get(f"/v1/sessions/{session_id}").json()
            print(f"status before hangup: {before.get('status')}")
            assert before.get("status") == "running"

            hangup_at = time.monotonic()
            if exit_action == "hangup":
                terminal.hang_up()
            elif exit_action == "sigterm":
                os.kill(terminal.child.pid, signal.SIGTERM)
            else:
                terminal.child.sendcontrol("c")
            host_exit = terminal.wait_exit(timeout=15)
            print(
                f"host exit (exitstatus, signalstatus)={host_exit} "
                f"after {time.monotonic() - hangup_at:.2f}s"
            )
            assert not terminal.child.isalive(), (
                f"host survived {exit_action} (test precondition): {host_exit}"
            )
            if exit_action != "hangup":
                assert _wait_until(lambda: not _pid_running(runner_pid), 15.0), (
                    f"explicit {exit_action} left the busy runner draining"
                )
                assert len(probe_requests()) == 1
                if native_pid is not None:
                    assert _wait_until(
                        lambda: not _pid_running(native_pid, native_create_time), 15.0
                    ), (
                        f"native pane {native_pid} survived explicit {exit_action}\n"
                        f"{terminal.log_path.read_text(errors='replace')[-12000:]}"
                    )
                print(f"explicit {exit_action}: runner stopped with the mock reply still held")
                return
            assert host_exit[1] == signal.SIGHUP, f"host should die by SIGHUP, not {host_exit}"
            # custom-lint: disable-next=sleep-then-assert -- negative window: runner must not exit
            time.sleep(2.0)
            runner_alive_at_release = _pid_running(runner_pid)
            assert runner_alive_at_release, (
                "runner must outlive the host's terminal hangup, but it exited "
                f"within 2s of the SIGHUP (host exit {host_exit})"
            )
            if native_pid is not None:
                assert _pid_running(native_pid, native_create_time), (
                    "native process exited during an active turn"
                )
            release_mock_gate(mock_url)
            print(
                f"gate released {time.monotonic() - hangup_at:.2f}s after hangup; "
                f"runner alive={runner_alive_at_release} zygote alive="
                f"{_pid_running(zygote_pid) if zygote_pid else None}"
            )

            if harness == "openai-agents":
                body = poll_session_until_terminal(
                    client, session_id=session_id, response_id=response_id, timeout=_TURN_TIMEOUT_S
                )
                settled = client.get(f"/v1/sessions/{session_id}").json()
                error = settled.get("last_task_error") or body.get("error")
                print(
                    f"turn terminal {time.monotonic() - hangup_at:.1f}s after hangup: "
                    f"status={body['status']} last_task_error={error}"
                )
                assert body["status"] == "completed", (
                    f"the in-flight turn did not finish after the terminal hangup: "
                    f"status={body['status']} last_task_error={error} "
                    f"(runner alive when the model replied: {runner_alive_at_release})"
                )
                assert _REPLY in final_assistant_text(body)
            else:
                _poll_for_assistant_marker(
                    client, session_id=session_id, marker=_REPLY, timeout=_TURN_TIMEOUT_S
                )
                assert _wait_until(
                    lambda: (
                        client.get(f"/v1/sessions/{session_id}").json().get("status") == "idle"
                    ),
                    30.0,
                )
                assert not client.get(f"/v1/sessions/{session_id}").json().get("last_task_error")
            assert len(probe_requests()) == 1, "turn must not be resubmitted"
            shutdown_timeout = 30.0
            if harness != "openai-agents":
                from omnigent.terminals.pane_reaper import PANE_OUTPUT_BUSY_WINDOW_S

                shutdown_timeout += PANE_OUTPUT_BUSY_WINDOW_S
            assert _wait_until(lambda: not _pid_running(runner_pid), shutdown_timeout), (
                f"runner {runner_pid} kept running after its turn finished"
            )
        finally:
            try:
                if zygote_pid is not None and not terminal.child.isalive():
                    assert _wait_until(lambda: not _pid_running(zygote_pid), 15.0), (
                        "runner zygote outlived the host"
                    )
            finally:
                if native_socket is not None and native_target is not None:
                    subprocess.run(
                        ["tmux", "-S", native_socket, "kill-session", "-t", native_target],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                # Confirm PID and start time before killing non-child processes.
                try:
                    for pid, create_time in (
                        (native_pid, native_create_time),
                        (runner_pid, runner_create_time),
                        (zygote_pid, zygote_create_time),
                    ):
                        if (
                            pid
                            and _pid_running(pid)
                            and _proc.process_create_time(pid) == create_time
                        ):
                            with contextlib.suppress(ProcessLookupError, PermissionError):
                                os.kill(pid, 9)
                finally:
                    terminal.close()
