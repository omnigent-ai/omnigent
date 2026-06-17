"""Shared helper functions for server integration tests."""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import click
import httpx
import yaml
from fastapi import FastAPI

from omnigent.onboarding.sandboxes import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
)
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runtime import session_stream

# Sentinel ready event so a stream collector's registration is a
# deterministic sync point (first delivered item) rather than a
# sleep-and-hope. Never validated at the SSE wire (the collector
# subscribes to the in-process pub/sub directly).
_COLLECTOR_READY = {"type": "_collector_ready"}


@dataclass
class SessionStreamCollector:
    """
    A live ``session_stream`` subscriber capturing published events.

    Used by presence tests to observe broadcasts through the same
    pub/sub path the SSE route consumes — every assertion exercises
    the real publish pipeline, not a mock.

    :param queue: Events delivered to this subscriber, in order.
    :param task: The pump task draining ``subscribe`` into ``queue``.
    """

    queue: asyncio.Queue[dict[str, Any]]
    task: asyncio.Task[None]

    async def next_event(self, timeout: float = 2.0) -> dict[str, Any]:
        """
        Await the next published event.

        :param timeout: Seconds before failing the test, e.g. ``2.0``.
        :returns: The event dict as published.
        """
        return await asyncio.wait_for(self.queue.get(), timeout)

    async def assert_no_event(self, within: float) -> None:
        """
        Assert no event is published within the window.

        :param within: Seconds to wait, e.g. ``0.2``. Chosen per test
            to comfortably cover the (shrunken) timer under test, so
            a spurious broadcast — the breakage being tested for —
            lands inside the window deterministically.
        """
        try:
            event = await asyncio.wait_for(self.queue.get(), within)
        except asyncio.TimeoutError:
            return
        raise AssertionError(f"unexpected broadcast: {event!r}")

    async def stop(self) -> None:
        """Cancel the pump task and await its teardown."""
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def start_session_stream_collector(conv_id: str) -> SessionStreamCollector:
    """
    Subscribe to a conversation's live stream and pump into a queue.

    Returns only after the subscriber slot is registered (signalled by
    the ready sentinel), so an event published right after this call
    is guaranteed to fan out to the collector.

    :param conv_id: Conversation to subscribe to, e.g. ``"conv_abc"``.
    :returns: The running collector.
    """
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    registered = asyncio.Event()

    async def _pump() -> None:
        async for event in session_stream.subscribe(conv_id, ready_event=_COLLECTOR_READY):
            if event is _COLLECTOR_READY:
                registered.set()
                continue
            queue.put_nowait(event)

    task = asyncio.create_task(_pump())
    await asyncio.wait_for(registered.wait(), 2.0)
    return SessionStreamCollector(queue=queue, task=task)


@dataclass
class HostStartInvocation:
    """
    The managed-host start command a fake launcher observed.

    Parsed from the ``omnigent host`` start command the managed-launch
    orchestration runs inside the sandbox, so tests can assert on (and
    act with) the exact identity + credential the server injected.

    :param host_id: Value of the injected ``OMNIGENT_HOST_ID``.
    :param host_name: Value of the injected ``OMNIGENT_HOST_NAME``.
    :param token: Value of the injected ``OMNIGENT_HOST_TOKEN`` — the
        raw launch token.
    :param command: The full shell command, for free-form assertions.
    """

    host_id: str
    host_name: str
    token: str
    command: str


