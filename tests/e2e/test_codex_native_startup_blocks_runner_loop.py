"""Native Codex startup must not block on redundant Databricks discovery.

Regression guard: a native Codex launch that pins the exact canonical
``system.ai.*`` model must not rediscover the workspace's model listing, and
the launch build must not freeze the runner's event loop for other sessions.

User journey (driven for real against a spawned server + runner):

1. Native Codex is configured to route through a Databricks *profile* whose
   workspace answers the Unity Catalog model listing slowly (a stub that
   delays ``/api/2.1/unity-catalog/model-services`` by
   :data:`_DISCOVERY_DELAY_S`).
2. The codex wrapper spec pins the *exact canonical* model
   ``system.ai.gpt-5-6-sol`` -- an id the gateway already serves, so no
   rediscovery should be needed to build the launch.
3. A plain ``hello_world`` session (B) is created on the same runner as a
   loop-liveness probe.
4. The codex-native session (A) is created; binding it makes the runner
   auto-create A's Codex terminal via ``build_codex_native_server()``.

Two observable symptoms are asserted, each keyed to the reported bug:

* **Facet 1 -- redundant model discovery.** Even though A pins the exact
  canonical ``system.ai.*`` model, the runner still issues the workspace
  model-services listing (``_resolve_databricks_codex_model`` runs credential
  resolution + live discovery before its ``if requested`` short-circuit). The
  regression contract is that a canonical pin must *not* trigger rediscovery,
  so the test asserts the discovery endpoint was **not** hit -- it fails on the
  buggy build (the listing *is* hit) and passes once the pin short-circuits.

* **Facet 2 -- frozen runner event loop.** ``build_codex_native_server()`` runs
  synchronously on the runner's asyncio loop, so while the slow discovery
  dependency blocks, the runner cannot service *any* other session. The test
  hammers session B's runner-served filesystem endpoint from a background
  thread and asserts no round-trip stalls near the discovery delay. It fails on
  the buggy build (B's endpoint stalls for ~the full discovery delay) and
  passes once the blocking builder work is offloaded off the event loop.

Both assertions encode the desired post-fix behavior, so this test is
red on the current build and turns green when the fix lands.

Usage::

    python -m pytest \
        tests/e2e/test_codex_native_startup_blocks_runner_loop.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact canonical id the workspace serves. A launch that pins this must
# not need credential acquisition or a live model listing to build.
_PINNED_MODEL = "system.ai.gpt-5-6-sol"

# How long the stub workspace stalls the Unity Catalog model listing. On the
# buggy build this whole delay lands on the runner's event loop (the discovery
# call is synchronous inside build_codex_native_server), so it doubles as the
# freeze the loop-liveness probe measures. Kept modest so the test is quick
# while still leaving a wide margin over the stall threshold below.
_DISCOVERY_DELAY_S = 6.0

# A probe round-trip longer than this counts as a runner-loop freeze. Baseline
# round-trips against the runner-served endpoint are ~0.1-0.2s; a frozen loop
# stalls for ~the full discovery delay. 3.0s sits well above baseline jitter
# and well below _DISCOVERY_DELAY_S, so the classification is unambiguous.
_STALL_THRESHOLD_S = 3.0

_UC_MODEL_SERVICES_PATH = "/api/2.1/unity-catalog/model-services"

_HEALTH_TIMEOUT_S = 120.0
_HEALTH_POLL_S = 0.5

_HELLO_AGENT_YAML = """\
name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _free_port() -> int:
    """Return an OS-assigned free localhost TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _StubState:
    """Shared record of requests the stub workspace received."""

    lock = threading.Lock()
    paths: list[str] = []

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.paths = []

    @classmethod
    def saw_model_discovery(cls) -> bool:
        with cls.lock:
            return any(p.startswith(_UC_MODEL_SERVICES_PATH) for p in cls.paths)


class _StubDatabricksHandler(BaseHTTPRequestHandler):
    """A Databricks workspace whose UC model listing answers slowly.

    Only the Unity Catalog model-services listing is delayed and answered with
    the canonical id the launch already pins; every other path 404s fast. The
    delay is what freezes the runner loop on the buggy build, and the request
    log is what proves rediscovery ran despite the canonical pin.
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:  # silence access logging
        pass

    def _record(self) -> None:
        with _StubState.lock:
            _StubState.paths.append(self.path)

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record()
        if self.path.startswith(_UC_MODEL_SERVICES_PATH):
            time.sleep(_DISCOVERY_DELAY_S)
            self._send_json(
                200,
                {"model_services": [{"name": f"model-services/{_PINNED_MODEL}"}]},
            )
            return
        self._send_json(404, {"error_code": "NOT_FOUND", "message": "nope"})

    do_POST = do_GET


