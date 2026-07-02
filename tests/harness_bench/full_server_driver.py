"""Full-server transport driver (phase-2).

Unlike :class:`tests.harness_bench.driver.SdkInprocDriver` (which drives a
harness wrap subprocess directly), this driver spins up a REAL Omnigent
``server`` + ``runner`` pair, registers an agent, and drives turns through
the full session path — so policy enforcement and server-dispatched tools
are exercised the way production does, not simulated at the wrap boundary.

It reuses the exact spawn recipe of the e2e ``live_server`` fixture
(``tests/e2e/conftest.py``) via the shared compat helpers, but packaged as
a plain async context manager so the bench CLI can drive it without pytest.

Status (live-verified): server+runner lifecycle, a basic turn, and the
payoff this transport exists for — a real **server-dispatched tool call**
(a read-only builtin) and **tool-call policy enforcement** (a spec-baked
``tool_call`` deny policy blocks the call the way production does). Ad-hoc
request-level function tools are NOT used here: the SDK harnesses handle
tools internally, so they never round-trip as a server-dispatched, policy-
gated call — a builtin does.

Interrupt/cancel is verified (a long turn is interrupted mid-flight and the
server's cancellation marker confirms it stopped), and delta-level
streaming is measured via the ``/v1/sessions/{id}/stream`` SSE subscribe.

Follow-up (stacked PR): a ``--transport`` selector + driver registry so the
bench's probes run through this driver, not just its gated tests.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

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
from tests.harness_bench.driver import TurnResult
from tests.harness_bench.profile import BenchProfile

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
_HEALTH_TIMEOUT_S = 90.0
_POLL_INTERVAL_S = 0.2

# The builtin the tool/policy probes drive: read-only, zero setup, server-
# dispatched, and gated at the tool_call phase. Its denial output carries
# _DENY_REASON so a blocked call is unambiguous.
_TOOL_NAME = "list_files"
_DENY_REASON = "bench-policy-deny"
_TOOL_PROMPT = f"List the files using the {_TOOL_NAME} tool, then tell me how many there are."

# The server persists an interrupted turn as a synthetic user message whose
# text contains this marker (see tests/e2e/test_cancel_history.py).
_CANCELLATION_MARKER = "interrupted"
_LONG_PROMPT = (
    "Write a very detailed 600-word essay about the history of computing, in full paragraphs."
)

# Prompt long enough that a streaming harness emits clearly many deltas.
_STREAM_PROMPT = (
    "Count from 1 to 30 in words, one number per line, and add a short note after each."
)
_TERMINAL_EVENTS = frozenset({"response.completed", "response.failed", "response.cancelled"})


def _find_free_port() -> int:
    import socket

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


class FullServerDriver:
    """Drive turns through a live Omnigent server + runner.

    Async context manager: on enter it spawns the server and runner,
    waits for both to report healthy, registers *profile*'s harness as an
    agent, and creates a runner-bound session. ``run_turn`` drives one turn
    through that session.
    """

    transport = "full-server"

    def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
        self._profile = profile
        self._db_profile = databricks_profile
        self._proc: subprocess.Popen[bytes] | None = None
        self._runner: subprocess.Popen[bytes] | None = None
        self._logs: list[Path] = []
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        # A second agent+session whose spec bakes a tool_call deny policy,
        # created lazily for the policy probe (the REST policy endpoint's
        # handler allowlist excludes make_fixed_action_callable, so the deny
        # must ride in the agent spec instead).
        self._deny_session_id: str | None = None
        self._runner_id = ""
        self._base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-fs-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else ``None``."""
        if not databricks_profile:
            return "no --profile / databricks profile provided; full-server needs a gateway route"
        if lookup_databricks_host(databricks_profile) is None:
            return (
                f"databricks profile {databricks_profile!r} missing/hostless in ~/.databrickscfg"
            )
        # Reuse the wrap driver's CLI gate (same binary requirement).
        from tests.harness_bench.driver import SdkInprocDriver

        return SdkInprocDriver.unavailable(profile, databricks_profile=databricks_profile)

    def __enter__(self) -> FullServerDriver:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        host = lookup_databricks_host(self._db_profile)
        assert host is not None  # guaranteed by unavailable()
        bearer = _mint_bearer(self._db_profile)
        port = _find_free_port()
        self._base_url = f"http://localhost:{port}"

        binding_token = uuid.uuid4().hex
        runner_id = token_bound_runner_id(binding_token)

        base_env = {
            **os.environ,
            "OPENAI_API_KEY": bearer,
            "OPENAI_BASE_URL": f"{host}/serving-endpoints",
            "DATABRICKS_CONFIG_PROFILE": self._db_profile,
        }
        apply_server_env(base_env, _REPO_ROOT)

        self._proc = self._spawn_server(port, base_env, binding_token)
        self._runner = self._spawn_runner(base_env, runner_id, binding_token)
        self._wait_ready(runner_id)

        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        self._runner_id = runner_id
        agent_name = self._register_agent(deny=False)
        self._session_id = self._create_session(agent_name, runner_id)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.close()
        for proc in (self._runner, self._proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── spawn ────────────────────────────────────────────────

    def _spawn_server(
        self, port: int, base_env: dict[str, str], binding_token: str
    ) -> subprocess.Popen[bytes]:
        db_path = self._tmp / "bench.db"
        artifact_dir = self._tmp / "artifacts"
        artifact_dir.mkdir(exist_ok=True)
        log = self._tmp / "server.log"
        self._logs.append(log)
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

    def _spawn_runner(
        self, base_env: dict[str, str], runner_id: str, binding_token: str
    ) -> subprocess.Popen[bytes]:
        log = self._tmp / "runner.log"
        self._logs.append(log)
        runner_env = apply_runner_env(
            {
                **base_env,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": self._base_url,
            }
        )
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=runner_env,
            cwd=compat_runner_cwd(),
            stdout=log.open("wb"),
            stderr=subprocess.STDOUT,
        )

    def _wait_ready(self, runner_id: str) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                health = httpx.get(f"{self._base_url}/health", timeout=2)
                status = httpx.get(f"{self._base_url}/v1/runners/{runner_id}/status", timeout=2)
                if (
                    health.status_code == 200
                    and status.status_code == 200
                    and status.json().get("online") is True
                ):
                    return
            except httpx.HTTPError:
                # Connection refused / read errors are expected while the
                # server and runner are still coming up; keep polling until
                # they answer or the timeout below fires.
                pass
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(
            f"server+runner not ready within {_HEALTH_TIMEOUT_S}s; logs in {self._tmp}"
        )

    # ── agent + session ──────────────────────────────────────

    def _register_agent(self, *, deny: bool) -> str:
        import io
        import tarfile

        import yaml

        assert self._client is not None
        name = f"bench-{self._profile.harness}" + ("-deny" if deny else "")
        config: dict[str, Any] = {
            "spec_version": 1,
            "name": name,
            "prompt": "You are a helpful assistant used for capability testing.",
            "executor": {
                "type": "omnigent",
                "model": self._profile.model,
                "profile": self._db_profile,
                "config": {"harness": self._profile.harness},
            },
            # A read-only builtin the server dispatches (and gates at the
            # tool_call phase). The tool/policy probes drive a call to it;
            # it is harmless for basic turns (the model just won't call it).
            "tools": {"builtins": [_TOOL_NAME]},
        }
        if deny:
            # Bake a tool_call-phase deny on the builtin so the server blocks
            # the call the way production policy enforcement does.
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
        # spec_version bundles load via the directory spec loader, which
        # recognizes tools.builtins and expects the member named config.yaml.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = yaml.safe_dump(config).encode()
            info = tarfile.TarInfo("config.yaml")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        resp = self._client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({})},
            files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        )
        if resp.status_code not in (200, 201, 409):
            raise RuntimeError(f"agent register failed: {resp.status_code} {resp.text[:400]}")
        return name

    def _create_session(self, agent_name: str, runner_id: str) -> str:
        assert self._client is not None
        listing = self._client.get("/v1/sessions", params={"agent_name": agent_name, "limit": 1})
        listing.raise_for_status()
        agent_id = str(listing.json()["data"][0]["agent_id"])
        created = self._client.post("/v1/sessions", json={"agent_id": agent_id})
        created.raise_for_status()
        session_id = str(created.json()["id"])
        bound = self._client.patch(f"/v1/sessions/{session_id}", json={"runner_id": runner_id})
        bound.raise_for_status()
        return session_id

    def _ensure_deny_session(self) -> str:
        """Lazily register the deny agent and its session; return the session id."""
        if self._deny_session_id is None:
            name = self._register_agent(deny=True)
            self._deny_session_id = self._create_session(name, self._runner_id)
        return self._deny_session_id

    # ── tool / policy probe ──────────────────────────────────

    def tool_probe_turn(self, *, deny: bool, timeout: float = 180.0) -> TurnResult:
        """Drive a turn that calls the builtin tool; return a :class:`TurnResult`.

        On the full-server transport a tool call is real and server-
        dispatched. With *deny* the turn runs against a session whose agent
        bakes a ``tool_call`` deny policy, so the server blocks the call and
        the tool output carries the deny reason.

        Fills :attr:`TurnResult.tool_calls` (the builtin call) and
        :attr:`TurnResult.tool_call_denied` (whether the server blocked it),
        plus ``completed`` / ``failed`` / ``text``.
        """
        assert self._client is not None
        sid = self._ensure_deny_session() if deny else self._session_id
        assert sid is not None
        result = TurnResult()
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": _TOOL_PROMPT}]},
        }
        self._client.post(f"/v1/sessions/{sid}/events", json=body).raise_for_status()

        deadline = time.monotonic() + timeout
        seen_running = False
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{sid}").json()
            status = snap.get("status")
            items = snap.get("items", [])
            if status in ("running", "waiting"):
                seen_running = True
            if status == "failed":
                result.failed = True
                result.error = snap.get("last_task_error") or snap.get("error")
                break
            if status == "idle" and seen_running:
                result.completed = True
                _scan_tool_items(items, result)
                result.text = _assistant_text(items)
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result

    # ── streaming probe ──────────────────────────────────────

    def streaming_probe_turn(self, *, timeout: float = 120.0) -> TurnResult:
        """Measure token-level streaming via the session SSE subscribe stream.

        The full-server stream (``GET /v1/sessions/{id}/stream``) is separate
        from the message POST, so a background thread subscribes and counts
        ``response.output_text.delta`` events while the main thread posts the
        turn. More than one delta means the harness streams incrementally.
        """
        assert self._client is not None and self._session_id is not None
        sid = self._session_id
        result = TurnResult()
        done = threading.Event()

        def _read_stream() -> None:
            try:
                with self._client.stream(  # type: ignore[union-attr]
                    "GET", f"/v1/sessions/{sid}/stream", timeout=timeout
                ) as resp:
                    for line in resp.iter_lines():
                        if not line.startswith("event:"):
                            continue
                        etype = line[len("event:") :].strip()
                        if etype == "response.output_text.delta":
                            result.text_delta_count += 1
                        elif etype in _TERMINAL_EVENTS:
                            result.completed = etype == "response.completed"
                            result.cancelled = etype == "response.cancelled"
                            result.failed = etype == "response.failed"
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)
            finally:
                done.set()

        reader = threading.Thread(target=_read_stream, daemon=True)
        reader.start()
        time.sleep(1.0)  # let the subscription register before the turn starts
        self._client.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": _STREAM_PROMPT}],
                },
            },
        ).raise_for_status()
        if not done.wait(timeout):
            result.timed_out = True
        return result

    # ── interrupt probe ──────────────────────────────────────

    def interrupt_probe_turn(self, *, timeout: float = 120.0) -> TurnResult:
        """Start a long turn, interrupt it mid-flight, and report the outcome.

        Posts an ``interrupt`` event once the turn is running (after a short
        hold so some text streams first), then waits for the server's
        cancellation marker. Sets :attr:`TurnResult.cancelled` when the
        marker appears — the honored-interrupt signal.
        """
        assert self._client is not None and self._session_id is not None
        sid = self._session_id
        result = TurnResult()
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": _LONG_PROMPT}]},
        }
        self._client.post(f"/v1/sessions/{sid}/events", json=body).raise_for_status()

        deadline = time.monotonic() + timeout
        interrupted = False
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{sid}").json()
            status = snap.get("status")
            items = snap.get("items", [])
            if status in ("running", "waiting") and not interrupted:
                # Let a little text stream so the interrupt lands mid-turn.
                time.sleep(1.5)
                self._client.post(f"/v1/sessions/{sid}/events", json={"type": "interrupt"})
                interrupted = True
            if _has_cancellation_marker(items):
                result.cancelled = True
                result.text = _assistant_text(items)
                break
            if status == "idle" and interrupted:
                # Settled after the interrupt; the marker lands just after.
                result.cancelled = _has_cancellation_marker(items)
                result.text = _assistant_text(items)
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result

    # ── turn ─────────────────────────────────────────────────

    def run_turn(self, prompt: str, *, timeout: float = 180.0) -> TurnResult:
        """Drive one basic turn through the full server, return a :class:`TurnResult`.

        Foundation scope: posts the user message and polls the session
        snapshot to a terminal state, filling ``text`` / ``completed`` /
        ``failed`` / ``timed_out``. A synchronous (request-phase) policy
        DENY short-circuits to ``failed``.

        The dimensions that motivated this transport — server-dispatched
        tools, tool-call policy enforcement, delta streaming, interrupt —
        are follow-ups (see the module docstring); they extend this
        signature and are not implemented yet.
        """
        assert self._client is not None and self._session_id is not None
        result = TurnResult()
        body: dict[str, Any] = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        }
        posted = self._client.post(f"/v1/sessions/{self._session_id}/events", json=body)
        if posted.status_code == 202 and posted.json().get("denied"):
            result.failed = True
            result.error = {"denied": True, "reason": posted.json().get("reason")}
            return result
        posted.raise_for_status()

        deadline = time.monotonic() + timeout
        seen_running = False
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{self._session_id}")
            snap.raise_for_status()
            body = snap.json()
            status = body.get("status")
            if status in ("running", "waiting"):
                seen_running = True
            if status == "failed":
                result.failed = True
                result.error = body.get("last_task_error") or body.get("error")
                break
            if status == "idle" and seen_running:
                result.completed = True
                result.text = _assistant_text(body.get("items", []))
                break
            time.sleep(_POLL_INTERVAL_S)
        else:
            result.timed_out = True
        return result


