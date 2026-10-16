"""E2E: a Pi model picked from a bound host's catalog must launch the native terminal.

An operator binds ``pi-native`` to a provider with a model allowlist on a real
``omnigent host``. The New Session picker offers that host's Pi catalog as
provider-qualified ids (``omnigent/<model>``); choosing one and creating the
session must start Pi on the permitted literal model through the bound provider.

Only the host-side provider endpoint is a stand-in (the e2e mock model server);
the host daemon, the runner it spawns, the server and the SPA are real.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.async_api import Request, async_playwright, expect
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.async_thread import run_in_fresh_loop
from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests.e2e_ui.conftest import _register_agent_yaml
from tests.e2e_ui.start_session.test_start_session import _close_entry_models

_HOST_ONLINE_TIMEOUT_S = 180.0
_LAUNCH_OUTCOME_TIMEOUT_S = 120.0
_DEFAULT_MODEL = "claude-example"
_PICKED_MODEL = "claude-second"
_PICKER_VALUE = f"omnigent/{_PICKED_MODEL}"
_HOST_NAME = "bound-pi-picker-host"
# Stable identity so an environment check can address the journey's host.
_HOST_ID = hashlib.sha1(b"bound-pi-picker-host").hexdigest()[:32]
# Pauses only matter for a recorded run, where they keep each step readable.
_PACE_MS = 1_000 if os.environ.get("OMNIGENT_E2E_RECORD_DIR") else 0


def _bound_pi_config(model_url: str) -> dict[str, object]:
    return {
        "providers": {
            "repro-pi": {
                "kind": "key",
                "default": ["anthropic"],
                "anthropic": {
                    "base_url": model_url,
                    "api_key": "mock-key",
                    "models": {"default": _DEFAULT_MODEL},
                },
            }
        },
        "inference": {
            "harnesses": {
                "pi-native": {
                    "provider": "repro-pi",
                    "default_model": _DEFAULT_MODEL,
                    "model_allowlist": [_DEFAULT_MODEL, _PICKED_MODEL],
                }
            }
        },
    }


@pytest.fixture
def bound_pi_host(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real host daemon whose config binds pi-native to a two-model allowlist."""
    # The host reports pi-native as not installed without the CLI, and the
    # picker then disables the Pi row before the journey can start.
    for tool in ("pi", "tmux"):
        if shutil.which(tool) is None:
            pytest.skip(f"the {tool!r} CLI is required for a native Pi terminal e2e")
    root = tmp_path_factory.mktemp("bound_pi_host")
    workspace = root / "workspace"
    workspace.mkdir()
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    with (
        httpx.Client(base_url=live_server, trust_env=False) as client,
        isolated_host(
            root,
            live_server,
            base_env=base_env,
            host_id=_HOST_ID,
            name=_HOST_NAME,
            config=_bound_pi_config(mock_llm_server_url),
            command=[
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--non-interactive",
            ],
        ) as host,
    ):
        row = host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
        yield {**row, "workspace": str(workspace), "home": host.home, "root": root}


def _pi_agent_id(api: httpx.Client, base_url: str) -> str:
    agents = api.get("/v1/agents").json().get("data", [])
    builtin = next((a for a in agents if a.get("harness") == "pi-native"), None)
    if builtin is not None:
        return str(builtin["id"])
    agent_id = _register_agent_yaml(
        base_url,
        "spec_version: 1\n"
        f"name: pi-bound-{uuid.uuid4().hex[:6]}\n"
        "prompt: You are a test agent.\n"
        "executor:\n"
        "  config:\n"
        "    harness: pi-native\n",
    )
    assert agent_id is not None
    return agent_id


async def _reveal_pi_row(page: Any, agent_id: str) -> Any:
    """Open the agent picker and reveal the Pi row, hovering the "Other..." flyout if needed."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await picker.click()
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if not await row.is_visible():
        more = page.get_by_test_id("new-chat-landing-harness-more")
        box = await more.bounding_box()
        assert box is not None
        await page.mouse.move(box["x"] + 4, box["y"] + box["height"] / 2, steps=4)
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=6)
        await expect(more).to_have_attribute("data-state", "open")
    await expect(row).to_be_visible(timeout=10_000)
    return row


async def _open_pi_models(page: Any, agent_id: str) -> None:
    """Select the Pi agent, then open its Models menu from the picker's Edit affordance."""
    row = await _reveal_pi_row(page, agent_id)
    await row.click()
    await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
        "aria-label", re.compile(r"^Pi,")
    )
    row = await _reveal_pi_row(page, agent_id)
    await row.hover()
    await (
        page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}")
        .get_by_text("Edit", exact=True)
        .click()
    )
    await expect(page.get_by_test_id("new-chat-landing-agent-models")).to_be_visible(
        timeout=30_000
    )


def _runner_log_text(home: Path) -> str:
    logs = sorted((home / ".omnigent" / "logs" / "runner").glob("*.log"))
    return "\n".join(path.read_text(errors="replace") for path in logs)


