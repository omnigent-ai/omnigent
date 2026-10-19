"""Browser regression for a managed launch whose host connects to another replica.

No browser/API/SSE routes are mocked. Two real server processes share a database
but not pub-sub; a local sandbox launcher starts the real host and runner on B.
The initial POST and live stream reach A before that host exists. Only cloud
allocation and model output are replaced with deterministic local equivalents.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from omnigent.native.native_coding_agents import CLAUDE_NATIVE_AGENT_NAME, CODEX_NATIVE_AGENT_NAME
from tests.e2e_ui.conftest import _find_free_port, configure_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.start_session._managed_replica_server import terminate
from tests.e2e_ui.start_session.test_start_session import _run_in_fresh_loop

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPT = "Confirm this first managed message arrived once."
_REPLY = "The first managed message arrived once."
_FOLLOWUP = "Confirm the live stream still works on the host replica."
_FOLLOWUP_REPLY = "The host replica delivered this second live response."


@dataclass(frozen=True)
class ManagedReplicas:
    ui: str
    ingress: str
    a: str
    b: str
    logs: Path


@pytest.fixture
def managed_replicas(
    request: pytest.FixtureRequest, tmp_path: Path, mock_llm_server_url: str
) -> Iterator[ManagedReplicas]:
    """
    Start isolated product replicas, the real host launcher and a keyed ingress.

    Indirect param ``(strip_host_id, legacy_host[, message_launch_wait_s])``:
    whether the ingress strips the create's chosen host id, as for a client that
    doesn't choose one, whether the sandbox host keys by its id regardless of
    the launch (default neither), and a shorter limit on how long a message
    waits for an in-flight launch.
    """
    strip_host_id, legacy_host, *launch_wait = getattr(request, "param", (False, False))
    ports = {role: _find_free_port() for role in ("a", "b", "ingress", "ui")}
    urls = {role: f"http://127.0.0.1:{port}" for role, port in ports.items()}
    config = tmp_path / "config"
    config.mkdir()
    (tmp_path / "routing_probe.yaml").write_text(
        "name: routing_probe\nprompt: Answer the user's question.\n"
        "executor:\n  model: gpt-4o-mini\n  harness: openai-agents\n"
    )
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        },
        "PYTHONPATH": str(_REPO_ROOT),
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_CONFIG": "",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(tmp_path / "routing_probe.yaml"),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
        "OMNIGENT_INTERNAL_WS_ORIGIN": "",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
    }
    set_fallback_mock_llm(mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}')
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for role in ("b", "a", "ingress", "ui"):
            log_path = tmp_path / f"{role}.log"
            if role == "ui":
                argv = [
                    "pnpm",
                    "exec",
                    "vite",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(ports[role]),
                    "--strictPort",
                ]
                cwd = _REPO_ROOT / "web"
                process_env = {
                    **env,
                    "OMNIGENT_URL": urls["ingress"],
                    "OMNIGENT_AUTH_TOKEN": "",
                    "VITE_DATABRICKS_WORKSPACE": "true",
                }
            else:
                argv = [
                    sys.executable,
                    "-m",
                    "tests.e2e_ui.start_session._managed_replica_server",
                    role,
                    "--port",
                    str(ports[role]),
                    "--root",
                    str(tmp_path),
                    "--replica-a",
                    urls["a"],
                    "--replica-b",
                    urls["b"],
                    *(["--strip-host-id"] if strip_host_id else []),
                    *(["--message-launch-wait-s", str(launch_wait[0])] if launch_wait else []),
                    *(["--legacy-host"] if legacy_host else []),
                ]
                cwd, process_env = _REPO_ROOT, env
            with log_path.open("w") as log:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=process_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            processes.append(process)
            path = "/" if role == "ui" else "/health"
            with httpx.Client(timeout=1, trust_env=False) as client:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()[-4000:]
                    try:
                        if client.get(urls[role] + path).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail(f"{role} did not start:\n{log_path.read_text()[-4000:]}")
        yield ManagedReplicas(urls["ui"], urls["ingress"], urls["a"], urls["b"], tmp_path)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{urls['a']}/__test/host/stop", timeout=20, trust_env=False)
        for process in reversed(processes):
            terminate(process)


@pytest.mark.timeout(300)
@pytest.mark.parametrize("managed_replicas", [(True, True)], indirect=True)
def test_managed_first_message_rebinds_live_stream_across_replicas(
    managed_replicas: ManagedReplicas,
    mock_llm_server_url: str,
) -> None:
    """
    Create and send through the UI; recover both POST and SSE without navigation.

    The create carries no host id, so the message and stream start on the
    default replica, while the host keys by its own id as an older sandbox
    does.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY, "stream": True, "block": True}],
        key="managed-first-message-routing",
        match=_PROMPT,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _FOLLOWUP_REPLY, "stream": True, "block": True}],
        key="managed-routing-followup",
        match=_FOLLOWUP,
    )
    _run_in_fresh_loop(_drive(managed_replicas, mock_llm_server_url))