def _scan_tool_items(items: list[dict], result: TurnResult) -> None:
    """Populate tool_calls and tool_call_denied from session items."""
    for raw in items:
        data = raw.get("data", raw)
        itype = raw.get("type") or data.get("type")
        if itype == "function_call":
            result.tool_calls.append(
                {
                    "call_id": data.get("call_id"),
                    "name": data.get("name"),
                    "arguments": data.get("arguments"),
                }
            )
        elif itype == "function_call_output":
            out = str(data.get("output", ""))
            if data.get("status") == "blocked" or _DENY_REASON in out:
                result.tool_call_denied = True


def _has_cancellation_marker(items: list[dict]) -> bool:
    """Whether items include the synthetic 'interrupted' user message."""
    for raw in items:
        data = raw.get("data", raw)
        if (raw.get("type") == "message") and (data.get("role") == "user"):
            if any(
                _CANCELLATION_MARKER in (b.get("text", "") or "")
                for b in data.get("content", []) or []
            ):
                return True
    return False


def _assistant_text(items: list[dict]) -> str:
    """Concatenate assistant output_text from session items."""
    out: list[str] = []
    for item in items:
        data = item.get("data", item)
        if data.get("role") == "assistant" or item.get("role") == "assistant":
            for block in data.get("content", []) or []:
                if block.get("type") in ("output_text", "text"):
                    out.append(block.get("text", ""))
    return "\n".join(t for t in out if t)
