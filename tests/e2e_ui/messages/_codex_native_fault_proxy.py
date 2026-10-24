"""Dedicated codex-native server + runner whose app-server traffic crosses a fault proxy.

The runner resolves ``codex`` through ``OMNIGENT_CODEX_PATH``. The wrapper written
here execs the real CLI for every invocation except ``app-server --listen``, where
it starts the real app-server on a private loopback port and serves a transparent
WebSocket proxy on the requested port. While the fault flag file exists, the proxy
answers the transcript forwarder's subscribe requests (``thread/resume`` carrying
only ``threadId``/``excludeTurns``) on its connections with the
``-32601 list_turns is not supported yet`` refusal and forwards everything else,
including the TUI's own resume and the terminal attach that shares the resumed
forwarder's connection.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _TEST_AGENT_YAML,
    _create_native_codex_session,
    _find_free_port,
)
from tests.helpers.ui_configuration import _CODEX_MOCK_MODEL, _CODEX_MOCK_PRICING_PER_MILLION

_REPO_ROOT = Path(__file__).resolve().parents[3]

RESUME_FAULT_ERROR: dict[str, Any] = {"code": -32601, "message": "list_turns is not supported yet"}
# A fresh session's forwarder subscribes over the runner's thread-discovery
# listener (orchestration.py), a resumed session's forwarder reuses the
# terminal-attach preload connection (app_server.py) or opens its own
# (forwarder.py); SUBSCRIBE_PARAMS mirrors the forwarder's thread/resume
# request. The journey asserts that a refusal was injected, so drift here
# fails loudly instead of degrading into a control run.
FORWARDER_CLIENT_NAMES = (
    "omnigent-codex-native-auto",
    "omnigent-codex-native-preload",
    "omnigent-codex-forwarder",
)
SUBSCRIBE_PARAMS = frozenset({"threadId", "excludeTurns"})

_PROXY_SCRIPT = textwrap.dedent(
    '''\
    #!{python}
    """Codex CLI stand-in that proxies app-server WebSocket traffic."""
    import asyncio
    import contextlib
    import json
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time

    import websockets

    REAL = {real!r}
    LOG = {log!r}
    FAULT_FLAG = {flag!r}
    FAULT_NAMES = {names!r}
    SUBSCRIBE_PARAMS = {subscribe_params!r}
    FAULT_ERROR = {error!r}


    def _log(**record):
        record["ts"] = time.time()
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\\n")


    def _free_port():
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]


    def _port_open(port):
        with socket.socket() as probe:
            probe.settimeout(0.2)
            try:
                probe.connect(("127.0.0.1", port))
            except OSError:
                return False
            return True


    async def _relay(conn, upstream_port):
        path = conn.request.path if conn.request is not None else "/"
        conn_id = id(conn)
        try:
            upstream = await websockets.connect(
                f"ws://127.0.0.1:{{upstream_port}}{{path}}", max_size=None, compression=None
            )
        except Exception as exc:  # noqa: BLE001
            _log(event="upstream_connect_failed", conn=conn_id, error=repr(exc))
            await conn.close(1011)
            return
        state = {{"name": None, "pending": set()}}

        async def client_to_upstream():
            async for message in conn:
                if isinstance(message, str):
                    try:
                        obj = json.loads(message)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict):
                        method = obj.get("method")
                        if method == "initialize":
                            params = obj.get("params") or {{}}
                            state["name"] = (params.get("clientInfo") or {{}}).get("name")
                            _log(event="initialize", conn=conn_id, client=state["name"])
                        elif method == "thread/resume":
                            params = obj.get("params") or {{}}
                            inject = (
                                state["name"] in FAULT_NAMES
                                and set(params) <= set(SUBSCRIBE_PARAMS)
                                and os.path.exists(FAULT_FLAG)
                            )
                            _log(
                                event="thread/resume",
                                conn=conn_id,
                                client=state["name"],
                                id=obj.get("id"),
                                params=obj.get("params"),
                                injected=inject,
                            )
                            if inject:
                                refusal = {{"id": obj.get("id"), "error": FAULT_ERROR}}
                                await conn.send(json.dumps(refusal))
                                continue
                            if obj.get("id") is not None:
                                state["pending"].add(obj["id"])
                await upstream.send(message)

        async def upstream_to_client():
            async for message in upstream:
                if isinstance(message, str) and state["pending"]:
                    try:
                        obj = json.loads(message)
                    except ValueError:
                        obj = None
                    if (
                        isinstance(obj, dict)
                        and "method" not in obj
                        and obj.get("id") in state["pending"]
                    ):
                        state["pending"].discard(obj.get("id"))
                        thread = (obj.get("result") or {{}}).get("thread") or {{}}
                        _log(
                            event="thread/resume.response",
                            conn=conn_id,
                            client=state["name"],
                            id=obj.get("id"),
                            error=obj.get("error"),
                            turns=len(thread.get("turns") or []) if not obj.get("error") else None,
                        )
                await conn.send(message)

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                _log(event="relay_error", conn=conn_id, error=repr(exc))
        with contextlib.suppress(Exception):
            await upstream.close()
        with contextlib.suppress(Exception):
            await conn.close()
        _log(event="closed", conn=conn_id, client=state["name"])


    async def _serve(host, port, upstream_port, child):
        deadline = time.monotonic() + 120.0
        while not _port_open(upstream_port):
            if child.poll() is not None:
                _log(event="child_exited_before_ready", code=child.returncode)
                return child.returncode
            if time.monotonic() > deadline:
                _log(event="upstream_timeout")
                child.terminate()
                return 1
            await asyncio.sleep(0.1)

        async def handler(conn):
            await _relay(conn, upstream_port)

        async with websockets.serve(handler, host, port, max_size=None, compression=None):
            _log(event="listening", host=host, port=port, upstream_port=upstream_port)
            while child.poll() is None:
                await asyncio.sleep(0.2)
        _log(event="child_exited", code=child.returncode)
        return child.returncode


    def main():
        argv = sys.argv[1:]
        if argv[:1] != ["app-server"] or "--listen" not in argv:
            os.execv(REAL, [REAL, *argv])
        url_index = argv.index("--listen") + 1
        listen = argv[url_index]
        if not listen.startswith("ws://"):
            os.execv(REAL, [REAL, *argv])
        host, _, port_text = listen.removeprefix("ws://").partition(":")
        upstream_port = _free_port()
        child_argv = [REAL, *argv]
        child_argv[url_index + 1] = f"ws://127.0.0.1:{{upstream_port}}"
        child = subprocess.Popen(child_argv)

        def _forward(signum, _frame):
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signum)

        signal.signal(signal.SIGTERM, _forward)
        signal.signal(signal.SIGINT, _forward)
        _log(
            event="start",
            pid=os.getpid(),
            child_pid=child.pid,
            listen=listen,
            upstream_port=upstream_port,
            argv=argv,
        )
        code = asyncio.run(_serve(host or "127.0.0.1", int(port_text), upstream_port, child))
        sys.exit(code or 0)


    if __name__ == "__main__":
        try:
            main()
        except Exception as exc:  # noqa: BLE001
            _log(event="shim_error", error=repr(exc))
            raise
    '''
)


def write_codex_fault_proxy(
    directory: Path, codex_path: str, *, log_path: Path, fault_flag: Path
) -> Path:
    """Write the ``codex`` wrapper that proxies app-server traffic; return its path."""
    shim = directory / "codex-fault-proxy"
    shim.write_text(
        _PROXY_SCRIPT.format(
            python=sys.executable,
            real=codex_path,
            log=str(log_path),
            flag=str(fault_flag),
            names=FORWARDER_CLIENT_NAMES,
            subscribe_params=tuple(sorted(SUBSCRIBE_PARAMS)),
            error=RESUME_FAULT_ERROR,
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _write_mock_codex_provider_config(config_home: Path, mock_llm_url: str) -> None:
    """Mirror the standalone codex mock provider config in a private config home."""
    config_home.mkdir(parents=True, exist_ok=True)
    rates = _CODEX_MOCK_PRICING_PER_MILLION
    (config_home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            providers:
              mock-codex:
                kind: key
                default: [openai]
                openai:
                  base_url: "{mock_llm_url}/v1"
                  api_key: "mock-key"
                  wire_api: responses
                  models:
                    default: {_CODEX_MOCK_MODEL}
                  pricing:
                    input_per_million: {rates[0]}
                    output_per_million: {rates[1]}
                    cache_read_per_million: {rates[2]}
            """
        ),
        encoding="utf-8",
    )