class FakeSandboxLauncher(SandboxLauncher):
    """
    Recording :class:`SandboxLauncher` for managed-host tests.

    Provisions nothing: ``provision`` returns a fixed sandbox id,
    ``run`` records commands and answers the two the managed flow
    issues (the ``$HOME`` probe and the host start). When the start
    command arrives, its injected identity/token are parsed into a
    :class:`HostStartInvocation` and handed to ``on_host_start`` so the
    test can simulate the sandbox host registering (directly via the
    host store, or through a real tunnel connection).

    Primitives the managed flow never touches raise ``AssertionError``
    so an unintended call fails the test loudly.

    :param on_host_start: Callback invoked with the parsed
        :class:`HostStartInvocation` when the host start command runs.
        ``None`` records the invocation without side effects.
    :param fail_on_host_start: When ``True``, the host start command
        raises ``click.ClickException`` (simulates the in-sandbox
        start failing).
    :param fail_on_command: Substring that makes a matching ``run``
        command raise ``click.ClickException``, e.g. ``"git clone"``
        (simulates an in-sandbox command failing). ``None`` disables.
    :param home: ``$HOME`` the fake sandbox reports, e.g. ``"/root"``.
    :param provision_gate: When set, ``provision`` blocks until the
        event is set — a deterministic hold-the-launch-mid-provision
        point for tests of the background managed launch (``provision``
        runs on an ``asyncio.to_thread`` worker, so a
        ``threading.Event`` is the correct primitive). ``None``
        provisions immediately.
    """

    provider: ClassVar[str] = "modal"

    def __init__(
        self,
        *,
        on_host_start: Callable[[HostStartInvocation], None] | None = None,
        fail_on_host_start: bool = False,
        fail_on_command: str | None = None,
        home: str = "/root",
        provision_gate: threading.Event | None = None,
    ) -> None:
        self._on_host_start = on_host_start
        # Public + mutable: relaunch tests flip the failure mode between
        # the first launch and the relaunch under test.
        self.fail_on_host_start = fail_on_host_start
        self._fail_on_command = fail_on_command
        self._home = home
        self._provision_gate = provision_gate
        # Image reference / secret names / env names the production code
        # constructed the launcher with (captured by the
        # ctor-monkeypatch shims).
        self.image: str | None = None
        self.template: str | None = None
        self.secrets: list[str] | None = None
        self.env: list[str] | None = None
        self.base_url: str | None = None
        self.gateway_profile: str | None = None
        self.snapshot_name: str | None = None
        self.workdir: str | None = None
        self.vcpus: int | None = None
        self.memory_mb: int | None = None
        self.disk_gb: int | None = None
        self.prepared = False
        self.provisioned_names: list[str] = []
        self.commands: list[str] = []
        self.host_starts: list[HostStartInvocation] = []
        self.terminated: list[str] = []

    def prepare(self) -> None:
        """Record the preflight call (no real SDK/credential check)."""
        self.prepared = True

    def provision(self, name: str) -> str:
        """Record provisioning and return a per-generation sandbox id.

        Ids increment per call (``sb-fake-1``, ``sb-fake-2``, …) so
        relaunch tests can tell sandbox generations apart.
        """
        if self._provision_gate is not None:
            # Bounded so a test that forgets to release the gate fails
            # with a clear message instead of hanging into the pytest
            # timeout.
            assert self._provision_gate.wait(timeout=30), "test never released the provision gate"
        self.provisioned_names.append(name)
        return f"sb-fake-{len(self.provisioned_names)}"

    def attach(self, sandbox_id: str) -> None:
        """Unused by the managed flow — fail loud if reached."""
        raise AssertionError("managed launch must not attach to existing sandboxes")

    def keep_alive(self, sandbox_id: str) -> None:
        """Unused by the managed flow — fail loud if reached."""
        raise AssertionError("managed launch must not call keep_alive")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Record the command and answer the managed flow's probes."""
        self.commands.append(command)
        if self._fail_on_command is not None and self._fail_on_command in command:
            raise click.ClickException(f"simulated failure of: {command}")
        if 'printf %s "$HOME"' in command:
            return RemoteCommandResult(returncode=0, stdout=self._home, stderr="")
        if "omnigent host" in command:
            if self.fail_on_host_start:
                raise click.ClickException("simulated in-sandbox host start failure")
            invocation = _parse_host_start(command)
            self.host_starts.append(invocation)
            if self._on_host_start is not None:
                self._on_host_start(invocation)
        return RemoteCommandResult(returncode=0, stdout="", stderr="")

    def put(self, sandbox_id: str, local_path: Any, remote_path: str) -> None:
        """Unused by the managed flow — fail loud if reached."""
        raise AssertionError("managed launch must not ship files (image is pre-baked)")

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """Unused by the managed flow — fail loud if reached."""
        raise AssertionError("managed launch must not stream_exec")

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Unused by the managed flow — fail loud if reached."""
        raise AssertionError("managed launch must not exec_foreground")

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Unused by the managed flow — fail loud if reached."""
        raise AssertionError("managed launch must not install wheels (image is pre-baked)")

    def terminate(self, sandbox_id: str) -> None:
        """Record the termination."""
        self.terminated.append(sandbox_id)


