"""E2E: the pre-launch Pi model picker offers a key provider endpoint's live inventory.

Journey (a real user's, end to end): a user configures a key-kind provider
(e.g. a z.ai API key) as the default for Pi, connects their machine with
``omnigent host``, opens the new-session screen in the web app, selects the
Pi agent, and opens its Model picker. The provider's endpoint serves many
models (ten here, like z.ai on both wire families), so the picker must offer
that live inventory. Today the picker renders the resolved provider's
``models.json``, which for key/gateway/local providers carries exactly one
entry — the configured default — so the user is offered a single model.

Unlike the sibling ``test_model_flows_prelaunch.py`` tests, the host and its
``model-options`` answer are REAL here: a genuine ``omnigent host`` daemon is
spawned with the key-provider config, and the SPA's picker request travels
server → host → ``pi_native_model_options`` for real. Only the UI-catalog
edges the landing screen needs (``/v1/agents``, the agent-discovery scan) are
stubbed, exactly like the sibling tests. A local HTTP endpoint stands in for
the provider's inventory API, serving the model list on ``GET …/models`` for
both wire families and recording every hit — on the buggy path it records
zero hits, proving the picker never consulted the endpoint at all.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _open_entry_config,
    _pi_native_agents_body,
    _run_in_fresh_loop,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The provider endpoint's live inventory (z.ai-shaped): several models on the
# same key, of which the omnigent config names exactly one as the default.
_ENDPOINT_MODELS: tuple[str, ...] = (
    "glm-4.7",
    "glm-4.7-flash",
    "glm-4.6",
    "glm-4.6v",
    "glm-4.5",
    "glm-4.5-air",
    "glm-4.5-x",
    "glm-4.5-airx",
    "glm-4.5-flash",
    "glm-4.5v",
)
_CONFIGURED_DEFAULT = "glm-4.7"
# Served by the endpoint but never named in the omnigent config — the picker
# can only offer it by consulting the endpoint's live listing.
_LIVE_ONLY_MODEL = "glm-4.6"


class _InventoryServer(ThreadingHTTPServer):
    """Provider-endpoint stand-in that records every request path."""

    daemon_threads = True

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.hits: list[str] = []


class _InventoryRequestHandler(BaseHTTPRequestHandler):
    """Serve the model inventory on ``GET …/models`` for both wire families.

    The body is a superset of the OpenAI-compatible and Anthropic list-models
    shapes (both wrap the entries in ``data`` with per-entry ``id``), so the
    same endpoint answers whichever family a resolver queries.
    """

    server: _InventoryServer

    def do_GET(self) -> None:
        self.server.hits.append(self.path)
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": model_id,
                            "object": "model",
                            "type": "model",
                            "display_name": model_id,
                            "created": 1735689600,
                            "created_at": "2025-01-01T00:00:00Z",
                        }
                        for model_id in _ENDPOINT_MODELS
                    ],
                    "has_more": False,
                    "first_id": _ENDPOINT_MODELS[0],
                    "last_id": _ENDPOINT_MODELS[-1],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, *args: object) -> None:
        """Keep pytest output clean."""


def _write_key_provider_config(tmp_path: Path, endpoint_base: str) -> tuple[Path, str]:
    """Seed ``config.yaml`` with a host identity and a default key provider.

    Mirrors a live z.ai key-provider setup: one ``kind: key`` provider serving
    both wire families off the same endpoint, with a single configured default
    model — while the endpoint itself serves :data:`_ENDPOINT_MODELS`.

    :param tmp_path: Per-test dir used as the daemon's ``HOME``.
    :param endpoint_base: Base URL of the inventory endpoint stand-in.
    :returns: ``(omni_dir, host_id)`` — the seeded ``.omnigent`` dir and the
        host id to poll for.
    """
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-pi-picker-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "host": {"host_id": host_id, "name": host_name},
                "providers": {
                    "zai": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": f"{endpoint_base}/api/anthropic",
                            "api_key": "sk-zai-e2e-picker-test",
                            "models": {"default": _CONFIGURED_DEFAULT},
                        },
                        "openai": {
                            "base_url": f"{endpoint_base}/api/coding/paas/v4",
                            "api_key": "sk-zai-e2e-picker-test",
                            "wire_api": "chat",
                            "models": {"default": _CONFIGURED_DEFAULT},
                        },
                    }
                },
            },
            default_flow_style=False,
            sort_keys=True,
        )
    )
    return omni_dir, host_id


def _spawn_host_daemon(
    tmp_path: Path, omni_dir: Path, live_server: str
) -> tuple[subprocess.Popen[bytes], Path]:
    """Spawn a real ``omnigent host`` daemon reading the seeded config.

    ``HOME`` points the host identity at the seeded ``config.yaml`` and
    ``OMNIGENT_CONFIG_HOME`` points the provider-config layer at the same
    file, so the daemon answers ``model-options`` from the key provider.
    Ambient runner/host env vars are stripped so a CI worker's own wiring
    can't leak into the daemon.

    :param tmp_path: Per-test dir used as the daemon's ``HOME``.
    :param omni_dir: The seeded ``.omnigent`` config dir.
    :param live_server: Server URL the daemon registers with.
    :returns: ``(proc, daemon_log)``.
    """
    daemon_log = tmp_path / "host-daemon.log"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST"))
    }
    env.update(
        {
            "HOME": str(tmp_path),
            "OMNIGENT_CONFIG_HOME": str(omni_dir),
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        }
    )
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, daemon_log


def _wait_for_host_online(base_url: str, host_id: str, timeout_s: float = 90.0) -> None:
    """Poll ``GET /v1/hosts`` until the spawned host reports online.

    :param base_url: Server base URL.
    :param host_id: The pre-seeded host id to wait for.
    :param timeout_s: Max seconds before failing the test.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout_s
    last: object = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/v1/hosts", timeout=5.0)
            if resp.status_code == 200:
                last = resp.json()
                for host in resp.json().get("hosts", []):
                    if host.get("host_id") == host_id and host.get("status") == "online":
                        return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"host {host_id!r} never came online within {timeout_s:.0f}s: {last!r}")


