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
def managed_replicas(tmp_path: Path, mock_llm_server_url: str) -> Iterator[ManagedReplicas]:
    """Start isolated product replicas, the real host launcher and a keyed ingress."""
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
def test_managed_first_message_rebinds_live_stream_across_replicas(
    managed_replicas: ManagedReplicas,
    mock_llm_server_url: str,
) -> None:
    """Create and send through the UI; recover both POST and SSE without navigation."""
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