def _parse_host_start(command: str) -> HostStartInvocation:
    """
    Parse the injected identity/token out of a host start command.

    The managed flow injects ``OMNIGENT_HOST_TOKEN`` / ``_HOST_ID`` /
    ``_HOST_NAME`` as inline env assignments; the values are
    shell-safe tokens (``shlex.quote`` leaves them unquoted), so a
    plain non-space match recovers them.

    :param command: The recorded shell command.
    :returns: The parsed invocation.
    :raises AssertionError: If any of the three env vars is missing —
        the production command regressed.
    """
    values: dict[str, str] = {}
    for var in ("OMNIGENT_HOST_TOKEN", "OMNIGENT_HOST_ID", "OMNIGENT_HOST_NAME"):
        match = re.search(rf"{var}=(\S+)", command)
        assert match is not None, f"host start command missing {var}: {command}"
        values[var] = match.group(1)
    return HostStartInvocation(
        host_id=values["OMNIGENT_HOST_ID"],
        host_name=values["OMNIGENT_HOST_NAME"],
        token=values["OMNIGENT_HOST_TOKEN"],
        command=command,
    )


def install_fake_modal_launcher(
    monkeypatch: Any,  # pytest.MonkeyPatch — Any avoids importing pytest in a helpers module
    fake: FakeSandboxLauncher,
) -> None:
    """
    Substitute the fake for ``ModalSandboxLauncher`` at its public seam.

    The managed flow constructs ``ModalSandboxLauncher(image=…)`` (and
    the terminate path constructs it bare); the shim records the image
    on the fake and hands the fake back, so production code runs
    unmodified against it.

    :param monkeypatch: The test's ``pytest.MonkeyPatch``.
    :param fake: The fake launcher to substitute.
    """
    import omnigent.onboarding.sandboxes.modal as modal_mod

    def _ctor(
        *, image: str | None = None, secrets: list[str] | None = None
    ) -> FakeSandboxLauncher:
        """Stand-in constructor recording the construction wiring."""
        fake.image = image
        fake.secrets = secrets
        return fake

    monkeypatch.setattr(modal_mod, "ModalSandboxLauncher", _ctor)


def install_fake_daytona_launcher(
    monkeypatch: Any,  # pytest.MonkeyPatch — Any avoids importing pytest in a helpers module
    fake: FakeSandboxLauncher,
) -> None:
    """
    Substitute the fake for ``DaytonaSandboxLauncher`` at its public seam.

    The managed flow constructs ``DaytonaSandboxLauncher(image=…,
    env=…)``; the shim records both on the fake and hands the fake
    back, so production code runs unmodified against it.

    :param monkeypatch: The test's ``pytest.MonkeyPatch``.
    :param fake: The fake launcher to substitute.
    """
    import omnigent.onboarding.sandboxes.daytona as daytona_mod

    def _ctor(*, image: str | None = None, env: list[str] | None = None) -> FakeSandboxLauncher:
        """Stand-in constructor recording the construction wiring."""
        fake.image = image
        fake.env = env
        return fake

    monkeypatch.setattr(daytona_mod, "DaytonaSandboxLauncher", _ctor)


def install_fake_islo_launcher(
    monkeypatch: Any,  # pytest.MonkeyPatch — Any avoids importing pytest in a helpers module
    fake: FakeSandboxLauncher,
) -> None:
    """
    Substitute the fake for ``IsloSandboxLauncher`` at its public seam.

    The managed flow constructs ``IsloSandboxLauncher(image=…, env=…,
    base_url=…, gateway_profile=…, snapshot_name=…, workdir=…,
    vcpus=…, memory_mb=…, disk_gb=…)``; the shim records those
    constructor args on the fake and hands it back, so production code
    runs unmodified against it.

    :param monkeypatch: The test's ``pytest.MonkeyPatch``.
    :param fake: The fake launcher to substitute.
    """
    import omnigent.onboarding.sandboxes.islo as islo_mod

    def _ctor(
        *,
        image: str | None = None,
        env: list[str] | None = None,
        base_url: str | None = None,
        gateway_profile: str | None = None,
        snapshot_name: str | None = None,
        workdir: str | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
    ) -> FakeSandboxLauncher:
        """Stand-in constructor recording the construction wiring."""
        fake.image = image
        fake.env = env
        fake.base_url = base_url
        fake.gateway_profile = gateway_profile
        fake.snapshot_name = snapshot_name
        fake.workdir = workdir
        fake.vcpus = vcpus
        fake.memory_mb = memory_mb
        fake.disk_gb = disk_gb
        return fake

    monkeypatch.setattr(islo_mod, "IsloSandboxLauncher", _ctor)