def test_pi_prelaunch_picker_offers_endpoint_live_inventory(
    live_server: str, tmp_path: Path
) -> None:
    """The Pi model picker offers the endpoint's live models, not just the default.

    With a key-kind provider whose endpoint serves :data:`_ENDPOINT_MODELS`,
    opening the pre-launch Pi Model picker must offer that inventory — at
    minimum a model the endpoint serves but the config never names
    (:data:`_LIVE_ONLY_MODEL`). Rendering only the configured default is the
    bug under test.
    """
    inventory = _InventoryServer(("127.0.0.1", 0), _InventoryRequestHandler)
    inventory_thread = threading.Thread(target=inventory.serve_forever, daemon=True)
    inventory_thread.start()
    endpoint_base = f"http://127.0.0.1:{inventory.server_address[1]}"

    omni_dir, host_id = _write_key_provider_config(tmp_path, endpoint_base)
    daemon, daemon_log = _spawn_host_daemon(tmp_path, omni_dir, live_server)
    try:
        try:
            _wait_for_host_online(live_server, host_id)
        except AssertionError:
            log_tail = daemon_log.read_text()[-3000:] if daemon_log.exists() else ""
            pytest.fail(f"host daemon never registered online. Daemon log tail:\n{log_tail}")
        _run_in_fresh_loop(_drive_pi_model_picker(live_server, host_id, inventory.hits))
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=5)
        inventory.shutdown()
        inventory.server_close()


async def _drive_pi_model_picker(base_url: str, host_id: str, endpoint_hits: list[str]) -> None:
    """Drive the real SPA to the Pi Model picker and read what it offers.

    :param base_url: Server base URL (real hosts + model-options wiring).
    :param host_id: The real host's id (seeds the recent-workspace chip).
    :param endpoint_hits: The inventory endpoint's recorded request paths,
        embedded in the failure message as evidence of whether the picker
        path consulted the endpoint at all.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # UI-catalog stubs only (mirrors the sibling start_session tests):
            # the landing picker needs a Pi agent to select, and leftover
            # sessions on the shared e2e server must not outrank it. Hosts and
            # model-options stay REAL — they are the surface under test.
            async def handle_agents(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=_pi_native_agents_body(),
                )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"data": []}),
                )

            await page.route("**/v1/agents", handle_agents)
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            # Seed a recent working directory for the REAL host so the
            # working-directory chip auto-fills like a returning user's.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ "{host_id}": ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Pi auto-selects (sole agent); open its config modal and the
            # Model combobox — the pre-launch Pi model picker.
            await _open_entry_config(page, "ag_pi_e2e")
            await page.get_by_test_id("new-chat-landing-config-model").click()

            options = page.locator("[data-model-id]")
            # The real host answered: the configured default is offered.
            await expect(
                page.locator(f'[data-model-id*="{_CONFIGURED_DEFAULT}"]').first
            ).to_be_visible(timeout=30_000)
            # Let the open picker settle so every offered row is rendered.
            await page.wait_for_timeout(1_500)

            offered = [
                await options.nth(i).get_attribute("data-model-id")
                for i in range(await options.count())
            ]
            assert any(_LIVE_ONLY_MODEL in (model_id or "") for model_id in offered), (
                f"pre-launch Pi model picker offers only {offered} — the configured "
                f"default from the resolved provider's models.json — instead of the "
                f"key provider endpoint's live inventory {list(_ENDPOINT_MODELS)} "
                f"(endpoint saw {len(endpoint_hits)} request(s): {endpoint_hits})"
            )
        finally:
            # Close the context before the browser so a recorded video (the
            # conftest injects ``record_video_dir`` when recording is on) is
            # fully written even when the assertion above fails.
            await page.context.close()
            await browser.close()