async def _poll(
    client: httpx.AsyncClient, url: str, ready: Callable[[Any], bool], *, timeout: float = 30
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        response = await client.get(url)
        response.raise_for_status()
        last = response.json()
        if ready(last):
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"Condition not met for {url}: {str(last)[-3000:]}")


def _persisted_messages(snapshot: dict[str, Any], role: str, text: str) -> list[dict[str, Any]]:
    return [
        item
        for item in snapshot["items"]
        if item.get("data", {}).get("role") == role
        and text in json.dumps(item["data"].get("content", []))
    ]


async def _drive(rig: ManagedReplicas, model_url: str) -> None:
    async with (
        async_playwright() as playwright,
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
    ):
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        await page.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        network_url = f"{rig.ingress}/__test/network"
        try:
            await page.goto(rig.ui)
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible()
            agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
            agent_id = next(agent["id"] for agent in agents if agent["name"] == "routing_probe")
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
            session_id = page.url.rsplit("/", 1)[-1]
            stream_path = f"/v1/sessions/{session_id}/stream"
            events_path = f"/v1/sessions/{session_id}/events"
            session_path = f"/v1/sessions/{session_id}"

            def streams(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [row for row in rows if row["path"] == stream_path]

            def posts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    row
                    for row in rows
                    if row["path"] == events_path
                    and json.loads(row["request"])["type"] == "message"
                ]

            rows = await _poll(
                client,
                network_url,
                lambda rows: bool(posts(rows) and streams(rows) and streams(rows)[0]["body"]),
            )
            create = next(
                row for row in rows if row["path"] == "/v1/sessions" and row["method"] == "POST"
            )
            assert json.loads(create["request"])["host_type"] == "managed"
            assert posts(rows)[0]["key"] is None and posts(rows)[0]["status"] is None
            assert streams(rows)[0]["replica"] == "a" and streams(rows)[0]["status"] == 200
            assert not streams(rows)[0]["closed"]
            snapshot = (await client.get(rig.a + session_path)).json()
            assert snapshot["host_id"] is None and snapshot["runner_id"] is None
            await expect(page.get_by_test_id("runner-starting-indicator")).to_be_visible()
            # The unbound sandbox is launching; the composer must not report a missing host.
            host_chip = page.get_by_test_id("composer-host-select")
            await expect(host_chip).to_have_attribute(
                "aria-label", "Host Modal Sandbox, Provisioning sandbox"
            )
            await expect(host_chip.locator(".lucide-monitor-cloud")).to_be_visible()

            # Keep the real keyless stream healthy across an actual heartbeat;
            # timeout-based stale-stream recovery must not be what fixes this.
            await _poll(
                client,
                network_url,
                lambda rows: streams(rows)[0]["body"].count("event: session.heartbeat") >= 2,
            )
            await page.screenshot(path=str(rig.logs / "provisioning.png"))
            (await client.post(f"{rig.a}/__test/provision/release")).raise_for_status()
            rows = await _poll(
                client,
                network_url,
                lambda rows: len(posts(rows)) == 2 and posts(rows)[1]["status"] == 202,
                timeout=90,
            )
            initial, retry = posts(rows)
            assert initial["status"] == 400
            assert json.loads(initial["body"])["error"]["code"] == "wrong_replica"
            assert initial["request"] == retry["request"], "retry changed the message/stable_id"
            snapshot = (await client.get(rig.b + session_path)).json()
            host_id = snapshot["host_id"]
            assert host_id and snapshot["runner_id"]
            assert retry["key"] == host_id and retry["replica"] == "b"
            state_a, state_b = [
                (await client.get(f"{url}/__test/state", params={"host_id": host_id})).json()
                for url in (rig.a, rig.b)
            ]
            assert state_a["pid"] != state_b["pid"]
            assert state_a["host_local"] is False and state_b["host_local"] is True
            await _poll(client, f"{model_url}/gate/pending", lambda body: body["pending"])
            (await client.post(f"{model_url}/gate/release")).raise_for_status()

            # Confirm the runner actually committed the reply before attributing
            # an empty browser transcript to the off-replica stream.
            snapshot = await _poll(
                client,
                rig.b + session_path,
                lambda body: len(_persisted_messages(body, "assistant", _REPLY)) == 1,
            )
            (rig.logs / "first-response.json").write_text(json.dumps(snapshot, indent=2))
            assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
            await expect(assistant.filter(has_text=_REPLY)).to_be_visible(timeout=20_000)
            rows = (await client.get(network_url)).json()
            assert streams(rows)[0]["closed"], "the browser left its wrong-replica stream open"
            keyed = [
                row for row in streams(rows) if row["key"] == host_id and row["status"] == 200
            ]
            assert keyed, "a snapshot-only recovery does not repair live streaming"
            assert any("response.output_text.delta" in row["body"] for row in keyed)

            # A second, gated turn must arrive on the repaired live connection,
            # with no reload/navigation and no new stream opened for this send.
            stream_count = len(streams(rows))
            await page.get_by_label("Message the agent").fill(_FOLLOWUP)
            await page.get_by_role("button", name="Send", exact=True).click()
            await _poll(client, f"{model_url}/gate/pending", lambda body: body["pending"])
            (await client.post(f"{model_url}/gate/release")).raise_for_status()
            await expect(assistant.filter(has_text=_FOLLOWUP_REPLY)).to_be_visible(timeout=20_000)
            await expect(page.get_by_test_id("runner-starting-indicator")).to_have_count(0)
            await expect(host_chip).to_have_attribute(
                "aria-label", "Host Modal Sandbox, online", timeout=20_000
            )
            await expect(host_chip.locator(".lucide-monitor-cloud")).to_be_visible()
            rows = (await client.get(network_url)).json()
            assert len(streams(rows)) == stream_count
            assert len(posts(rows)) == 3
            assert posts(rows)[2]["key"] == host_id
            await expect(page).to_have_url(f"{rig.ui}/c/{session_id}")

            snapshot = (await client.get(rig.b + session_path)).json()
            for role, text in (
                ("user", _PROMPT),
                ("assistant", _REPLY),
                ("user", _FOLLOWUP),
                ("assistant", _FOLLOWUP_REPLY),
            ):
                bubble = page.locator(
                    f'[data-testid="message-bubble"][data-role="{role}"]'
                ).filter(has_text=text)
                await expect(bubble).to_have_count(1)
                persisted = _persisted_messages(snapshot, role, text)
                assert len(persisted) == 1, f"expected one persisted {role}: {persisted}"
            await page.screenshot(path=str(rig.logs / "recovered.png"))
        finally:
            rows = (await client.get(network_url)).json()
            (rig.logs / "network.json").write_text(json.dumps(rows, indent=2))
            model_requests = (await client.get(f"{model_url}/mock/requests")).json()
            (rig.logs / "model-requests.json").write_text(json.dumps(model_requests, indent=2))
            await page.screenshot(path=str(rig.logs / "final.png"))
            await page.context.tracing.stop(path=str(rig.logs / "trace.zip"))
            await client.post(f"{rig.a}/__test/provision/release")
            await client.post(f"{model_url}/gate/release")
            await browser.close()


