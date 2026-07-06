"""Native-TUI transport driver (phase-2, walking skeleton).

Drives a native-tui harness — a resident vendor CLI (``claude``, ``codex``,
``pi``, ``cursor-agent``, ...) running in a runner-owned tmux pane — through
the bench's :class:`~tests.harness_bench.transport.Driver` protocol.

The research finding this is built on: a native-tui turn rides the *same*
HTTP surface as the full server — ``POST /v1/sessions/{id}/events`` to send,
``GET /v1/sessions/{id}/stream`` for ``response.output_text.delta`` events,
``/v1/sessions/{id}/policies`` for a tool-call deny, and item polling for the
assistant reply. So ~90% of this driver is shared with
:class:`~tests.harness_bench.full_server_driver.FullServerDriver`. Three
things genuinely diverge, and they are the entire reason this is a separate
driver:

1. **Provisioning** — instead of registering an agent tarball, a native
   harness needs a **host daemon** (``omnigent host``) registered with the
   server, the auto-registered ``<harness>-native-ui`` agent, and a session
   created with ``{agent_id, host_id, workspace}``. The runner then launches
   the vendor CLI in tmux itself.
2. **Interrupt detection** — native turns do not persist an "interrupted"
   user-message marker; cancellation surfaces as a ``session.interrupted``
   event / status, so the interrupt probe keys off that.
3. **Auth + login** — the vendor CLI must be interactively logged in on the
   host. ``OMNIGENT_CREDENTIAL`` vendors (claude, codex) can take a minted
   bearer; ``OWN_AUTH`` vendors (cursor, kiro, ...) must be pre-logged-in.
   Either way the bench cannot provision a fresh login, so this transport is
   only exercisable on a host with the vendor CLI already authenticated.

Per-vendor differences beyond auth (tmux paste vs app-server RPC delivery,
readiness signal, tool-deny surface) are captured in :class:`NativeVendor`
records, so adding a harness is a config entry, not a new driver — until a
vendor diverges in kind (codex-native is RPC-delivered; opencode-native is
``native-server`` not ``native-tui``), which will want its own handling.

Scope: this driver runs **any** native-tui harness — the two shipped
(claude-native, codex-native) and any other in-repo or community-plugin native
harness — with no per-vendor table. It derives what it needs (agent name,
terminal name, whether the vendor self-authenticates) from the capability model
via :func:`native_vendor`, and provisions every native uniformly: launch/bind a
runner, ensure the native terminal, and wait for the runner-side forwarder to
come live (all natives stamp ``external_session_id`` once their terminal thread
starts) before driving turns on the shared observe path. An OMNIGENT_CREDENTIAL
native (claude, codex) is routed through the run's Databricks profile via a
written config home; an own-auth native runs only where its vendor CLI is
already logged in.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from omnigent.host.daemon_launch import (
    launch_or_reuse_daemon_runner,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native_terminal import bind_session_runner
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import lookup_databricks_host
from tests.harness_bench.driver import TurnResult
from tests.harness_bench.full_server_driver import (
    _find_free_port,
    _mint_bearer,
    spawn_omnigent_server,
)
from tests.harness_bench.profile import BenchProfile

_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 0.3
_TURN_TIMEOUT_S = 180.0
# How long to wait for the forwarder to come live (external_session_id stamped)
# after the terminal ensure returns.
_FORWARDER_READY_TIMEOUT_S = 90.0

# Native turns take longer (terminal boot + a real interactive vendor turn),
# so the prompts stay short and the timeouts generous.
_STREAM_PROMPT = "Count from 1 to 20 in words, one per line."
_LONG_PROMPT = "Write a detailed 500-word essay about the history of computing."

# Session SSE event names (confirmed live against claude-native with a
# per-event diagnostic). Critical native-tui quirk: ``response.completed``
# fires EARLY — right after the turn is accepted, seconds before the
# assistant's text deltas — so it marks the orchestration round, not the
# reply. The real end-of-output is ``response.output_item.done``, which
# arrives immediately after the final delta. Treating ``response.completed``
# as terminal makes the SSE reader exit before any delta streams, so every
# delta is lost (0 counted) and the interrupt probe never sees text to
# interrupt. The reader therefore stops on ``response.output_item.done``.
_DELTA_EVENT = "response.output_text.delta"
_OUTPUT_DONE_EVENT = "response.output_item.done"
_IN_PROGRESS_EVENT = "response.in_progress"
_FAILED_EVENT = "response.failed"
_INTERRUPTED_EVENT = "session.interrupted"

# How long to let a turn run after it reports in-progress before firing the
# interrupt, so the cancel lands mid-turn rather than racing turn setup.
_INTERRUPT_HOLD_S = 2.0
# The reader stops here: output finished, failed, or cancelled. Note the
# deliberate absence of ``response.completed`` (see above).
_READER_TERMINAL = frozenset({_OUTPUT_DONE_EVENT, _FAILED_EVENT, _INTERRUPTED_EVENT})


@dataclass(frozen=True)
class NativeVendor:
    """Per-vendor facts a native-tui harness needs beyond the shared path.

    Derived from the capability model (see :func:`native_vendor`), so a native
    harness — in-repo or a community plugin — is probeable with no bench edit.

    :param harness: The native harness id, e.g. ``"claude-native"``.
    :param agent_name: The server's auto-registered UI agent, by convention
        ``"<harness>-ui"`` (e.g. ``"claude-native-ui"``).
    :param terminal_name: The native terminal to ensure, by convention the
        vendor CLI name (``"<harness>" minus "-native"``, e.g. ``"codex"``).
    :param own_auth: ``True`` when the vendor logs in itself (auth is not
        ``OMNIGENT_CREDENTIAL``), so the bench cannot provision it — runnable
        only on a host where the vendor CLI is already logged in.
    """

    harness: str
    agent_name: str
    terminal_name: str
    own_auth: bool = False


def native_vendor(harness: str) -> NativeVendor | None:
    """Derive the :class:`NativeVendor` for *harness* from its capabilities.

    Returns ``None`` unless the harness declares ``integration_mode ==
    NATIVE_TUI`` in :func:`omnigent.harness_plugins.harness_capabilities`
    (which already discovers community plugins via entry points), so any
    native-tui harness is drivable by name with no per-vendor table here.
    ``native-server`` harnesses (e.g. opencode-native) are a different
    transport and return ``None``.
    """
    from omnigent.harness_capabilities import AuthModel, IntegrationMode
    from omnigent.harness_plugins import harness_capabilities

    caps = harness_capabilities().get(harness)
    if caps is None or caps.integration_mode is not IntegrationMode.NATIVE_TUI:
        return None
    # agent_name and terminal_name are convention (``<harness>-ui`` and the
    # vendor CLI name), which holds for every in-repo native. A community
    # plugin whose registered terminal/agent name diverges would need an
    # override map here, mirroring the manifest's _NATIVE_CLI_BINARY.
    return NativeVendor(
        harness=harness,
        agent_name=f"{harness}-ui",
        terminal_name=harness.removesuffix("-native"),
        own_auth=caps.auth is not AuthModel.OMNIGENT_CREDENTIAL,
    )


class NativeTuiDriver:
    """Drive a native-tui harness through a live server + host daemon.

    Async context manager: on enter it spawns a server, a host daemon (under
    the real ``$HOME`` so the vendor login is inherited), waits for the host
    to come online, and creates a native session bound to that host. The
    ``run_*`` methods drive turns over the same HTTP surface the full-server
    driver uses; the runner mirrors them into the tmux vendor TUI.
    """

    transport = "native-tui"

    def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
        self._profile = profile
        self._db_profile = databricks_profile
        self._vendor = native_vendor(profile.harness)
        self._proc: subprocess.Popen[bytes] | None = None
        self._daemon: subprocess.Popen[bytes] | None = None
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        self._base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-nt-{uuid.uuid4().hex[:8]}"

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else None."""
        vendor = native_vendor(profile.harness)
        if vendor is None:
            return f"{profile.harness!r} is not a native-tui harness"
        if not databricks_profile:
            return "no --profile / databricks profile provided; native-tui needs a gateway route"
        if lookup_databricks_host(databricks_profile) is None:
            return (
                f"databricks profile {databricks_profile!r} missing/hostless in ~/.databrickscfg"
            )
        # The vendor CLI must exist AND be interactively logged in on this
        # host; the bench cannot provision a login. Presence on PATH is the
        # cheapest precondition we can check — a missing login still fails the
        # live turn, reported as a capability-neutral skip by the probes.
        from tests.e2e._harness_probes import cli_unavailable_reason

        binary = profile.cli_binary
        if binary is not None:
            reason = cli_unavailable_reason(binary)
            if reason is not None:
                return reason
        return None

    # ── async driver protocol ────────────────────────────────

    async def __aenter__(self) -> NativeTuiDriver:
        await asyncio.to_thread(self._provision)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self._teardown)

    async def run_basic_turn(self, marker: str) -> TurnResult:
        prompt = f"Reply with exactly the literal string {marker} and nothing else."
        return await asyncio.to_thread(self._drive_turn, prompt)

    async def run_streaming_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_turn, _STREAM_PROMPT, count_deltas=True)

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        # Native tool calls are the vendor's own tools (Bash/Read/...), not a
        # server-dispatched builtin the bench can force, and a native deny
        # surfaces as a vendor permission decision rather than a
        # function_call_output. Wiring that observation is the next
        # increment; today the skeleton reports it unmeasured so the probe
        # records a capability-neutral skip rather than a false verdict.
        result = TurnResult()
        result.error = "native tool/policy observation not yet wired (skeleton)"
        return result

    async def run_interrupt_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_interrupt_turn)

    # ── provisioning ─────────────────────────────────────────

    def _provision(self) -> None:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert self._vendor is not None
        host = lookup_databricks_host(self._db_profile)
        assert host is not None
        port = _find_free_port()
        self._base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex

        base_env = {
            **os.environ,
            "OPENAI_API_KEY": _mint_bearer(self._db_profile),
            "OPENAI_BASE_URL": f"{host}/serving-endpoints",
            "DATABRICKS_CONFIG_PROFILE": self._db_profile,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        }
        # An omnigent-credential native resolves its provider from omnigent's
        # global config, not DATABRICKS_CONFIG_PROFILE; without it some vendors
        # (codex) hit the login screen and never start a thread. Own-auth
        # vendors use their own login and are left untouched.
        if not self._vendor.own_auth:
            base_env["OMNIGENT_CONFIG_HOME"] = str(self._write_provider_config())
        self._proc = spawn_omnigent_server(self._tmp, port, base_env, binding_token)
        self._wait_health()
        self._daemon = self._spawn_host_daemon(base_env)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        host_id = self._wait_host_online()
        agent_id = self._agent_id(self._vendor.agent_name)
        workspace = self._tmp / "workspace"
        workspace.mkdir(exist_ok=True)
        created = self._client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        created.raise_for_status()
        self._session_id = str(created.json()["id"])
        # Ensure the native terminal + wait for the forwarder for every
        # native-tui harness: it is the uniform readiness protocol (all natives
        # stamp external_session_id once their terminal thread starts).
        self._wire_native_forwarder(host_id, workspace)

    def _write_provider_config(self) -> Path:
        """Write the ``OMNIGENT_CONFIG_HOME`` config that routes the vendor's
        LLM provider through this run's Databricks profile; return its dir."""
        config_home = self._tmp / "omnigent-config"
        config_home.mkdir(exist_ok=True)
        (config_home / "config.yaml").write_text(
            f"auth:\n  type: databricks\n  profile: {self._db_profile}\n",
            encoding="utf-8",
        )
        return config_home

    def _wire_native_forwarder(self, host_id: str, workspace: Path) -> None:
        """Launch/bind a runner, ensure the native terminal, and wait for the
        forwarder to come live, so turns can drive on the shared observe path.

        Readiness is the session's ``external_session_id`` being stamped (the
        vendor thread id), which the forwarder sets once its thread starts.
        """
        assert self._client is not None and self._session_id is not None
        assert self._vendor is not None
        session_id = self._session_id
        self._launch_and_bind_runner(host_id, workspace)
        ensure = self._client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": self._vendor.terminal_name,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=90.0,
        )
        ensure.raise_for_status()
        # Gate on the forwarder wiring up: it stamps external_session_id (the
        # vendor thread id) on the session once the TUI creates its thread.
        # Posting a turn before this races ahead of the forwarder subscription.
        deadline = time.monotonic() + _FORWARDER_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{session_id}")
            if snap.status_code == 200 and snap.json().get("external_session_id"):
                return
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(
            f"native forwarder did not wire up within {_FORWARDER_READY_TIMEOUT_S}s "
            f"(no external_session_id); logs in {self._tmp}"
        )

    def _launch_and_bind_runner(self, host_id: str, workspace: Path) -> str:
        """Launch (or reuse) a daemon runner for the session and bind it.

        The daemon auto-spawns a runner, but the native terminal ensure needs
        the session explicitly bound to an online runner first (an unbound
        session 503s ``runner_unavailable``). Bridges the async daemon-launch
        helpers into this sync provisioning path.
        """
        assert self._client is not None and self._session_id is not None
        session_id = self._session_id

        async def _run() -> str:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(30.0, read=120.0),
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            ) as ac:
                await wait_for_host_online(ac, host_id, timeout_s=_HOST_ONLINE_TIMEOUT_S)
                runner_id = await launch_or_reuse_daemon_runner(
                    ac, host_id=host_id, session_id=session_id, workspace=str(workspace)
                )
                await wait_for_runner_online(ac, runner_id, timeout_s=_HOST_ONLINE_TIMEOUT_S)
                await bind_session_runner(ac, session_id, runner_id)
                return runner_id

        return asyncio.run(_run())

    def _spawn_host_daemon(self, base_env: dict[str, str]) -> subprocess.Popen[bytes]:
        # Under the real $HOME so the vendor's interactive login is inherited
        # (auth cannot be relocated for native harnesses).
        log = (self._tmp / "host-daemon.log").open("wb")
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", self._base_url],
            env=apply_runner_env(base_env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log,
        )

    def _wait_health(self) -> None:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self._base_url}/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                # Connection refused while the server boots; keep polling.
                pass
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"server not healthy within {_HEALTH_TIMEOUT_S}s; logs in {self._tmp}")

    def _wait_host_online(self) -> str:
        assert self._client is not None
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            resp = self._client.get("/v1/hosts")
            if resp.status_code == 200:
                online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
                if online:
                    return str(online[0]["host_id"])
            time.sleep(_POLL_INTERVAL_S)
        raise RuntimeError(f"no host came online within {_HOST_ONLINE_TIMEOUT_S}s")

    def _agent_id(self, agent_name: str) -> str:
        assert self._client is not None
        resp = self._client.get("/v1/agents")
        resp.raise_for_status()
        for agent in resp.json()["data"]:
            if agent.get("name") == agent_name:
                return str(agent["id"])
        raise RuntimeError(f"{agent_name!r} not auto-registered on the server")

    def _teardown(self) -> None:
        if self._client is not None:
            self._client.close()
        for proc in (self._daemon, self._proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    # ── turns ────────────────────────────────────────────────

    def _post_message(self, prompt: str) -> None:
        assert self._client is not None
        self._client.post(
            f"/v1/sessions/{self._session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            },
            timeout=30.0,
        ).raise_for_status()

    def _drive_turn(self, prompt: str, *, count_deltas: bool = False) -> TurnResult:
        """Send *prompt*; read the terminal + delta count from the stream and
        the reply text from the *new* assistant item.

        Two sources, each for what it reliably provides:

        - **Stream** (subscribe-first, background thread): the delta count,
          scoped to this turn. Subscribing before posting is required — the
          stream is not replayed. The reader stops on
          ``response.output_item.done`` (the true end-of-output), NOT on
          ``response.completed`` — on native-tui that fires seconds early
          (see ``_READER_TERMINAL``), so stopping there would count zero
          deltas.
        - **Item poll**: the assistant reply text. A short reply may arrive as
          a single ``response.output_item.done`` with no text deltas, so
          delta-accumulated text is unreliable for basic turns — the persisted
          item is authoritative. The driver reuses one session across probes,
          so the poll must ignore items that predate this turn: it records the
          assistant-item count *before* posting and waits for a NEW one.
        """
        assert self._client is not None
        result = TurnResult()
        events: list[str] = []
        ready = threading.Event()

        def _read() -> None:
            assert self._client is not None
            try:
                with self._client.stream(
                    "GET", f"/v1/sessions/{self._session_id}/stream", timeout=_TURN_TIMEOUT_S
                ) as resp:
                    ready.set()
                    for line in resp.iter_lines():
                        if line.startswith("event:"):
                            events.append(line[len("event:") :].strip())
                            if events[-1] in _READER_TERMINAL:
                                return
            except httpx.HTTPError as exc:
                result.error = repr(exc)

        baseline = self._assistant_item_count()
        reader = threading.Thread(target=_read)
        reader.start()
        ready.wait(timeout=10.0)  # subscribe before posting so no delta is lost
        self._post_message(prompt)
        text = self._poll_new_assistant_text(baseline)
        # The item landed, so the turn's output is done; the reader stops on
        # output_item.done, which coincides with the last delta.
        reader.join(timeout=10.0)

        result.text_delta_count = sum(1 for e in events if e == _DELTA_EVENT)
        result.text = text or ""
        if text is not None:
            result.completed = True
        elif _OUTPUT_DONE_EVENT in events:
            # Output finished but no new assistant item surfaced — treat as a
            # completed-but-empty turn rather than a hang.
            result.completed = True
        else:
            result.timed_out = True
        return result

    def _assistant_item_count(self) -> int:
        """Current number of assistant items in the session (pre-turn baseline)."""
        assert self._client is not None
        resp = self._client.get(f"/v1/sessions/{self._session_id}/items", params={"order": "asc"})
        if resp.status_code != 200:
            return 0
        return sum(1 for it in resp.json().get("data", []) if it.get("role") == "assistant")

    def _poll_new_assistant_text(
        self, baseline: int, timeout: float = _TURN_TIMEOUT_S
    ) -> str | None:
        """Poll until a NEW assistant item (beyond *baseline*) appears; return its text.

        Scoping to items past the pre-turn baseline is what keeps a reused
        session from returning a prior turn's stale reply.
        """
        assert self._client is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.get(
                f"/v1/sessions/{self._session_id}/items", params={"order": "asc"}
            )
            if resp.status_code == 200:
                assistants = [
                    it for it in resp.json().get("data", []) if it.get("role") == "assistant"
                ]
                if len(assistants) > baseline:
                    return _assistant_text(assistants[-1])
            time.sleep(_POLL_INTERVAL_S)
        return None

    def _drive_interrupt_turn(self) -> TurnResult:
        """Start a long turn, interrupt it mid-flight, detect the cancel.

        Native cancellation surfaces as a ``session.interrupted`` SSE event
        (no persisted "interrupted" user message, unlike full-server). The
        reader subscribes **before** posting so no event is lost; the main
        thread drives the interrupt timing.

        Why the main thread fires the interrupt (not the reader on first
        delta): on native-tui the text deltas arrive in a burst at the very
        *end* of the turn, after seconds of the vendor CLI working. Waiting
        for a delta to fire the interrupt would leave a fraction of a second
        before the turn finishes — too late to land mid-turn. The turn is
        in-flight from ``response.in_progress`` onward, so the reader signals
        that, and the main thread interrupts after a short hold while the CLI
        is still working.
        """
        assert self._client is not None
        result = TurnResult()
        ready = threading.Event()
        in_progress = threading.Event()

        def _read() -> None:
            assert self._client is not None
            try:
                with self._client.stream(
                    "GET", f"/v1/sessions/{self._session_id}/stream", timeout=_TURN_TIMEOUT_S
                ) as resp:
                    ready.set()
                    for line in resp.iter_lines():
                        if not line.startswith("event:"):
                            continue
                        etype = line[len("event:") :].strip()
                        if etype == _IN_PROGRESS_EVENT:
                            in_progress.set()
                        elif etype == _DELTA_EVENT:
                            result.text_delta_count += 1
                        elif etype == _INTERRUPTED_EVENT:
                            result.cancelled = True
                            return
                        elif etype in (_OUTPUT_DONE_EVENT, _FAILED_EVENT):
                            # Output finished (or failed) before the interrupt
                            # landed. Stop on output_item.done, not the early
                            # response.completed (see _READER_TERMINAL).
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)

        reader = threading.Thread(target=_read)
        reader.start()
        ready.wait(timeout=10.0)
        self._post_message(_LONG_PROMPT)
        # Interrupt once the turn is in flight, after a short hold so it lands
        # mid-turn (the CLI is working; deltas have not burst yet).
        if in_progress.wait(timeout=_TURN_TIMEOUT_S):
            time.sleep(_INTERRUPT_HOLD_S)
            try:
                self._client.post(
                    f"/v1/sessions/{self._session_id}/events",
                    json={"type": "interrupt"},
                    timeout=15.0,
                )
            except httpx.HTTPError as exc:
                result.error = repr(exc)
        reader.join(timeout=_TURN_TIMEOUT_S)
        return result


def _assistant_text(item: dict[str, Any]) -> str:
    """Concatenate assistant output_text blocks from a session item."""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    return " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )
