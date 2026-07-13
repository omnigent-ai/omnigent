"""Drive native-TUI harnesses through a live server and host daemon.

Native sessions use the standard session HTTP surface, but require host/runner
provisioning and report cancellation through ``session.interrupted``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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
from tests._helpers.live_server import find_free_port
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.harness_bench.driver import (
    ForkResult,
    ProvisioningError,
    TurnResult,
    fill_snapshot_cost,
)
from tests.harness_bench.full_server import spawn_omnigent_server
from tests.harness_bench.mcp_tools import is_target_omnigent_mcp_tool
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.runtime_env import (
    BenchRuntimeEnv,
    bench_creds_skip_reason,
    resolve_bench_env,
)
from tests.harness_bench.session_items import (
    assistant_text,
    contains_user_text,
    function_calls,
    item_role,
    item_type,
)

# Timeouts are "clearly stuck" ceilings, not expected durations: provisioning is
# local (server/runner/host/forwarder boot, no model call) and a healthy native
# turn streams within seconds. A run that blows these is a cold-start on a slow
# CLI or a connection/network problem, not normal latency — so keep them tight
# enough that a broken harness fails fast, with cold-start headroom.
_HEALTH_TIMEOUT_S = 45.0
_HOST_ONLINE_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.3
_TURN_TIMEOUT_S = 60.0
_FORWARDER_READY_TIMEOUT_S = 45.0

_STREAM_PROMPT = "Count from 1 to 20 in words, one per line."
_LONG_PROMPT = "Write a detailed 500-word essay about the history of computing."

# ``response.completed`` precedes native text deltas; output_item.done is terminal.
_DELTA_EVENT = "response.output_text.delta"
_OUTPUT_DONE_EVENT = "response.output_item.done"
_IN_PROGRESS_EVENT = "response.in_progress"
_FAILED_EVENT = "response.failed"
_INTERRUPTED_EVENT = "session.interrupted"
_POLICY_DENIED_EVENT = "response.policy_denied"
_ELICITATION_EVENT = "response.elicitation_request"

_CEL_POLICY_HANDLER = "omnigent.policies.builtins.cel.cel_policy"
_NATIVE_POLICY_REASON = "bench-native-tool-policy"
_TOOL_TURN_TIMEOUT_S = 60.0
# policy_denied may arrive after output_item.done.
_DENY_OBSERVE_S = 15.0

_INTERRUPT_HOLD_S = 2.0
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


# These vendors create external_session_id only after the first message.
_LAZY_CHAT_HARNESSES: frozenset[str] = frozenset({"cursor-native"})

# Missing entries skip tool and policy probes.
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

_NATIVE_OMNIGENT_MCP_HARNESSES = frozenset(
    {
        "antigravity-native",
        "claude-native",
        "codex-native",
        "cursor-native",
        "goose-native",
        "hermes-native",
        "kiro-native",
        "qwen-native",
    }
)
_MCP_TOOL_PROMPT = (
    "You must call the omnigent MCP tool mcp__omnigent__sys_session_list exactly once. "
    "It may be displayed as sys_session_list by your client. Do not use a shell or any "
    "other tool. After the tool returns, reply with done."
)
_MCP_TOOL_RETRY_PROMPT = (
    "Call the available tool named sys_session_list now with an empty argument object. "
    "This is required; do not answer from memory and do not use any other tool."
)


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
        self._runner_id: str | None = None
        self._base_url = ""
        self._tmp = Path("/tmp") / f"omni-bench-nt-{uuid.uuid4().hex[:8]}"
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
        binary = profile.cli_binary
        if binary is not None:
            reason = cli_unavailable_reason(binary)
            if reason is not None:
                return reason
        return None

    async def __aenter__(self) -> NativeTuiDriver:
        try:
            await asyncio.to_thread(self._provision)
        except httpx.HTTPError as exc:
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

    async def run_mcp_tool_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_mcp_tool_turn)

    async def run_fork_turn(self, marker: str) -> ForkResult:
        return await asyncio.to_thread(self._drive_fork_turn, marker)

    async def run_policy_turn(self, *, action: str) -> TurnResult:
        return await asyncio.to_thread(self._drive_policy_turn, action=action)

    async def run_interrupt_turn(self) -> TurnResult:
        return await asyncio.to_thread(self._drive_interrupt_turn)

    def _provision(self) -> None:
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert self._vendor is not None
        port = find_free_port()
        self._base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex

        self._resolved_env = resolve_bench_env(self._db_profile)
        base_env = {
            **self._resolved_env.base_env,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        }
        # Omnigent-credential natives resolve their provider from global config.
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
        self._wire_native_forwarder(host_id, workspace)

    def _write_provider_config(self) -> Path:
        """Write config routing the native vendor through the resolved profile."""
        config_home = self._tmp / "omnigent-config"
        config_home.mkdir(exist_ok=True)
        profile = self._resolved_env.db_profile if self._resolved_env is not None else None
        body = f"auth:\n  type: databricks\n  profile: {profile}\n" if profile else "auth: {}\n"
        (config_home / "config.yaml").write_text(body, encoding="utf-8")
        return config_home

    def _wire_native_forwarder(self, host_id: str, workspace: Path) -> None:
        """Launch the runner and wait for the native forwarder."""
        assert self._client is not None and self._session_id is not None
        assert self._vendor is not None
        session_id = self._session_id
        self._runner_id = self._launch_and_bind_runner(host_id, workspace)
        ensure = self._client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": self._vendor.terminal_name,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=_FORWARDER_READY_TIMEOUT_S,
        )
        ensure.raise_for_status()
        with contextlib.suppress(Exception):
            self._policy_hook_disabled_reason = ensure.json().get("policy_hook_disabled_reason")
        # Waiting before the first message would deadlock lazy-chat vendors.
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
        # Keep the real HOME so the vendor login remains available.
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
        reader.join(timeout=10.0)

        result.text_delta_count = sum(1 for e in events if e == _DELTA_EVENT)
        result.text = text or ""
        if text is not None or _OUTPUT_DONE_EVENT in events:
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
            policy_id, attach_error = self._attach_tool_policy("deny")
            if attach_error is not None:
                result.error = attach_error
                return result
        else:
            policy_id = None

        events: list[str] = []
        ready = threading.Event()

        stop = threading.Event()

        def _read() -> None:
            assert self._client is not None
            # A deny reader stays open because policy_denied can arrive after completion.
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

        # Avoid satisfying the second probe from reused-session history.
        token = "deny" if deny else "allow"
        prompt = self._vendor.tool_prompt.replace("omnigent-bench-ok", f"omnigent-bench-{token}")

        reader = threading.Thread(target=_read)
        try:
            baseline = self._tool_item_count()
            reader.start()
            ready.wait(timeout=10.0)
            self._post_message(prompt)
            self._poll_new_tool_calls(baseline, result)
            if deny and not result.tool_call_denied:
                deadline = time.monotonic() + _DENY_OBSERVE_S
                while reader.is_alive() and time.monotonic() < deadline:
                    reader.join(timeout=0.5)
        finally:
            stop.set()
            if reader.is_alive():
                reader.join(timeout=10.0)
            self._delete_tool_policy(policy_id)

        result.completed = _OUTPUT_DONE_EVENT in events or bool(result.tool_calls)
        return result

    def _drive_mcp_tool_turn(self, *, timeout: float = _TOOL_TURN_TIMEOUT_S) -> TurnResult:
        """Call the read-only Omnigent MCP relay tool for this native harness."""
        assert self._vendor is not None
        result = TurnResult()
        if self._vendor.harness not in _NATIVE_OMNIGENT_MCP_HARNESSES:
            result.error = (
                f"{self._vendor.harness!r} has no Omnigent MCP bridge; "
                "its relayed tools use another native mechanism"
            )
            return result

        baseline = self._tool_item_count()
        for prompt in (_MCP_TOOL_PROMPT, _MCP_TOOL_RETRY_PROMPT):
            calls_before = len(result.tool_calls)
            self._post_message(prompt)
            self._poll_new_tool_calls(baseline, result, timeout=timeout)
            if any(is_target_omnigent_mcp_tool(call.get("name")) for call in result.tool_calls):
                result.completed = True
                break
            baseline += len(result.tool_calls) - calls_before
        return result

    def _drive_fork_turn(self, marker: str, *, timeout: float = _TURN_TIMEOUT_S) -> ForkResult:
        """Fork the native session and verify the clone replays copied history."""
        assert self._client is not None and self._session_id is not None
        assert self._runner_id is not None
        source_id = self._session_id
        result = ForkResult()
        try:
            forked = self._client.post(f"/v1/sessions/{source_id}/fork", json={})
            forked.raise_for_status()
            fork_id = str(forked.json()["id"])
            result.created = fork_id != source_id
            bound = self._client.patch(
                f"/v1/sessions/{fork_id}", json={"runner_id": self._runner_id}
            )
            bound.raise_for_status()
            copied = self._client.get(f"/v1/sessions/{fork_id}/items", params={"order": "asc"})
            copied.raise_for_status()
            result.history_copied = contains_user_text(copied.json().get("data", []), marker)

            self._session_id = fork_id
            recall = self._drive_turn(
                "Reply with exactly the literal string you were instructed to return "
                "in the first user message, and nothing else."
            )
            result.text = recall.text
            result.recalled = recall.completed and marker in recall.text
            result.failed = recall.failed
            result.error = recall.error
            result.timed_out = recall.timed_out
        except httpx.HTTPError as exc:
            result.failed = True
            result.error = repr(exc)
        return result

    def _drive_policy_turn(self, *, action: str) -> TurnResult:
        """Provoke a native tool under an explicit ALLOW or ASK policy.

        ALLOW proves that a tool proceeds while an explicit policy is attached;
        the native hook emits no positive signal that distinguishes an evaluated
        ALLOW from its default no-op behavior.
        """
        if action not in {"allow", "ask"}:
            raise ValueError(f"unsupported native policy action: {action!r}")
        assert self._client is not None and self._vendor is not None
        result = TurnResult()
        if not self._vendor.tool_name:
            result.error = f"no tool-provocation mapping for {self._vendor.harness!r}; skipped"
            return result
        if self._policy_hook_disabled_reason:
            result.error = (
                f"native policy enforcement inactive ({self._policy_hook_disabled_reason}); "
                f"cannot exercise tool-call {action.upper()}"
            )
            return result

        policy_id, attach_error = self._attach_tool_policy(action)
        if attach_error is not None:
            result.error = attach_error
            return result

        ready = threading.Event()
        stop = threading.Event()
        elicitation_id: list[str] = []

        def _read() -> None:
            assert self._client is not None
            event_type: str | None = None
            try:
                with self._client.stream(
                    "GET",
                    f"/v1/sessions/{self._session_id}/stream",
                    timeout=_TOOL_TURN_TIMEOUT_S,
                ) as resp:
                    ready.set()
                    for line in resp.iter_lines():
                        if line.startswith("event:"):
                            event_type = line[len("event:") :].strip()
                            if action == "allow" and event_type in _READER_TERMINAL:
                                return
                        if line.startswith("data:"):
                            try:
                                frame = json.loads(line[len("data:") :].strip())
                            except (TypeError, ValueError):
                                continue
                            if frame.get("type") == _ELICITATION_EVENT or (
                                event_type == _ELICITATION_EVENT
                            ):
                                value = frame.get("elicitation_id")
                                if isinstance(value, str):
                                    elicitation_id.append(value)
                                result.elicitation_requested = True
                                return
                        if stop.is_set():
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)

        reader = threading.Thread(target=_read)
        try:
            baseline = self._tool_item_count()
            reader.start()
            ready.wait(timeout=10.0)
            prompt = self._vendor.tool_prompt.replace(
                "omnigent-bench-ok", f"omnigent-bench-{action}"
            )
            self._post_message(prompt)
            deadline = time.monotonic() + _TOOL_TURN_TIMEOUT_S
            while time.monotonic() < deadline:
                if result.elicitation_requested:
                    if elicitation_id:
                        self._resolve_elicitation(elicitation_id[0])
                    break
                self._poll_new_tool_calls(
                    baseline, result, timeout=min(1.0, deadline - time.monotonic())
                )
                if result.tool_calls:
                    break
            else:
                result.timed_out = True
        finally:
            stop.set()
            reader.join(timeout=10.0)
            self._delete_tool_policy(policy_id)

        result.completed = bool(result.tool_calls)
        result.tool_call_allowed = action == "allow" and bool(result.tool_calls)
        return result

    def _attach_tool_policy(self, action: str) -> tuple[str | None, str | None]:
        """Attach a temporary CEL policy for every native tool call."""
        assert self._client is not None
        verdict = action.upper()
        expression = (
            'event.type == "tool_call" '
            f'? {{"result": "{verdict}", "reason": "{_NATIVE_POLICY_REASON}"}} '
            ': {"result": "ALLOW"}'
        )
        resp = self._client.post(
            f"/v1/sessions/{self._session_id}/policies",
            json={
                "name": f"bench_tool_{action}_{uuid.uuid4().hex[:8]}",
                "type": "python",
                "handler": _CEL_POLICY_HANDLER,
                "factory_params": {"expression": expression, "reason": _NATIVE_POLICY_REASON},
            },
            timeout=30.0,
        )
        if resp.status_code not in (200, 201):
            return None, (
                f"could not attach tool-call {action} policy (status {resp.status_code}); skipped"
            )
        policy_id = resp.json().get("id")
        return (policy_id if isinstance(policy_id, str) else None), None

    def _delete_tool_policy(self, policy_id: str | None) -> None:
        if policy_id is None:
            return
        assert self._client is not None
        with contextlib.suppress(httpx.HTTPError):
            self._client.delete(
                f"/v1/sessions/{self._session_id}/policies/{policy_id}", timeout=30.0
            )

    def _resolve_elicitation(self, elicitation_id: str) -> None:
        assert self._client is not None
        with contextlib.suppress(httpx.HTTPError):
            self._client.post(
                f"/v1/sessions/{self._session_id}/events",
                json={
                    "type": "approval",
                    "data": {"elicitation_id": elicitation_id, "action": "accept"},
                },
            )

    def _tool_item_count(self) -> int:
        """Current number of function_call items in the session (pre-turn baseline)."""
        assert self._client is not None
        resp = self._client.get(f"/v1/sessions/{self._session_id}/items", params={"order": "asc"})
        if resp.status_code != 200:
            return 0
        return sum(1 for it in resp.json().get("data", []) if item_type(it) == "function_call")

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
                calls = function_calls(resp.json().get("data", []))
                if len(calls) > baseline:
                    result.tool_calls.extend(calls[baseline:])
                    return
            if result.tool_call_denied:
                return
            time.sleep(_POLL_INTERVAL_S)

    def _assistant_item_count(self) -> int:
        """Current number of assistant items in the session (pre-turn baseline)."""
        assert self._client is not None
        resp = self._client.get(f"/v1/sessions/{self._session_id}/items", params={"order": "asc"})
        if resp.status_code != 200:
            return 0
        return sum(1 for it in resp.json().get("data", []) if item_role(it) == "assistant")

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
                    it for it in resp.json().get("data", []) if item_role(it) == "assistant"
                ]
                if len(assistants) > baseline:
                    return assistant_text(assistants[-1], separator=" ")
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
                            return
            except httpx.HTTPError as exc:
                result.error = repr(exc)

        reader = threading.Thread(target=_read)
        reader.start()
        ready.wait(timeout=10.0)
        self._post_message(_LONG_PROMPT)
        # Native deltas arrive late, so interrupt from in-progress instead.
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