@dataclasses.dataclass
class CodexNativeFaultEnvironment:
    """A spawned server/runner pair whose Codex app-servers sit behind the fault proxy."""

    base_url: str
    runner_id: str
    root: Path
    home: Path
    data_dir: Path
    proxy_log: Path
    fault_flag: Path
    server_log: Path
    mock_llm_url: str
    runner_logs: list[Path]
    _restart_runner: Callable[[], None]

    def enable_resume_fault(self) -> None:
        self.fault_flag.write_text("1", encoding="utf-8")

    def disable_resume_fault(self) -> None:
        self.fault_flag.unlink(missing_ok=True)

    def restart_runner(self) -> None:
        self._restart_runner()

    @property
    def runner_log(self) -> Path:
        return self.runner_logs[-1]

    def proxy_events(self) -> list[dict[str, Any]]:
        if not self.proxy_log.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.proxy_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue  # the shim may still be appending the last record
        return events

    def bridge_dir(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        return self.home / ".omnigent" / "codex-native" / digest

    def create_session(self) -> str:
        return _create_native_codex_session(self.base_url, self.runner_id)


def _wait_until_ready(
    base_url: str,
    runner_id: str,
    server: subprocess.Popen[bytes] | None,
    runner: subprocess.Popen[bytes],
    *,
    logs: tuple[Path, ...],
) -> None:
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S * 2
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if server is not None and server.poll() is not None:
            last_error = f"server exited early with code {server.returncode}"
            break
        if runner.poll() is not None:
            last_error = f"runner exited early with code {runner.returncode}"
            break
        try:
            health = httpx.get(f"{base_url}/health", timeout=2)
            if health.status_code == 200:
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if status.status_code == 200 and status.json().get("online") is True:
                    return
                last_error = f"runner status HTTP {status.status_code}: {status.text[:200]}"
            else:
                last_error = f"health HTTP {health.status_code}: {health.text[:200]}"
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    tails = "\n".join(
        f"{log}:\n{log.read_text(encoding='utf-8', errors='replace')[-3000:]}"
        for log in logs
        if log.exists()
    )
    raise RuntimeError(f"codex-native fault environment not ready ({last_error}).\n{tails}")


def _stop(process: subprocess.Popen[bytes] | None, *, grace: float) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)