def install_fake_e2b_launcher(
    monkeypatch: Any,  # pytest.MonkeyPatch — Any avoids importing pytest in a helpers module
    fake: FakeSandboxLauncher,
) -> None:
    """
    Substitute the fake for ``E2BSandboxLauncher`` at its public seam.

    The managed flow constructs ``E2BSandboxLauncher(template=…, env=…)``;
    the shim records the template name and env names on the fake and
    hands the fake back, so production code runs unmodified against it.

    :param monkeypatch: The test's ``pytest.MonkeyPatch``.
    :param fake: The fake launcher to substitute.
    """
    import omnigent.onboarding.sandboxes.e2b as e2b_mod

    def _ctor(*, template: str | None = None, env: list[str] | None = None) -> FakeSandboxLauncher:
        """Stand-in constructor recording the construction wiring."""
        fake.template = template
        fake.env = env
        # Report the e2b provider so managed-host teardown's provider match
        # (launcher.provider vs host.sandbox_provider) exercises the real path
        # instead of the FakeSandboxLauncher default ("modal").
        fake.provider = "e2b"  # type: ignore[misc]  # shadow the ClassVar per-instance
        return fake

    monkeypatch.setattr(e2b_mod, "E2BSandboxLauncher", _ctor)


async def wait_for_completion(
    client: httpx.AsyncClient,
    response_id: str,
) -> dict[str, Any]:
    """Poll ``/v1/responses/{response_id}`` until terminal.

    ~60s cap; sized for CI cold-start under managed Python + DBOS init.
    """
    for _ in range(600):
        resp = await client.get(f"/v1/responses/{response_id}")
        body = resp.json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(f"Response {response_id} did not reach terminal status")


class FakeRunnerWebSocket:
    """
    Minimal WebSocket object accepted by ``TunnelRegistry.register``.

    The runner registry only stores the object in tests that do not
    actually exchange tunnel frames.
    """

    async def send_text(self, data: str) -> None:
        """
        Accept a text frame.

        :param data: Encoded tunnel frame.
        :returns: None.
        """
        del data

    async def receive_text(self) -> str:
        """
        Return an empty frame.

        :returns: Empty string.
        """
        return ""


def register_test_runner(
    app: FastAPI,
    runner_id: str,
    *,
    harnesses: list[str] | None = None,
    owner: str | None = None,
) -> None:
    """
    Register a runner in the app's live tunnel registry.

    :param app: FastAPI app built by the shared server fixture.
    :param runner_id: Runner id to register, e.g.
        ``"runner_alpha"``.
    :param harnesses: Harnesses advertised in the runner hello
        frame, e.g. ``["codex"]``. ``None`` advertises
        ``["default"]``.
    :param owner: Authenticated user who owns this runner, e.g.
        ``"alice@example.com"``. ``None`` for single-user mode.
    :returns: None.
    """
    app.state.tunnel_registry.register(
        runner_id,
        FakeRunnerWebSocket(),
        HelloFrame(
            runner_version="0.1.0-test",
            frame_protocol_version=1,
            harnesses=harnesses or ["default"],
            envs=["os_sandbox"],
        ),
        owner=owner,
    )


