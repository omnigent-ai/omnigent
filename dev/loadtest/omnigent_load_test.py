"""Load test: each Locust user is a real Omnigent host running real turns.

One unit of load = one **host**. Each Locust user spawns a real
``omnigent host`` subprocess (a unique host identity), registers it with the
target server over the host tunnel, then repeatedly: creates a **host-bound
session** and drives **real multi-turn conversations** on it — every turn is a
genuine ``POST .../events`` → server → the user's host → a runner subprocess it
spawns → LLM → stream → ``idle`` loop. The LLM is **mocked** (zero latency), so
the numbers isolate Omnigent's own dispatch / streaming / history overhead, not
provider time. ``-u N`` scales the number of hosts.

Because turns really execute on the host, each host spawns a **real runner
subprocess per session**, and every Locust user runs a **real host subprocess**
— so N users × M sessions puts N×M runner processes on *this* (load-generating)
machine. This is deliberately capacity-limited: it drives genuine end-to-end
turns rather than faking the runner, so it stresses the real host↔server↔runner
path but does not scale to hundreds on one box. Keep N modest (a few dozen).

This is driven by ``run.py``, which boots the whole local stack (server + mock
LLM) first and passes the wiring in via the environment below. It is not meant
to run against a bare ``locust -f`` invocation — turns need the mock LLM and a
registered agent that ``run.py`` sets up.

Environment (set by ``run.py``):

    LOADTEST_SERVER_URL   Local server the hosts register with (Locust --host).
    LOADTEST_AGENT_ID     Registered agent id every host-bound session uses.
    LOADTEST_MOCK_URL     Mock LLM base URL the hosts' runners are pointed at.
    LOADTEST_WORKSPACE_ROOT  Dir under which each host gets a workspace subdir.
    SESSIONS_PER_USER     Sessions each user drives, sequentially (default 2).
    TURNS_PER_SESSION     Turns per session — history grows across them (default 4).
    HOST_ONLINE_TIMEOUT_S Seconds to wait for a spawned host to come online (default 90).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import gevent
from locust import HttpUser, task

# Sentinel Origin the server's CSRF/CSWSH guard trusts for a non-browser client.
INTERNAL_ORIGIN = "omnigent://internal"

# Rotating prompts so each turn sends distinct text and history keeps growing —
# the point is a real long conversation, not realistic dialogue.
_PROMPTS = [
    "Give me one interesting fact.",
    "Expand on that a little.",
    "How does it connect to what you said before?",
    "Summarize the conversation so far in one line.",
    "Now give me a different fact.",
    "What's a common misconception about it?",
]

# Poll cadence while waiting for a host to register / a turn to settle.
_POLL_INTERVAL_S = 0.5
# Cap on how long one turn may take (post → idle) before it's a failure.
_TURN_TIMEOUT_S = 180.0
# Settled session states after a turn.
_TERMINAL_STATES = frozenset({"idle", "failed"})


class HostUser(HttpUser):
    """One simulated Omnigent host: spawns a real host, drives real turns.

    ``on_start`` spawns the host subprocess and waits for it to register;
    the task creates host-bound sessions and drives turns; ``on_stop`` tears
    the host down (which reaps the runners it spawned).
    """

    def on_start(self) -> None:
        """Spawn this user's host subprocess and wait until it is online."""
        self._server = os.environ["LOADTEST_SERVER_URL"].rstrip("/")
        self._agent_id = os.environ["LOADTEST_AGENT_ID"]
        self._sessions_per_user = int(os.getenv("SESSIONS_PER_USER", "2"))
        self._turns_per_session = int(os.getenv("TURNS_PER_SESSION", "4"))
        online_timeout = float(os.getenv("HOST_ONLINE_TIMEOUT_S", "90"))

        # Unique identity so one machine can masquerade as many hosts: the
        # server keys hosts on (owner, host_id), and host identity comes purely
        # from these env vars (no config.yaml read/write).
        self._host_id = uuid.uuid4().hex
        self._host_name = f"loadtest-host-{self._host_id[:8]}"
        self._workspace = Path(os.environ["LOADTEST_WORKSPACE_ROOT"]) / self._host_name
        self._workspace.mkdir(parents=True, exist_ok=True)

        self._host_proc = self._spawn_host()
        start = time.monotonic()
        try:
            self._wait_online(online_timeout)
        except Exception as exc:  # noqa: BLE001 — a host that never registers is a failed sample
            self._fire("host online", start, exc=exc)
            self._stop_host()
            self._host_id = ""  # mark unusable; the task will no-op
            return
        self._fire("host online", start)

    def on_stop(self) -> None:
        """Terminate the host subprocess (reaping the runners it spawned)."""
        self._stop_host()

    # ── host subprocess lifecycle ────────────────────────────────────

    def _spawn_host(self) -> subprocess.Popen:
        """Launch ``python -m omnigent host`` with this user's identity + mock.

        Runs under the current interpreter (``sys.executable``) so it uses the
        same venv, never a stale PATH binary. The mock LLM is wired via
        ``OPENAI_*`` env, which the host propagates to the runners it spawns.
        """
        # Each host needs its OWN HOME. The host-daemon singleton guard keys its
        # pidfile + daemon-record on ~/.omnigent (a module-level path captured
        # from Path.home() — it does NOT honor OMNIGENT_DATA_DIR/CONFIG_HOME), so
        # hosts sharing $HOME refuse to start ("a host daemon is already running
        # for this server"). A per-host $HOME moves ~/.omnigent entirely
        # (pidfile, daemon registry, identity, local db), giving each host its
        # own singleton scope. Identity is still pinned via the env vars below so
        # nothing is read from / written to the shared config.
        host_home = self._workspace / "home"
        host_home.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "HOME": str(host_home),
            "OMNIGENT_HOST_ID": self._host_id,
            "OMNIGENT_HOST_NAME": self._host_name,
            "OPENAI_BASE_URL": f"{os.environ['LOADTEST_MOCK_URL']}/v1",
            "OPENAI_API_KEY": "mock-key",
        }
        # Capture each host's output to a per-host log so a host that fails to
        # register is diagnosable (its traceback lands here) instead of silently
        # swallowed. The handle is closed in _stop_host.
        self._host_log = (self._workspace / "host.log").open("w")
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "host",
                "--server",
                self._server,
                "--non-interactive",
            ],
            env=env,
            cwd=str(self._workspace),
            stdout=self._host_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def _stop_host(self) -> None:
        """Best-effort terminate the host subprocess and its process group."""
        proc = getattr(self, "_host_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM the whole tree
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        log = getattr(self, "_host_log", None)
        if log is not None and not log.closed:
            log.close()

    def _host_log_tail(self, max_chars: int = 500) -> str:
        """Return the tail of this host's log, for a failure message."""
        try:
            text = (self._workspace / "host.log").read_text(errors="replace")
        except OSError:
            return ""
        return text[-max_chars:].strip()

    def _wait_online(self, timeout: float) -> None:
        """Poll ``GET /v1/hosts`` until this user's host reads ``online``.

        :raises RuntimeError: If the host dies, or is not online within *timeout*.
            The host log tail is included so a registration failure is diagnosable.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._host_proc.poll() is not None:
                raise RuntimeError(
                    f"host exited (code {self._host_proc.returncode}) before online. "
                    f"host.log tail: {self._host_log_tail()}"
                )
            with self.client.get(
                "/v1/hosts", headers=self._headers(), name="GET /v1/hosts", catch_response=True
            ) as resp:
                if resp.status_code == 200:
                    resp.success()
                    hosts = resp.json().get("hosts", []) if _is_json(resp) else []
                    if any(
                        h.get("host_id") == self._host_id and h.get("status") == "online"
                        for h in hosts
                    ):
                        return
                else:
                    resp.failure(f"list hosts -> {resp.status_code}")
            gevent.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(
            f"host not online within {timeout}s. host.log tail: {self._host_log_tail()}"
        )

    # ── the load: sessions × turns on this user's host ───────────────

    @task
    def drive_sessions(self) -> None:
        """Create SESSIONS_PER_USER host-bound sessions, each with T turns."""
        if not self._host_id:
            return  # host failed to come up; on_start already recorded it
        for _ in range(self._sessions_per_user):
            session_id = self._create_session()
            if session_id is None:
                continue
            for i in range(self._turns_per_session):
                if not self._drive_turn(session_id, _PROMPTS[i % len(_PROMPTS)]):
                    break  # a broken session won't recover — stop this convo

    def _create_session(self) -> str | None:
        """Create a session bound to this user's host; return its id or None."""
        body = {
            "agent_id": self._agent_id,
            "host_id": self._host_id,
            "host_type": "external",
            "workspace": str(self._workspace),
            "title": f"loadtest-{uuid.uuid4().hex[:8]}",
        }
        with self.client.post(
            "/v1/sessions",
            json=body,
            headers=self._headers(),
            name="POST /v1/sessions (create hosted)",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 201):
                resp.failure(f"create session -> {resp.status_code}")
                return None
            resp.success()
            return resp.json().get("id") if _is_json(resp) else None

    def _drive_turn(self, session_id: str, text: str) -> bool:
        """Post a message and poll the session to a terminal state.

        Timed as one ``TURN`` sample (post → idle). Returns True on completion.
        """
        start = time.monotonic()
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        }
        with self.client.post(
            f"/v1/sessions/{session_id}/events",
            json=body,
            headers=self._headers(),
            name="POST /v1/sessions/{id}/events",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 202):
                resp.failure(f"post message -> {resp.status_code}")
                self._fire("turn", start, exc=RuntimeError(f"post -> {resp.status_code}"))
                return False
            resp.success()

        ok = self._poll_idle(session_id)
        self._fire("turn", start, exc=None if ok else RuntimeError("turn did not settle"))
        return ok

    def _poll_idle(self, session_id: str) -> bool:
        """Poll the session snapshot until it settles (idle/failed) or times out."""
        deadline = time.monotonic() + _TURN_TIMEOUT_S
        seen_running = False
        while time.monotonic() < deadline:
            with self.client.get(
                f"/v1/sessions/{session_id}",
                headers=self._headers(),
                name="GET /v1/sessions/{id} (poll)",
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(f"poll -> {resp.status_code}")
                    return False
                resp.success()
                status = resp.json().get("status") if _is_json(resp) else None
            if status in ("running", "waiting"):
                seen_running = True
            elif status == "idle" and seen_running:
                return True
            elif status in _TERMINAL_STATES and status != "idle":
                return False
            gevent.sleep(_POLL_INTERVAL_S)
        return False

    # ── helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """Headers for control-plane requests (single-user local: no token)."""
        return {"Origin": INTERNAL_ORIGIN}

    def _fire(self, name: str, start: float, *, exc: Exception | None = None) -> None:
        """Report one Locust sample under a dedicated request type.

        ``host online`` / ``turn`` are multi-step spans (subprocess registration,
        post→idle) that aren't a single HTTP call, so they're reported as manual
        events; the per-request HTTP calls are recorded under their own names.
        """
        self.environment.events.request.fire(
            request_type="LOADTEST",
            name=name,
            response_time=(time.monotonic() - start) * 1000,
            response_length=0,
            exception=exc,
            context={},
        )


def _is_json(resp: object) -> bool:
    """Whether a Locust/requests response carries a JSON content-type."""
    ctype = getattr(resp, "headers", {}).get("Content-Type", "")
    return "application/json" in ctype