def _wait_healthy(
    base_url: str,
    runner_id: str,
    server_proc: subprocess.Popen[bytes],
    runner_proc: subprocess.Popen[bytes],
) -> None:
    """Block until the server is healthy and the runner is online."""
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    last = "not polled"
    while time.monotonic() < deadline:
        if server_proc.poll() is not None:
            raise RuntimeError(f"server exited early rc={server_proc.returncode}")
        if runner_proc.poll() is not None:
            raise RuntimeError(f"runner exited early rc={runner_proc.returncode}")
        try:
            health = httpx.get(f"{base_url}/health", timeout=2, trust_env=False)
            status = httpx.get(
                f"{base_url}/v1/runners/{runner_id}/status", timeout=2, trust_env=False
            )
            if (
                health.status_code == 200
                and status.status_code == 200
                and status.json().get("online") is True
            ):
                return
            last = f"health={health.status_code} status={status.status_code}"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_HEALTH_POLL_S)
    raise RuntimeError(f"server/runner not healthy in {_HEALTH_TIMEOUT_S:.0f}s (last={last})")


def _create_session(
    base_url: str,
    runner_id: str,
    yaml_text: str,
    arcname: str,
    metadata: dict[str, object],
) -> str:
    """Register a bundled agent, create its session, and bind the runner.

    :returns: The new session/conversation id.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo(arcname)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": (f"{arcname}.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
        trust_env=False,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    patch = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
        trust_env=False,
    )
    patch.raise_for_status()
    return session_id


@pytest.fixture
def codex_native_databricks_env(tmp_path: Path) -> Iterator[dict[str, object]]:
    """Spawn a server + runner routing native Codex through a slow workspace.

    Yields the pieces the test drives: ``base_url``, ``runner_id``, the stub's
    request state, a workspace dir for the codex session, and the plain
    ``hello_world`` agent YAML.
    """
    codex_path = shutil.which("codex") or os.environ.get("OMNIGENT_CODEX_PATH")
    if not codex_path:
        pytest.skip("codex CLI is required for native Codex startup e2e")

    _StubState.reset()
    stub_port = _free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), _StubDatabricksHandler)
    stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    stub_thread.start()

    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        """providers:
  dbx-e2e-repro:
    kind: databricks
    profile: repro
    default: openai