def build_agent_bundle(
    name: str,
    description: str | None = None,
    sub_agents: list[dict[str, Any]] | None = None,
    max_iterations: int | None = None,
    executor: dict[str, Any] | None = None,
    skills: list[dict[str, str]] | None = None,
    guardrails: dict[str, Any] | None = None,
    terminals: dict[str, Any] | None = None,
) -> bytes:
    """
    Build a minimal valid agent bundle (tar.gz) for testing.

    The bundle contains a single config.yaml with the given spec
    fields. When ``sub_agents`` is provided, each entry is added as
    ``agents/<name>/config.yaml`` and the parent's
    ``tools.agents`` list is populated.

    :param name: Agent name, e.g. ``"test-agent"``.
    :param description: Optional description.
    :param sub_agents: Optional list of sub-agent config dicts.
        Each must have at least a ``"name"`` key, e.g.
        ``[{"name": "researcher", "description": "..."}]``.
    :param max_iterations: Optional override for
        ``executor.max_iterations`` — useful for tests that want
        to force an ``incomplete`` terminal state after a known
        number of LLM turns. ``None`` uses the spec default.
    :param executor: Optional executor block to write verbatim,
        e.g. ``{"type": "omnigent", "config": {"harness":
        "codex"}}``. ``None`` uses the default in-process LLM
        executor.
    :param skills: Optional bundled skills. Each dict must include
        ``"name"``, ``"description"``, and ``"content"``, e.g.
        ``{"name": "triage", "description": "Triage issues",
        "content": "Ask one question."}``.
    :param guardrails: Optional ``guardrails:`` block written verbatim
        into the spec, e.g. ``{"policies": {"cost_guard": {"type":
        "function", "function": {"path": "...cost_budget",
        "arguments": {"max_cost_usd": 1.0}}}}}``. ``None`` omits it.
    :param terminals: Optional ``terminals:`` block written verbatim
        into the spec, e.g. ``{"shell": {"command": "bash"}}``.
        ``None`` omits it (the agent has no terminal access).
    :returns: A gzipped tar archive containing the generated
        ``config.yaml`` plus optional sub-agent and skill files.
    """
    # Any: YAML config values are heterogeneous (str, int, etc.)
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        # LLM config is required for the real workflow to execute.
        # The model value must match the agent name used by tests.
        "llm": {
            "model": name,
            # api_key is required by spec validation; the workflow
            # uses the mock LLM client so it's never actually sent.
            "connection": {"api_key": "test-key"},
        },
    }
    if description is not None:
        config["description"] = description
    if guardrails is not None:
        config["guardrails"] = guardrails
    if terminals is not None:
        config["terminals"] = terminals
    if executor is not None:
        config["executor"] = dict(executor)
        config["executor"].setdefault("config", {}).setdefault("harness", "claude-sdk")
        if max_iterations is not None:
            config["executor"]["max_iterations"] = max_iterations
    elif max_iterations is not None:
        config["executor"] = {
            "max_iterations": max_iterations,
            "config": {"harness": "claude-sdk"},
        }
    else:
        config["executor"] = {"config": {"harness": "claude-sdk"}}
    if sub_agents:
        config["tools"] = {
            "agents": [sa["name"] for sa in sub_agents],
        }
    # sort_keys=False: write the config in the order built above so
    # order-sensitive projections (e.g. the AgentObject ``terminals``
    # list, which mirrors the spec's declaration order) see the same
    # order the test declared, not an alphabetized rewrite.
    config_bytes = yaml.dump(config, sort_keys=False).encode()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config_bytes)
        tf.addfile(info, io.BytesIO(config_bytes))
        # Add sub-agent config files
        for sa in sub_agents or []:
            sa_config: dict[str, Any] = {
                "spec_version": 1,
                "name": sa["name"],
                "llm": {
                    "model": sa["name"],
                    "connection": {"api_key": "test-key"},
                },
            }
            if "description" in sa:
                sa_config["description"] = sa["description"]
            sa_bytes = yaml.dump(sa_config).encode()
            sa_info = tarfile.TarInfo(
                name=f"agents/{sa['name']}/config.yaml",
            )
            sa_info.size = len(sa_bytes)
            tf.addfile(sa_info, io.BytesIO(sa_bytes))
        for skill in skills or []:
            skill_doc = (
                "---\n"
                + yaml.dump(
                    {
                        "name": skill["name"],
                        "description": skill["description"],
                    },
                    sort_keys=False,
                )
                + "---\n\n"
                + skill["content"]
            )
            skill_bytes = skill_doc.encode()
            skill_info = tarfile.TarInfo(
                name=f"skills/{skill['name']}/SKILL.md",
            )
            skill_info.size = len(skill_bytes)
            tf.addfile(skill_info, io.BytesIO(skill_bytes))
    return buf.getvalue()