def spawn_codex_native_fault_environment(
    root: Path, *, mock_llm_url: str, codex_path: str
) -> tuple[CodexNativeFaultEnvironment, Callable[[], None]]:
    """Start a private server + runner pair; return the environment and its teardown."""
    config_home = root / "config-home"
    source_codex_home = root / "source-codex-home"
    home = root / "home"
    state_dir = root / "codex-native-state"
    artifact_dir = root / "artifacts"
    data_dir = root / "data"
    for path in (source_codex_home, home, state_dir, artifact_dir, data_dir):
        path.mkdir(parents=True, exist_ok=True)
    _write_mock_codex_provider_config(config_home, mock_llm_url)
    proxy_log = root / "codex-proxy.jsonl"
    fault_flag = root / "codex-resume-fault.flag"
    shim = write_codex_fault_proxy(root, codex_path, log_path=proxy_log, fault_flag=fault_flag)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    agent_yaml_path = root / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home),
        "OMNIGENT_DATA_DIR": str(data_dir),
        "OMNIGENT_CODEX_PATH": str(shim),
        "ANTHROPIC_API_KEY": "",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }
    server_log = root / "server.log"
    server_handle = open(server_log, "w")  # noqa: SIM115 — closed in teardown
    server: subprocess.Popen[bytes] | None = None
    server_argv = [
        sys.executable,
        "-c",
        "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
        "from omnigent.cli import main; main()",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{root / 'test.db'}",
        "--artifact-location",
        str(artifact_dir),
        "--agent",
        str(agent_yaml_path),
    ]
    runner_logs: list[Path] = []
    runner_handles: list[Any] = []
    runner_box: dict[str, subprocess.Popen[bytes]] = {}

    def _spawn_runner() -> subprocess.Popen[bytes]:
        log_path = root / f"runner-{len(runner_logs) + 1}.log"
        handle = open(log_path, "w")  # noqa: SIM115 — closed in teardown
        runner_logs.append(log_path)
        runner_handles.append(handle)
        process = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=apply_runner_env(dict(runner_env)),
            cwd=compat_runner_cwd(),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        runner_box["runner"] = process
        return process

    def _restart_runner() -> None:
        _stop(runner_box.get("runner"), grace=15)
        process = _spawn_runner()
        _wait_until_ready(base_url, runner_id, server, process, logs=(server_log, runner_logs[-1]))

    def _teardown() -> None:
        try:
            _stop(runner_box.get("runner"), grace=10)
            _stop(server, grace=10)
        finally:
            server_handle.close()
            for handle in runner_handles:
                handle.close()

    try:
        server = subprocess.Popen(
            server_argv, env=server_env, stdout=server_handle, stderr=subprocess.STDOUT
        )
        runner = _spawn_runner()
        _wait_until_ready(base_url, runner_id, server, runner, logs=(server_log, runner_logs[-1]))
    except BaseException:
        _teardown()
        raise
    environment = CodexNativeFaultEnvironment(
        base_url=base_url,
        runner_id=runner_id,
        root=root,
        home=home,
        data_dir=data_dir,
        proxy_log=proxy_log,
        fault_flag=fault_flag,
        server_log=server_log,
        mock_llm_url=mock_llm_url,
        runner_logs=runner_logs,
        _restart_runner=_restart_runner,
    )
    return environment, _teardown