_SHARDED_PROMPT = "Confirm this managed message went straight to the host replica."
_SHARDED_REPLY = "The managed message reached the host replica first try."


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("managed_replicas", "keyed"),
    [((False, False), True), ((True, False), False)],
    indirect=["managed_replicas"],
)
def test_managed_create_lands_on_its_hosts_replica(
    managed_replicas: ManagedReplicas,
    mock_llm_server_url: str,
    keyed: bool,
) -> None:
    """
    A create with a chosen host id keys everything by it from the start; one
    without (an older client) stays on the default replica, where the host it
    launches connects too. Either way the first message lands on the first try.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _SHARDED_REPLY, "stream": True, "block": True}],
        key="managed-sharded-routing",
        match=_SHARDED_PROMPT,
    )
    _run_in_fresh_loop(_drive_sharded(managed_replicas, mock_llm_server_url, keyed=keyed))


async def _drive_sharded(rig: ManagedReplicas, model_url: str, *, keyed: bool) -> None:
    async with (
        async_playwright() as playwright,
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
    ):
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        network_url = f"{rig.ingress}/__test/network"
        try:
            await page.goto(rig.ui)
            await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible()
            agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
            agent_id = next(agent["id"] for agent in agents if agent["name"] == "routing_probe")
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            await page.get_by_test_id("new-chat-landing-input").fill(_SHARDED_PROMPT)
            await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The page URL can still name the optimistic chat; the create's
            # response carries the server's session id.
            def created(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
                return next(
                    (
                        row
                        for row in rows
                        if row["path"] == "/v1/sessions"
                        and row["method"] == "POST"
                        and row["status"] is not None
                        and row["closed"]
                    ),
                    None,
                )

            def posts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
                create = created(rows)
                if create is None:
                    return []
                events_path = f"/v1/sessions/{json.loads(create['body'])['id']}/events"
                return [
                    row
                    for row in rows
                    if row["path"] == events_path
                    and json.loads(row["request"])["type"] == "message"
                ]

            rows = await _poll(client, network_url, lambda rows: bool(posts(rows)))
            create = created(rows)
            assert create is not None
            session_prefix = f"/v1/sessions/{json.loads(create['body'])['id']}"
            replica, replica_url = ("b", rig.b) if keyed else ("a", rig.a)
            host_id = json.loads(create["request"]).get("host_id")
            assert create["replica"] == replica and create["key"] == host_id
            assert create["status"] == 201
            labels = json.loads(create["body"])["labels"]
            assert labels.get("omnigent.sandbox.requested_host_id") == host_id
            if keyed:
                assert host_id is not None and re.fullmatch(r"[0-9a-f]{32}", host_id)
            # The message waits on the launch on the replica that will hold the
            # host's tunnel, instead of failing over from another replica.
            assert posts(rows)[0]["key"] == host_id and posts(rows)[0]["replica"] == replica

            # The create launched there, so that replica holds the provisioning gate.
            (await client.post(f"{replica_url}/__test/provision/release")).raise_for_status()
            rows = await _poll(
                client,
                network_url,
                lambda rows: posts(rows)[0]["status"] == 202,
                timeout=90,
            )
            assert len(posts(rows)) == 1, "the first message was re-sent"
            snapshot = (await client.get(replica_url + session_prefix)).json()
            assert snapshot["host_id"] and snapshot["runner_id"]
            state = (
                await client.get(
                    f"{replica_url}/__test/state", params={"host_id": snapshot["host_id"]}
                )
            ).json()
            assert state["host_local"] is True, "the host connected to another replica"
            if keyed:
                assert snapshot["host_id"] == host_id
            await _poll(client, f"{model_url}/gate/pending", lambda body: body["pending"])
            (await client.post(f"{model_url}/gate/release")).raise_for_status()
            assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
            await expect(assistant.filter(has_text=_SHARDED_REPLY)).to_be_visible(timeout=20_000)

            if keyed:
                rows = (await client.get(network_url)).json()
                session_rows = [row for row in rows if row["path"].startswith(session_prefix)]
                assert session_rows
                assert all(row["key"] == host_id for row in session_rows), session_rows
                assert not [row for row in rows if '"wrong_replica"' in row["body"]], (
                    "a request reached a replica without the host's tunnel"
                )
        finally:
            rows = (await client.get(network_url)).json()
            (rig.logs / "network.json").write_text(json.dumps(rows, indent=2))
            await page.screenshot(path=str(rig.logs / "final.png"))
            for url in (rig.a, rig.b):
                await client.post(f"{url}/__test/provision/release")
            await client.post(f"{model_url}/gate/release")
            await browser.close()


# The web's cold-boot grace for a fresh session (STARTING_GRACE_S in useSessionLiveness.ts).
_COLD_BOOT_GRACE_S = 45


@pytest.mark.timeout(300)
def test_terminal_view_stays_starting_while_sandbox_provisions(
    managed_replicas: ManagedReplicas,
) -> None:
    """
    A terminal-first session keeps its Terminal view on "Starting up…" through a
    provision that outlasts the cold-boot grace, instead of offering Resume.
    """
    _run_in_fresh_loop(_drive_terminal_startup(managed_replicas))


async def _drive_terminal_startup(rig: ManagedReplicas) -> None:
    async with (
        async_playwright() as playwright,
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
    ):
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        try:
            await page.goto(rig.ui)
            agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
            agent_id = next(a["id"] for a in agents if a["name"] == CLAUDE_NATIVE_AGENT_NAME)
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            await page.get_by_test_id("new-chat-landing-input").fill("Hey")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
            session_path = f"/v1/sessions/{page.url.rsplit('/', 1)[-1]}"
            await page.get_by_test_id("view-mode-terminal").click()
            terminal = page.locator('[data-testid="main-terminal-view"][data-visible="true"]')
            starting = terminal.get_by_test_id("terminal-starting-up")
            await expect(starting).to_be_visible()

            # The keyed create provisions on B; outlast the grace plus a liveness poll.
            created_at = (await client.get(rig.b + session_path)).json()["created_at"]
            await asyncio.sleep(max(0.0, created_at + _COLD_BOOT_GRACE_S + 15 - time.time()))
            snapshot = (await client.get(rig.b + session_path)).json()
            assert snapshot["sandbox_status"]["stage"] == "provisioning"
            assert snapshot["runner_id"] is None
            await expect(starting).to_be_visible()
            await expect(terminal.get_by_role("button", name="Resume session")).to_have_count(0)
            await page.screenshot(path=str(rig.logs / "terminal-provisioning.png"))

            # A freshly loaded tab learns about the launch from the session snapshot.
            async with page.expect_response(
                lambda response: "/health?session_ids=" in response.url
            ):
                await page.reload()
            await page.get_by_test_id("view-mode-terminal").click()
            await expect(starting).to_be_visible()
            await expect(terminal.get_by_role("button", name="Resume session")).to_have_count(0)
        finally:
            for url in (rig.a, rig.b):
                await client.post(f"{url}/__test/provision/release")
            await browser.close()


# How long the server holds a message for an in-flight launch here, shortened
# from the production four minutes so the test can outlast it quickly.
_MESSAGE_LAUNCH_WAIT_S = 20
_NATIVE_PROMPT = "Confirm this first message outlasted the sandbox launch."


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "managed_replicas", [(False, False, _MESSAGE_LAUNCH_WAIT_S)], indirect=True
)
@pytest.mark.parametrize(
    ("agent_name", "sends_once_ready"),
    [(CLAUDE_NATIVE_AGENT_NAME, False), (CODEX_NATIVE_AGENT_NAME, True)],
    ids=["claude", "codex"],
)
def test_first_native_message_outlasts_a_long_sandbox_launch(
    managed_replicas: ManagedReplicas, agent_name: str, sends_once_ready: bool
) -> None:
    """
    A Claude Code or Codex session's first message stays pending through a launch
    longer than the browser's model wait and the server's hold. Codex then sends
    it once; Claude Code also waits for its model, which needs real inference.
    """
    _run_in_fresh_loop(_drive_long_launch(managed_replicas, agent_name, sends_once_ready))


async def _drive_long_launch(
    rig: ManagedReplicas, agent_name: str, sends_once_ready: bool
) -> None:
    async with (
        async_playwright() as playwright,
        httpx.AsyncClient(timeout=10, trust_env=False) as client,
    ):
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1440, "height": 960})
        # Lets the test jump past the browser's three-minute wait for the model.
        await page.clock.install()
        network_url = f"{rig.ingress}/__test/network"
        try:
            await page.goto(rig.ui)
            agents = (await client.get(f"{rig.a}/v1/agents")).json()["data"]
            agent_id = next(a["id"] for a in agents if a["name"] == agent_name)
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
            await page.get_by_test_id("new-chat-landing-input").fill(_NATIVE_PROMPT)
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
            session_path = f"/v1/sessions/{page.url.rsplit('/', 1)[-1]}"

            def posts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [
                    row
                    for row in rows
                    if row["path"] == f"{session_path}/events"
                    and json.loads(row["request"])["type"] == "message"
                ]

            await asyncio.sleep(_MESSAGE_LAUNCH_WAIT_S + 5)
            await page.clock.fast_forward("03:05")
            # The keyed create provisions on B.
            snapshot = (await client.get(rig.b + session_path)).json()
            assert snapshot["sandbox_status"]["stage"] == "provisioning"
            user = page.locator('[data-testid="message-bubble"][data-role="user"]')
            await expect(user.filter(has_text=_NATIVE_PROMPT)).to_have_count(1)
            await expect(page.get_by_test_id("error-pill")).to_have_count(0)
            await expect(page.get_by_label("Message the agent")).to_have_value("")
            # Still in the browser, not parked on the server.
            assert not posts((await client.get(network_url)).json())
            await page.screenshot(path=str(rig.logs / "long-launch.png"))
            if not sends_once_ready:
                return

            (await client.post(f"{rig.b}/__test/provision/release")).raise_for_status()
            rows = await _poll(
                client,
                network_url,
                lambda rows: any(row["status"] == 202 for row in posts(rows)),
                timeout=120,
            )
            [sent] = posts(rows)
            assert json.loads(sent["request"])["data"]["content"] == [
                {"type": "input_text", "text": _NATIVE_PROMPT}
            ]
        finally:
            for url in (rig.a, rig.b):
                await client.post(f"{url}/__test/provision/release")
            await browser.close()