async def create_test_agent(
    client: httpx.AsyncClient,
    name: str = "test-agent",
    description: str | None = None,
    max_iterations: int | None = None,
    executor: dict[str, Any] | None = None,
    skills: list[dict[str, str]] | None = None,
    user: str | None = None,
    guardrails: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Create an agent via multipart session create and return the agent JSON.

    Uses ``POST /v1/sessions`` (multipart) to upload the bundle and
    create a session in one step, then fetches the agent metadata via
    ``GET /v1/sessions/{session_id}/agent``.

    :param client: Test HTTP client.
    :param name: Agent name to write into the bundle, e.g.
        ``"test-agent"``.
    :param description: Optional agent description.
    :param max_iterations: Optional executor iteration cap.
    :param executor: Optional executor block to write verbatim,
        e.g. ``{"type": "omnigent", "config": {"harness":
        "codex"}}``.
    :param skills: Optional bundled skills. Each dict must include
        ``"name"``, ``"description"``, and ``"content"``.
    :param user: Optional user identity for ``X-Forwarded-Email``
        header. When set, the owning session is created as this
        user so subsequent sessions referencing the agent pass
        session-scoped access checks.
    :param guardrails: Optional ``guardrails:`` block for the agent
        spec (e.g. a ``cost_budget`` policy). Passed verbatim to
        :func:`build_agent_bundle`. ``None`` omits guardrails.
    :returns: Parsed agent response body from the session agent
        endpoint, with an extra ``_session_id`` key for the owning
        session.
    """
    bundle = build_agent_bundle(
        name=name,
        description=description,
        max_iterations=max_iterations,
        executor=executor,
        skills=skills,
        guardrails=guardrails,
    )
    metadata: dict[str, Any] = {}
    headers: dict[str, str] = {}
    if user is not None:
        headers["X-Forwarded-Email"] = user
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=headers,
    )
    assert resp.status_code == 201, f"create_test_agent session create failed: {resp.text}"
    session_id = resp.json()["session_id"]
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent", headers=headers)
    assert agent_resp.status_code == 200, (
        f"create_test_agent agent lookup failed: {agent_resp.text}"
    )
    agent_data = agent_resp.json()
    # Include the owning session_id so callers can reference it
    # when creating additional sessions with this agent.
    agent_data["_session_id"] = session_id
    return agent_data


async def create_test_session(
    client: httpx.AsyncClient,
    name: str = "test-agent",
    description: str | None = None,
    title: str | None = None,
    labels: dict[str, str] | None = None,
    max_iterations: int | None = None,
    executor: dict[str, Any] | None = None,
    skills: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """
    Create a session via multipart ``POST /v1/sessions``.

    The Alpha runner-state create endpoint derives the agent from the
    uploaded bundle; request metadata carries only session fields.
    This helper returns the hydrated session snapshot so tests can
    assert on the durable agent/session linkage.

    :param client: Test HTTP client.
    :param name: Agent name to write into the uploaded bundle, e.g.
        ``"test-agent"``.
    :param description: Optional agent description.
    :param title: Optional session title, e.g. ``"debug run"``.
    :param labels: Optional initial labels, e.g.
        ``{"env": "test"}``.
    :param max_iterations: Optional executor iteration cap.
    :param executor: Optional executor block to write verbatim,
        e.g. ``{"type": "omnigent", "config": {"harness":
        "codex"}}``.
    :param skills: Optional bundled skills. Each dict must include
        ``"name"``, ``"description"``, and ``"content"``.
    :returns: Parsed ``GET /v1/sessions/{id}`` snapshot.
    """
    metadata: dict[str, Any] = {}
    if title is not None:
        metadata["title"] = title
    if labels is not None:
        metadata["labels"] = labels
    bundle = build_agent_bundle(
        name=name,
        description=description,
        max_iterations=max_iterations,
        executor=executor,
        skills=skills,
    )
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 201, f"create_test_session failed: {resp.text}"
    session_id = resp.json()["session_id"]
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, f"session snapshot failed: {snapshot.text}"
    return snapshot.json()


class CapturingRunnerClient:
    """
    Real stub for the in-process runner client used by popup-forward tests.

    Records every control event the Omnigent server POSTs to the runner's
    ``/events`` and signals when a ``cost_approval_popup`` arrives. A real
    class (not MagicMock) so an unexpected call shape fails loud rather than
    silently returning a mock. Install it as the global runner client with
    ``monkeypatch.setattr("omnigent.runtime._globals._runner_client", c)``;
    the server's forward falls back to it when no runner is bound.

    :param posted: Accumulated ``{"url", "json"}`` records of each POST.
    :param popup_seen: Set once a ``cost_approval_popup`` event is posted.
    """

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.popup_seen = asyncio.Event()

    async def post(
        self,
        url: str,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        Record a forwarded control event and ack it 202 like the runner.

        :param url: Runner path, e.g. ``"/v1/sessions/conv_x/events"``.
        :param json: The forwarded event body, e.g.
            ``{"type": "cost_approval_popup", "elicitation_id": "..."}``.
        :param timeout: Ignored; present to match the real client call.
        :returns: A 202 response so the forward's status check passes.
        """
        body = json or {}
        self.posted.append({"url": url, "json": body})
        if body.get("type") == "cost_approval_popup":
            self.popup_seen.set()
        return httpx.Response(202, request=httpx.Request("POST", f"http://runner{url}"))