""",
        encoding="utf-8",
    )
    dbcfg = tmp_path / "databrickscfg"
    dbcfg.write_text(
        f"[repro]\nhost = http://127.0.0.1:{stub_port}\ntoken = dummy-token\n",
        encoding="utf-8",
    )

    home_dir = tmp_path / "home"
    source_codex_home = tmp_path / "source-codex-home"
    state_dir = tmp_path / "codex-native-state"
    artifact_dir = tmp_path / "artifacts"
    workdir = tmp_path / "workspace"
    for path in (home_dir, source_codex_home, state_dir, artifact_dir, workdir):
        path.mkdir(parents=True, exist_ok=True)

    agent_yaml = tmp_path / "hello_world.yaml"
    agent_yaml.write_text(_HELLO_AGENT_YAML, encoding="utf-8")

    binding_token = secrets.token_urlsafe(32)
    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    # Strip ambient Databricks/omnigent + proxy vars so the staged profile and
    # loopback wiring are authoritative (CI runners carry Databricks creds; a
    # leaked proxy routes the loopback stub through an unreachable proxy).
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABRICKS_", "OMNIGENT_"))
        and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
    }
    shared_env = {
        **env,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "DATABRICKS_CONFIG_FILE": str(dbcfg),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
        "OMNIGENT_CODEX_PATH": str(codex_path),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = tmp_path / "server.log"
    runner_log = tmp_path / "runner.log"
    server_handle = open(server_log, "w")  # noqa: SIM115 - lives for Popen's lifetime
    runner_handle = open(runner_log, "w")  # noqa: SIM115
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
            f"sqlite:///{tmp_path / 'test.db'}",
            "--artifact-location",
            str(artifact_dir),
            "--agent",
            str(agent_yaml),
        ],
        env=server_env,
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )
    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=runner_env,
        stdout=runner_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        try:
            _wait_healthy(base_url, runner_id, server_proc, runner_proc)
        except RuntimeError as exc:
            server_handle.flush()
            runner_handle.flush()
            raise RuntimeError(
                f"{exc}\nserver log:\n{server_log.read_text()[-2000:]}\n"
                f"runner log:\n{runner_log.read_text()[-2000:]}"
            ) from exc
        yield {
            "base_url": base_url,
            "runner_id": runner_id,
            "stub": _StubState,
            "workdir": workdir,
            "hello_yaml": _HELLO_AGENT_YAML,
        }
    finally:
        for proc in (runner_proc, server_proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        runner_handle.close()
        server_handle.close()
        stub.shutdown()
        stub_thread.join(timeout=5)


@pytest.mark.timeout(240)
def test_codex_native_startup_does_not_block_runner_loop(
    codex_native_databricks_env: dict[str, object],
) -> None:
    """A canonical-pinned native Codex launch must not rediscover or freeze."""
    base_url = str(codex_native_databricks_env["base_url"])
    runner_id = str(codex_native_databricks_env["runner_id"])
    workdir = codex_native_databricks_env["workdir"]
    hello_yaml = str(codex_native_databricks_env["hello_yaml"])
    assert isinstance(workdir, Path)

    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    # ---- session B: a plain agent on the same runner, our loop probe ----
    b_sid = _create_session(base_url, runner_id, hello_yaml, "hello_world.yaml", {})
    b_fs_url = f"{base_url}/v1/sessions/{b_sid}/resources/environments/default/filesystem?limit=5"

    # Wait for B's runner-served endpoint to answer, then measure a baseline.
    baseline_deadline = time.monotonic() + 90
    while time.monotonic() < baseline_deadline:
        try:
            resp = httpx.get(b_fs_url, timeout=5, trust_env=False)
            if resp.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    else:
        pytest.fail("session B filesystem endpoint never became ready")

    baseline: list[float] = []
    for _ in range(5):
        start = time.monotonic()
        httpx.get(b_fs_url, timeout=10, trust_env=False)
        baseline.append(time.monotonic() - start)
    assert max(baseline) < _STALL_THRESHOLD_S, (
        f"session B baseline round-trips already exceed the stall threshold "
        f"({baseline}); the runner is not idle-healthy before the test starts"
    )

    # ---- background probe: hammer B's endpoint through codex startup ----
    probe_max: dict[str, float] = {"stall": 0.0}
    probe_stop = threading.Event()

    def _probe() -> None:
        client = httpx.Client(timeout=60, trust_env=False)
        try:
            while not probe_stop.is_set():
                start = time.monotonic()
                with contextlib.suppress(httpx.HTTPError):
                    client.get(b_fs_url)
                elapsed = time.monotonic() - start
                if elapsed > probe_max["stall"]:
                    probe_max["stall"] = elapsed
                probe_stop.wait(0.2)
        finally:
            client.close()

    probe_thread = threading.Thread(target=_probe, daemon=True)
    probe_thread.start()

    try:
        # ---- session A: codex-native pinned to the exact canonical model ----
        import tempfile

        from omnigent.codex_native import _materialize_codex_agent_spec

        with tempfile.TemporaryDirectory() as spec_tmp:
            spec_path = _materialize_codex_agent_spec(Path(spec_tmp), model=_PINNED_MODEL)
            codex_yaml = spec_path.read_text(encoding="utf-8")
        assert f"model: {_PINNED_MODEL}" in codex_yaml
        assert "harness: codex-native" in codex_yaml

        a_sid = _create_session(
            base_url,
            runner_id,
            codex_yaml,
            "codex-native-ui.yaml",
            {
                "labels": {
                    UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                    WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
                },
                "workspace": str(workdir),
            },
        )

        # Let the runner auto-create A's terminal (which builds the codex
        # launch) and finish the slow discovery window, while the probe runs.
        deadline = time.monotonic() + _DISCOVERY_DELAY_S + 20
        terminal_seen = False
        while time.monotonic() < deadline:
            try:
                resources = httpx.get(
                    f"{base_url}/v1/sessions/{a_sid}/resources", timeout=30, trust_env=False
                )
                if resources.status_code == 200 and "terminal" in resources.text:
                    terminal_seen = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        assert terminal_seen, "codex-native session A never surfaced its terminal resource"
    finally:
        probe_stop.set()
        probe_thread.join(timeout=70)

    stub = codex_native_databricks_env["stub"]
    saw_discovery = stub.saw_model_discovery()  # type: ignore[union-attr]

    # --- Facet 1: an exact canonical pin must not trigger rediscovery. ---
    assert not saw_discovery, (
        "native Codex ran Databricks model discovery "
        f"({_UC_MODEL_SERVICES_PATH}) even though the launch pinned the exact "
        f"canonical model {_PINNED_MODEL!r}; a canonical pin must not need "
        "credential acquisition or a live model listing to build the launch"
    )

    # --- Facet 2: the synchronous builder must not freeze the runner loop. ---
    assert probe_max["stall"] < _STALL_THRESHOLD_S, (
        "an unrelated session's runner-served endpoint stalled for "
        f"{probe_max['stall']:.2f}s (baseline max {max(baseline):.2f}s) during "
        "native Codex startup; build_codex_native_server() blocked the runner "
        "event loop instead of offloading its slow work off the loop"
    )