def test_bound_pi_picker_selection_launches_the_native_terminal(
    live_server: str, bound_pi_host: dict[str, Any], tmp_path: Path
) -> None:
    """Choosing an offered Pi model on a bound host must start Pi, not fail the launch.

    :param live_server: Base URL of the e2e server.
    :param bound_pi_host: Online host row plus its workspace and HOME.
    :param tmp_path: Scratch dir for the observation screenshot.
    """
    run_in_fresh_loop(_drive(live_server, bound_pi_host, tmp_path))


async def _drive(base_url: str, host: dict[str, Any], tmp_path: Path) -> None:
    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as api:
        agent_id = _pi_agent_id(api, base_url)
        create_bodies: list[dict[str, Any]] = []
        session_id: str | None = None

        def record_create(request: Request) -> None:
            if request.method == "POST" and re.search(r"/v1/sessions(\?.*)?$", request.url):
                create_bodies.append(request.post_data_json or {})

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            # An explicit context so a recorded run finalizes its video on close.
            context = await browser.new_context()
            page = await context.new_page()
            started = time.monotonic()

            def mark(step: str) -> None:
                print(f"[t=+{time.monotonic() - started:5.1f}s] {step}", flush=True)

            try:
                page.on("request", record_create)
                await page.add_init_script(
                    "window.localStorage.setItem("
                    '"omnigent:recent-workspaces", '
                    f"JSON.stringify({json.dumps({host['host_id']: [host['workspace']]})}))"
                )
                await page.goto(f"{base_url}/")
                await page.get_by_test_id("new-chat-landing-input").wait_for(
                    state="visible", timeout=30_000
                )
                mark("landing visible")
                chip = page.get_by_test_id("new-chat-landing-host-chip")
                await expect(chip).to_have_attribute("aria-label", re.compile("Online"))

                await _open_pi_models(page, agent_id)
                picked_row = page.get_by_test_id(f"new-chat-landing-agent-model-{_PICKER_VALUE}")
                await expect(picked_row).to_be_visible(timeout=30_000)
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-model-omnigent/{_DEFAULT_MODEL}")
                ).to_be_visible()
                mark("models menu lists the host's bound Pi catalog")
                await page.wait_for_timeout(_PACE_MS)
                await picked_row.click()
                await expect(picked_row).to_have_attribute("aria-checked", "true")
                mark(f"picked {_PICKED_MODEL}")
                await page.wait_for_timeout(_PACE_MS)
                await _close_entry_models(page)

                prompt = "hello from the bound Pi picker journey"
                await page.get_by_test_id("new-chat-landing-input").fill(prompt)
                await page.wait_for_timeout(_PACE_MS)
                await page.get_by_test_id("new-chat-landing-submit").click()
                mark("submitted new session")
                await page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
                session_id = re.search(r"/c/([0-9a-f]+)", page.url).group(1)  # type: ignore[union-attr]
                mark(f"session created {session_id}")
                assert create_bodies and create_bodies[0].get("model_override") == _PICKER_VALUE, (
                    create_bodies
                )
                assert create_bodies[0].get("host_id") == host["host_id"], create_bodies

                error_pill = page.get_by_test_id("error-pill").first
                resources: list[dict[str, Any]] = []
                deadline = time.monotonic() + _LAUNCH_OUTCOME_TIMEOUT_S
                while time.monotonic() < deadline:
                    resources = (
                        api.get(f"/v1/sessions/{session_id}/resources").json().get("data", [])
                    )
                    if any(r.get("type") == "terminal" for r in resources):
                        break
                    if await error_pill.count() and await error_pill.is_visible():
                        break
                    await page.wait_for_timeout(500)
                headline = ""
                if await error_pill.count():
                    try:
                        headline = (
                            await error_pill.get_by_test_id("error-headline").inner_text(
                                timeout=5_000
                            )
                        ).strip()
                    except PlaywrightTimeoutError:
                        headline = "<error pill present but headline unreadable>"
                if headline:
                    await error_pill.click()
                    await page.wait_for_timeout(2 * _PACE_MS)
                terminals = [r for r in resources if r.get("type") == "terminal"]
                mark(f"outcome observed: terminals={len(terminals)} error={headline!r}")
                await page.screenshot(path=str(tmp_path / "observation.png"))
                items = api.get(f"/v1/sessions/{session_id}/items").json().get("data", [])
                errors = [i for i in items if i.get("type") == "error"]
                runner_log = _runner_log_text(host["home"])
                rejections = [
                    line for line in runner_log.splitlines() if "configured model list" in line
                ]
                print(
                    json.dumps(
                        {
                            "session_id": session_id,
                            "create_body_model_override": create_bodies[0].get("model_override"),
                            "error_headline": headline,
                            "error_items": errors,
                            "terminal_resources": [
                                r for r in resources if r.get("type") == "terminal"
                            ],
                            "runner_log_rejections": rejections,
                        },
                        indent=1,
                        default=str,
                    ),
                    flush=True,
                )
                await page.wait_for_timeout(_PACE_MS)
                assert any(r.get("type") == "terminal" for r in resources), (
                    f"Pi terminal did not start for picker value {_PICKER_VALUE!r}; "
                    f"session error: {headline!r}; runner log: {rejections}"
                )
                assert not errors and not rejections, (headline, errors, rejections)
            finally:
                await context.close()
                await browser.close()
                if session_id is not None:
                    with contextlib.suppress(httpx.HTTPError):
                        api.delete(f"/v1/sessions/{session_id}")
