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
import contextlib
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from omnigent.harness_capabilities import AuthModel, IntegrationMode
from omnigent.harness_plugins import harness_capabilities
from omnigent.host.daemon_launch import (
    launch_or_reuse_daemon_runner,
    wait_for_host_online,
    wait_for_runner_online,
)
from omnigent.native_terminal import bind_session_runner
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.harness_bench.driver import ProvisioningError, TurnResult, fill_snapshot_cost
from tests.harness_bench.full_server import (
    _find_free_port,
    spawn_omnigent_server,
)
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.runtime_env import (
    BenchRuntimeEnv,
    bench_creds_skip_reason,
    resolve_bench_env,
)

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
# Published by the server when a native tool-call policy DENY is enforced (see
# sessions.py::_publish_policy_denied). The positive signal the deny probe keys
# on — a native deny is decided in the hook and otherwise leaves no stream trace.
_POLICY_DENIED_EVENT = "response.policy_denied"

# The registered CEL policy handler + a bench-owned deny reason. The deny probe
# attaches a session policy that DENYs the provoked tool at the tool_call phase.
_CEL_POLICY_HANDLER = "omnigent.policies.builtins.cel.cel_policy"
_NATIVE_DENY_REASON = "bench-native-tool-deny"
# How long to wait for the tool call / deny signal on a tool turn.
_TOOL_TURN_TIMEOUT_S = 180.0
# On a deny turn, how long to keep the SSE reader open after the tool call to
# observe response.policy_denied. The PreToolUse hook publishes it when it
# evaluates, whose timing relative to the turn's output_item.done is variable
# (it can trail a second output_item.done and the session settle). The reader
# returns the instant it sees the deny, so a real deny rarely waits the full
# budget; this is the ceiling before the probe SKIPs "no deny observed".
_DENY_OBSERVE_S = 30.0

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
    :param lazy_chat: ``True`` when the vendor's ``external_session_id`` (its
        chat/thread id) is created by the FIRST message rather than at TUI
        launch (cursor writes its chat store lazily on the first message). For
        such a vendor the driver must NOT gate provisioning on
        ``external_session_id`` — that id cannot exist until a turn is posted,
        so waiting for it pre-turn deadlocks. Thread-at-launch vendors
        (claude/codex) leave this ``False`` and are gated normally.
    :param tool_name: The vendor's own shell/terminal tool, e.g. ``"Bash"`` for
        claude, ``"shell"`` for codex/kiro, ``"run_shell_command"`` for qwen.
        Used for documentation and as a non-empty gate: empty means the
        tool/policy probes cannot run for this vendor and SKIP. The deny gates
        on the tool_call phase (name-agnostic), so this need not be wire-exact.
        Not derivable from the capability model, so it is an explicit per-vendor
        fact (see :data:`_NATIVE_TOOL_PROVOCATION`).
    :param tool_prompt: A prompt that reliably makes the vendor call its shell
        tool. Empty when :attr:`tool_name` is.
    """

    harness: str
    agent_name: str
    terminal_name: str
    own_auth: bool = False
    lazy_chat: bool = False
    tool_name: str = ""
    tool_prompt: str = ""


# Vendors whose external_session_id is created by the first message, not at TUI
# launch (see NativeVendor.lazy_chat). A delivery-mechanism fact not derivable
# from the capability model, so it is an explicit set. cursor is confirmed
# (its forwarder discovers the chat store written on the first message); others
# are added only once live-verified to behave this way.
_LAZY_CHAT_HARNESSES: frozenset[str] = frozenset({"cursor-native"})

# Per-harness tool provocation for the tool/policy probes:
# ``harness -> (tool_name, prompt)``. The prompt asks the vendor to run a
# harmless ``echo`` via its own shell/terminal tool (side-effect-free in the
# ephemeral workspace); the tool call then surfaces as a ``function_call`` item
# and (with a deny attached) trips the tool_call-phase policy.
#
# ``tool_name`` is the vendor's own tool for a shell command, for documentation
# and as a non-empty gate — a harness absent here (or with an empty entry) has
# its tool/policy probes SKIP, never a false UNSUPPORTED. The deny policy gates
# on the tool_call *phase* alone (name-agnostic; see _attach_tool_deny_policy),
# so ``tool_name`` no longer has to match the vendor's raw hook name exactly.
# Names sourced from omnigent/policies/builtins/safety.py::ask_on_os_tools and
# each vendor's native module.
_SHELL_PROMPT = "Use your shell/terminal tool to run this exact command: echo omnigent-bench-ok"
_NATIVE_TOOL_PROVOCATION: dict[str, tuple[str, str]] = {
    "claude-native": (
        "Bash",
        "Use the Bash tool to run this exact command: echo omnigent-bench-ok",
    ),
    "codex-native": ("shell", _SHELL_PROMPT),
    "pi-native": ("Bash", "Use the Bash tool to run this exact command: echo omnigent-bench-ok"),
    "kiro-native": ("shell", _SHELL_PROMPT),
    "qwen-native": ("run_shell_command", _SHELL_PROMPT),
    "goose-native": ("developer__shell", _SHELL_PROMPT),
    "hermes-native": ("terminal", _SHELL_PROMPT),
    "antigravity-native": ("run_command", _SHELL_PROMPT),
    "kimi-native": ("Bash", "Use the Bash tool to run this exact command: echo omnigent-bench-ok"),
}


def native_vendor(harness: str) -> NativeVendor | None:
    """Derive the :class:`NativeVendor` for *harness* from its capabilities.

    Returns ``None`` unless the harness declares ``integration_mode ==
    NATIVE_TUI`` in :func:`omnigent.harness_plugins.harness_capabilities`
    (which already discovers community plugins via entry points), so any
    native-tui harness is drivable by name with no per-vendor table here.
    ``native-server`` harnesses (e.g. opencode-native) are a different
    transport and return ``None``.
    """
    caps = harness_capabilities().get(harness)
    if caps is None or caps.integration_mode is not IntegrationMode.NATIVE_TUI:
        return None
    # agent_name and terminal_name are convention (``<harness>-ui`` and the
    # vendor CLI name), which holds for every in-repo native. A community
    # plugin whose registered terminal/agent name diverges would need an
    # override map here, mirroring the manifest's _NATIVE_CLI_BINARY.
    tool_name, tool_prompt = _NATIVE_TOOL_PROVOCATION.get(harness, ("", ""))
    return NativeVendor(
        harness=harness,
        agent_name=f"{harness}-ui",
        terminal_name=harness.removesuffix("-native"),
        own_auth=caps.auth is not AuthModel.OMNIGENT_CREDENTIAL,
        lazy_chat=harness in _LAZY_CHAT_HARNESSES,
        tool_name=tool_name,
        tool_prompt=tool_prompt,
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

    def __init__(self, profile: BenchProfile, *, databricks_profile: str | None) -> None:
        self._profile = profile
        self._db_profile = databricks_profile
        self._resolved_env: BenchRuntimeEnv | None = None
        self._vendor = native_vendor(profile.harness)
        self._proc: subprocess.Popen[bytes] | None = None
        self._daemon: subprocess.Popen[bytes] | None = None
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        self._base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-nt-{uuid.uuid4().hex[:8]}"
        # Set from the terminal-ensure response when native policy enforcement
        # is inactive (fail-open: codex too old / hook untrusted). The deny
        # probe reads this to SKIP rather than report a false UNSUPPORTED.
        self._policy_hook_disabled_reason: str | None = None

    @staticmethod
    def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
        """Return a skip reason if this driver cannot run *profile*, else None."""
        vendor = native_vendor(profile.harness)
        if vendor is None:
            return f"{profile.harness!r} is not a native-tui harness"
        creds_skip = bench_creds_skip_reason(databricks_profile)
        if creds_skip is not None:
            return creds_skip
        # The vendor CLI must exist AND be interactively logged in on this
        # host; the bench cannot provision a login. Presence on PATH is the
        # cheapest precondition we can check — a missing login still fails the
        # live turn, reported as a capability-neutral skip by the probes.
        binary = profile.cli_binary
        if binary is not None:
            reason = cli_unavailable_reason(binary)
            if reason is not None:
                return reason
        return None

    # ── async driver protocol ────────────────────────────────

    async def __aenter__(self) -> NativeTuiDriver:
        try:
            await asyncio.to_thread(self._provision)
        except httpx.HTTPError as exc:
            # Native provisioning drives a live vendor CLI + a server-native
            # terminal, so an HTTP failure here (e.g. a 500 from terminal-ensure
            # when the vendor cannot start a thread) is an environment/server-
            # state gap, not a bench bug. Re-raise as ProvisioningError so the
            # orchestrator skips this harness quietly (reason shown in its row)
            # instead of dumping a traceback. A programming error (AssertionError
            # on a misconfigured profile, etc.) is not an HTTPError, so it still
            # propagates loud.
            raise ProvisioningError(f"native provisioning HTTP error: {exc}") from exc
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.to_thread(self._teardown)

    async def run_basic_turn(self, marker: str) -> TurnResult:
        prompt = f"Reply with exactly the literal string {marker} and nothing else."
        return await asyncio.to_thread(self._drive_turn, prompt)

    async def run_streaming_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_turn, _STREAM_PROMPT, count_deltas=True)

    async def run_tool_turn(self, *, deny: bool) -> TurnResult:
        return await asyncio.to_thread(self._drive_tool_turn, deny=deny)

    async def run_interrupt_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_interrupt_turn)

    # ── provisioning ─────────────────────────────────────────

    def _provision(self) -> None:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert self._vendor is not None
        port = _find_free_port()
        self._base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex

        # Credentials derived the way `omni run` does (ambient OPENAI_* wins,
        # else resolve_databricks_workspace); plus the native tunnel token.
        self._resolved_env = resolve_bench_env(self._db_profile)
        base_env = {
            **self._resolved_env.base_env,
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
        LLM provider through this run's Databricks profile; return its dir.

        Uses the profile resolved by :func:`resolve_bench_env` (``--profile`` or
        the config-derived one). When auth came from the ambient env (no
        profile), the vendor inherits the ambient ``OPENAI_*`` already in
        ``base_env``, so no ``auth:`` block is written."""
        config_home = self._tmp / "omnigent-config"
        config_home.mkdir(exist_ok=True)
        profile = self._resolved_env.db_profile if self._resolved_env is not None else None
        body = f"auth:\n  type: databricks\n  profile: {profile}\n" if profile else "auth: {}\n"
        (config_home / "config.yaml").write_text(body, encoding="utf-8")
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
        # The terminal-ensure success body carries a one-shot
        # ``policy_hook_disabled_reason`` when native tool-call policy
        # enforcement is inactive (fail-open: codex too old / hook untrusted).
        # Record it so the deny probe SKIPs rather than reading an unenforced
        # deny as UNSUPPORTED.
        with contextlib.suppress(Exception):
            self._policy_hook_disabled_reason = ensure.json().get("policy_hook_disabled_reason")
        # A lazy-chat vendor (cursor) does not create its external_session_id
        # until the first message lands, so it cannot be gated on here — waiting
        # pre-turn would deadlock. The terminal is ensured; the first probe turn
        # triggers the chat, and the forwarder discovers it then. Thread-at-launch
        # vendors (claude/codex) stamp external_session_id at TUI launch, so gate
        # on it to avoid racing a turn ahead of the forwarder subscription.
        if self._vendor.lazy_chat:
            return
        deadline = time.monotonic() + _FORWARDER_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            snap = self._client.get(f"/v1/sessions/{session_id}")
            if snap.status_code == 200 and snap.json().get("external_session_id"):
                return
            time.sleep(_POLL_INTERVAL_S)
        raise ProvisioningError(
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
        raise ProvisioningError(
            f"server not healthy within {_HEALTH_TIMEOUT_S}s; logs in {self._tmp}"
        )

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
        raise ProvisioningError(f"no host came online within {_HOST_ONLINE_TIMEOUT_S}s")

    def _agent_id(self, agent_name: str) -> str:
        assert self._client is not None
        resp = self._client.get("/v1/agents")
        resp.raise_for_status()
        for agent in resp.json()["data"]:
            if agent.get("name") == agent_name:
                return str(agent["id"])
        # A native agent the server did not seed (the hardcoded seeding seam):
        # an environment gap, not a bench bug, so skip this harness quietly.
        raise ProvisioningError(f"{agent_name!r} not auto-registered on the server")

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
        if result.completed:
            self._fill_cost_from_snapshot(result)
        return result

    def _fill_cost_from_snapshot(self, result: TurnResult) -> None:
        """Read cumulative usage/cost off the session snapshot after a turn.

        Native runtimes report usage via ``external_session_usage`` (published as
        ``session.usage``); the cumulative totals also land on the session
        snapshot (``total_cost_usd`` / ``last_total_tokens``), which is the same
        read point the full-server driver uses. A vendor that forwards no usage
        leaves both ``None`` and the cost probe SKIPs with that reason.
        """
        assert self._client is not None
        try:
            snap = self._client.get(f"/v1/sessions/{self._session_id}", timeout=15.0)
            if snap.status_code == 200:
                fill_snapshot_cost(result, snap.json())
        except httpx.HTTPError:
            pass  # cost is best-effort; absence -> probe SKIPs, never a false verdict

    def _drive_tool_turn(self, *, deny: bool) -> TurnResult:
        """Provoke the vendor's own tool, observe the call (and, with *deny*, the block).

        The tool call surfaces as a persisted ``function_call`` item (the vendor
        bridge mirrors ``tool_use`` into one), so ``result.tool_calls`` is filled
        by scanning the session items past a pre-turn baseline. With *deny*, a
        tool_call-phase DENY is attached to the session first (a CEL policy
        targeting the provoked tool); the block is decided in the vendor hook and
        surfaces on the stream as ``response.policy_denied`` — the positive signal
        that sets ``result.tool_call_denied``.

        Returns a capability-neutral SKIP (``error`` set, no verdict fields) when
        the vendor has no known tool to provoke, or when *deny* is requested but
        native policy enforcement is inactive (fail-open) — so the probes never
        read an environment gap as UNSUPPORTED.
        """
        assert self._client is not None and self._vendor is not None
        result = TurnResult()
        if not self._vendor.tool_name:
            result.error = f"no tool-provocation mapping for {self._vendor.harness!r}; skipped"
            return result
        if deny:
            if self._policy_hook_disabled_reason:
                result.error = (
                    f"native policy enforcement inactive ({self._policy_hook_disabled_reason}); "
                    "cannot exercise tool-call DENY"
                )
                return result
            attached = self._attach_tool_deny_policy()
            if attached is not None:
                result.error = attached  # e.g. CEL handler unavailable -> SKIP
                return result

        events: list[str] = []
        ready = threading.Event()

        stop = threading.Event()

        def _read() -> None:
            assert self._client is not None
            # response.policy_denied is published when the PreToolUse hook
            # evaluates the tool call — its timing relative to the turn's
            # output_item.done is highly variable (it can land after a SECOND
            # output_item.done and the session settle). So on a deny turn the
            # reader does NOT stop on the turn's terminal events; it reads until
            # it sees policy_denied (the signal we want) or the caller signals
            # stop() once the deny deadline elapses. On a non-deny turn it stops
            # on the terminal event as before.
            try:
                with self._client.stream(
                    "GET",
                    f"/v1/sessions/{self._session_id}/stream",
                    timeout=_TOOL_TURN_TIMEOUT_S,
                ) as resp:
                    ready.set()
                    for line in resp.iter_lines():
                        if line.startswith("event:"):
                            etype = line[len("event:") :].strip()
                            events.append(etype)
                            if etype == _POLICY_DENIED_EVENT:
                                result.tool_call_denied = True
                                return
                            if not deny and etype in _READER_TERMINAL:
                                return
                        if stop.is_set():
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)

        # Vary the command per turn so a reused session can't treat the deny
        # turn as already-satisfied by the allow turn's identical request (which
        # would leave the model with nothing to call, and nothing to deny).
        token = "deny" if deny else "allow"
        prompt = self._vendor.tool_prompt.replace("omnigent-bench-ok", f"omnigent-bench-{token}")

        baseline = self._tool_item_count()
        reader = threading.Thread(target=_read)
        reader.start()
        ready.wait(timeout=10.0)  # subscribe before posting so no event is lost
        self._post_message(prompt)
        self._poll_new_tool_calls(baseline, result)
        # On a deny turn, give response.policy_denied a generous window to land
        # (the hook round-trip can trail the tool call), then stop the reader.
        # The reader returns early the moment it sees the deny, so a real deny
        # rarely waits the full budget. On a non-deny turn the reader has already
        # stopped on the terminal event, so this join returns immediately.
        if deny and not result.tool_call_denied:
            deadline = time.monotonic() + _DENY_OBSERVE_S
            while reader.is_alive() and time.monotonic() < deadline:
                reader.join(timeout=0.5)
        stop.set()
        reader.join(timeout=10.0)

        # Turn end. On a deny turn tool_call_denied is set from the observed
        # response.policy_denied event (the server's positive signal when the
        # PreToolUse policy hook returns DENY). Note the probe's SUPPORTED means
        # "the tool call was routed through policy and a DENY verdict returned";
        # whether the vendor CLI then hard-blocks the tool is a separate axis
        # (claude-native evaluates the DENY but may still run the tool).
        result.completed = _OUTPUT_DONE_EVENT in events or bool(result.tool_calls)
        return result

    def _attach_tool_deny_policy(self) -> str | None:
        """Attach a session policy that DENYs any tool call at the tool_call phase.

        Uses the registered CEL handler via ``POST /v1/sessions/{id}/policies``.
        Denies on the *phase* alone rather than a specific tool name: the wire
        ``event.data.name`` is the vendor's raw hook ``tool_name``, which varies
        per vendor and is not always the item name the forwarder mirrors (codex
        mirrors ``shell`` but its hook may report a different tool_name). Gating
        on ``event.type == "tool_call"`` blocks whatever tool the model calls,
        which is exactly what "is a tool-call DENY enforced?" asks. The session
        is bench-owned, so denying every tool call in it is harmless.

        Returns ``None`` on success (200/201/409); a skip-reason string when the
        handler is unavailable (e.g. ``cel_expr_python`` not installed, so the
        server rejects it as unregistered) — an environment gap, not a
        capability gap.
        """
        assert self._client is not None
        # Ternary so the expression returns a map (a bare comparison returns a
        # bool, which the CEL policy treats as abstain -> ALLOW).
        expression = (
            'event.type == "tool_call" '
            f'? {{"result": "DENY", "reason": "{_NATIVE_DENY_REASON}"}} '
            ': {"result": "ALLOW"}'
        )
        resp = self._client.post(
            f"/v1/sessions/{self._session_id}/policies",
            json={
                "name": "bench_tool_deny",
                "type": "python",
                "handler": _CEL_POLICY_HANDLER,
                "factory_params": {"expression": expression, "reason": _NATIVE_DENY_REASON},
            },
            timeout=30.0,
        )
        if resp.status_code in (200, 201, 409):  # 409: already attached (reused session)
            return None
        return f"could not attach tool-call deny policy (status {resp.status_code}); skipped"

    def _tool_item_count(self) -> int:
        """Current number of function_call items in the session (pre-turn baseline)."""
        assert self._client is not None
        resp = self._client.get(f"/v1/sessions/{self._session_id}/items", params={"order": "asc"})
        if resp.status_code != 200:
            return 0
        return sum(1 for it in resp.json().get("data", []) if _item_type(it) == "function_call")

    def _poll_new_tool_calls(
        self, baseline: int, result: TurnResult, timeout: float = _TOOL_TURN_TIMEOUT_S
    ) -> None:
        """Poll session items for NEW function_call items; append them to *result*.

        Scoped past *baseline* so a reused session's prior tool calls don't
        re-count. Stops as soon as at least one new call is seen (or on a deny,
        as soon as the deny signal already landed on the stream).
        """
        assert self._client is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = self._client.get(
                f"/v1/sessions/{self._session_id}/items", params={"order": "asc"}
            )
            if resp.status_code == 200:
                calls = [
                    it for it in resp.json().get("data", []) if _item_type(it) == "function_call"
                ]
                if len(calls) > baseline:
                    for raw in calls[baseline:]:
                        data = raw.get("data", raw)
                        result.tool_calls.append(
                            {
                                "call_id": data.get("call_id"),
                                "name": data.get("name"),
                                "arguments": data.get("arguments"),
                            }
                        )
                    return
            # On a deny the tool never runs, so no new function_call item will
            # appear — stop waiting once the stream already reported the block.
            if result.tool_call_denied:
                return
            time.sleep(_POLL_INTERVAL_S)

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


def _item_type(item: dict[str, Any]) -> str | None:
    """The item's type, tolerant of a top-level or nested-``data`` shape.

    A native ``function_call`` item may carry ``type`` at the top level or under
    ``data`` depending on the items-endpoint serialization, so check both (mirrors
    full_server_driver._scan_tool_items).
    """
    data = item.get("data", item)
    return item.get("type") or (data.get("type") if isinstance(data, dict) else None)
