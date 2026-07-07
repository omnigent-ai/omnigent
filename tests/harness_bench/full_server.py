"""Shared full-server infrastructure: spawn a real Omnigent server + runner.

Split from :mod:`tests.harness_bench.full_server_driver` so the *server
lifecycle* (spawning the server/runner, minting a bearer, registering bench
agents + sessions) lives apart from the *driver* that runs probes against it.
Two consumers use this:

- :class:`~tests.harness_bench.full_server_driver.FullServerDriver` — one
  harness per :class:`SharedFullServer` (solo run), or several harnesses on one
  shared server (parallel run; see ``bench.run_bench``).
- :mod:`tests.harness_bench.native_tui_driver` reuses the lower-level spawn
  helpers (:func:`spawn_omnigent_server`, :func:`_mint_bearer`,
  :func:`_find_free_port`) for its own server + host-daemon topology.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests.e2e.helpers import lookup_databricks_host
from tests.harness_bench.profile import BenchProfile

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_HEALTH_TIMEOUT_S = 90.0
_POLL_INTERVAL_S = 0.2

# The builtin the tool/policy probes drive: read-only, zero setup, server-
# dispatched, and gated at the tool_call phase. Its denial output carries
# _DENY_REASON so a blocked call is unambiguous.
_TOOL_NAME = "list_files"
_DENY_REASON = "bench-policy-deny"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _mint_bearer(profile: str) -> str:
    """Mint a Databricks bearer for *profile* via the CLI (isolated from ambient token env).

    ``env -u DATABRICKS_TOKEN -u DATABRICKS_BEARER`` guards against a stale
    ambient credential shadowing profile auth (see omnigent issue #1781).
    """
    proc = subprocess.run(
        ["databricks", "auth", "token", "--profile", profile, "--output", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        env={
            k: v
            for k, v in os.environ.items()
            if k not in ("DATABRICKS_TOKEN", "DATABRICKS_BEARER")
        },
    )
    return str(json.loads(proc.stdout)["access_token"])


def spawn_omnigent_server(
    tmp: Path, port: int, base_env: dict[str, str], binding_token: str
) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent server`` subprocess writing state under *tmp*.

    Shared by the full-server and native-tui drivers (both need the same
    server; only what connects to it differs — a bare runner vs a host
    daemon). Writes ``server.log`` / ``bench.db`` / ``artifacts`` under *tmp*.
    """
    db_path = tmp / "bench.db"
    artifact_dir = tmp / "artifacts"
    artifact_dir.mkdir(exist_ok=True)
    log = tmp / "server.log"
    args = [
        server_executable(),
        "-m",
        "omnigent.cli",
        "server",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
    ]
    return subprocess.Popen(
        args,
        env={**base_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
        cwd=compat_server_cwd(),
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
    )


def _spawn_bench_runner(
    tmp: Path, base_env: dict[str, str], runner_id: str, binding_token: str, base_url: str
) -> subprocess.Popen[bytes]:
    """Spawn a bench runner bound to *base_url* (the full-server execution sandbox)."""
    log = tmp / "runner.log"
    runner_env = apply_runner_env(
        {
            **base_env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
        }
    )
    return subprocess.Popen(
        [runner_executable(), "-m", "omnigent.runner._entry"],
        env=runner_env,
        cwd=compat_runner_cwd(),
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
    )


def _wait_server_runner_ready(base_url: str, runner_id: str) -> None:
    """Poll until the server is healthy and *runner_id* reports online."""
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            health = httpx.get(f"{base_url}/health", timeout=2)
            status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
            if (
                health.status_code == 200
                and status.status_code == 200
                and status.json().get("online") is True
            ):
                return
        except httpx.HTTPError:
            # Connection refused / read errors are expected while the server
            # and runner are still coming up; keep polling until the timeout.
            pass
        time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        f"server+runner not ready within {_HEALTH_TIMEOUT_S}s; logs near {base_url}"
    )


def _build_bench_agent_config(
    profile: BenchProfile, db_profile: str, *, deny: bool
) -> dict[str, Any]:
    """The agent spec for a bench harness: the harness + the read-only builtin,
    plus (when *deny*) a baked tool_call-phase deny on that builtin."""
    name = f"bench-{profile.harness}" + ("-deny" if deny else "")
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "prompt": "You are a helpful assistant used for capability testing.",
        "executor": {
            "type": "omnigent",
            "model": profile.model,
            "profile": db_profile,
            "config": {"harness": profile.harness},
        },
        # A read-only builtin the server dispatches (and gates at the tool_call
        # phase). The tool/policy probes drive a call to it; harmless for basic
        # turns (the model just won't call it).
        "tools": {"builtins": [_TOOL_NAME]},
    }
    if deny:
        config["guardrails"] = {
            "policies": {
                "deny_tool": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.policies.function.make_fixed_action_callable",
                        "arguments": {
                            "action": "deny",
                            "reason": _DENY_REASON,
                            "on_phases": ["tool_call"],
                            "on_tools": [_TOOL_NAME],
                        },
                    },
                }
            }
        }
    return config


def _bundle_agent_config(config: dict[str, Any]) -> bytes:
    """Gzip-tar a spec_version agent config as the ``config.yaml`` bundle member."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = yaml.safe_dump(config).encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


class SharedFullServer:
    """One server + one runner shared by several full-server harnesses.

    The Omnigent server is multi-agent/multi-session, and a single runner
    resolves the harness type per session from that session's agent spec (see
    ``runner/app.py``). So N SDK harnesses do NOT each need their own
    server+runner — they can each register as their own agent + session on one
    shared pair, with the runner spawning the right harness subprocess per
    session. Under ``--jobs`` > 1 this replaces N server boots + N runners with
    one, cutting the heaviest, slowest part of full-server startup.

    Sync context manager (spawn/health-wait are blocking); the orchestrator
    bridges it via ``asyncio.to_thread``. ``register_agent`` / ``create_session``
    are the per-harness operations a ``FullServerDriver`` calls against it.
    """

    def __init__(self, db_profile: str) -> None:
        self._db_profile = db_profile
        self._proc: subprocess.Popen[bytes] | None = None
        self._runner: subprocess.Popen[bytes] | None = None
        self.client: httpx.Client | None = None
        self.runner_id = ""
        self.base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-fs-shared-{uuid.uuid4().hex[:8]}"

    def __enter__(self) -> SharedFullServer:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        host = lookup_databricks_host(self._db_profile)
        assert host is not None
        bearer = _mint_bearer(self._db_profile)
        port = _find_free_port()
        self.base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex
        self.runner_id = token_bound_runner_id(binding_token)
        base_env = {
            **os.environ,
            "OPENAI_API_KEY": bearer,
            "OPENAI_BASE_URL": f"{host}/serving-endpoints",
            "DATABRICKS_CONFIG_PROFILE": self._db_profile,
        }
        apply_server_env(base_env, _REPO_ROOT)
        self._proc = spawn_omnigent_server(self._tmp, port, base_env, binding_token)
        self._runner = _spawn_bench_runner(
            self._tmp, base_env, self.runner_id, binding_token, self.base_url
        )
        _wait_server_runner_ready(self.base_url, self.runner_id)
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self.client is not None:
            self.client.close()
        for proc in (self._runner, self._proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def register_agent(self, profile: BenchProfile, *, deny: bool) -> str:
        """Register a bench agent for *profile*; return its agent name."""
        assert self.client is not None
        config = _build_bench_agent_config(profile, self._db_profile, deny=deny)
        resp = self.client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", _bundle_agent_config(config), "application/gzip")},
        )
        if resp.status_code not in (200, 201, 409):
            raise RuntimeError(f"agent register failed: {resp.status_code} {resp.text[:400]}")
        return str(config["name"])

    def create_session(self, agent_name: str) -> str:
        """Create a runner-bound session for a registered agent name."""
        assert self.client is not None
        listing = self.client.get("/v1/sessions", params={"agent_name": agent_name, "limit": 1})
        listing.raise_for_status()
        agent_id = str(listing.json()["data"][0]["agent_id"])
        created = self.client.post("/v1/sessions", json={"agent_id": agent_id})
        created.raise_for_status()
        session_id = str(created.json()["id"])
        bound = self.client.patch(f"/v1/sessions/{session_id}", json={"runner_id": self.runner_id})
        bound.raise_for_status()
        return session_id


__all__ = [
    "SharedFullServer",
    "spawn_omnigent_server",
]
