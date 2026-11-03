"""E2E: a configured, launchable ``acp:<slug>`` agent must not be flagged "needs setup".

A real server seeded from an ``acp:`` block, a real ``omnigent host`` daemon
carrying the same block, and a stdio ACP agent standing in for the ``traex``
binary, so New Chat renders the daemon's actual readiness map.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, expect

from tests._helpers.acp_agent import FAKE_ACP_AGENT_REPLY, FAKE_ACP_AGENT_SCRIPT
from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_local_server

_ACP_AGENT_NAME = "TraeX"
_ACP_SLUG = "traex"
_ACP_HARNESS = f"acp:{_ACP_SLUG}"
_HOST_ONLINE_TIMEOUT_S = 180.0
_REPLY_TIMEOUT_MS = 120_000

# isolated_host owns the host identity in config.yaml, so the acp: block is merged
# into that file by the daemon's own bootstrap before it reads any config.
_HOST_BOOTSTRAP = (
    "import json, os, sys, yaml\n"
    "from pathlib import Path\n"
    "path = Path(os.environ['OMNIGENT_CONFIG_HOME']) / 'config.yaml'\n"
    "config = yaml.safe_load(path.read_text()) or {}\n"
    "config.update(json.loads(sys.argv[1]))\n"
    "path.write_text(yaml.safe_dump(config))\n"
    "sys.argv = [sys.argv[0], *sys.argv[2:]]\n"
    "from omnigent.cli import main\n"
    "main()\n"
)


@pytest.fixture(scope="module")
def acp_host_stack(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Yield the server URL, the seeded TraeX row, the online host row and a workspace."""
    root = tmp_path_factory.mktemp("acp_slug_stack")
    agent_script = root / "fake_acp_agent.py"
    agent_script.write_text(FAKE_ACP_AGENT_SCRIPT)
    acp_block = {
        "acp": {
            "agents": [
                {
                    "name": _ACP_AGENT_NAME,
                    "command": shlex.join([sys.executable, str(agent_script)]),
                }
            ]
        }
    }
    server_root = root / "server"
    server_config = server_root / "server-home" / ".omnigent" / "config.yaml"
    server_config.parent.mkdir(parents=True)
    server_config.write_text(yaml.safe_dump(acp_block))
    workspace = root / "workspace"
    workspace.mkdir()

    host_id = uuid.uuid4().hex
    with (
        isolated_local_server(server_root) as base_url,
        httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as client,
    ):
        agents_response = client.get("/v1/agents", params={"limit": 1000})
        agents_response.raise_for_status()
        agents = agents_response.json()["data"]
        agent = next((a for a in agents if a.get("harness") == _ACP_HARNESS), None)
        assert agent is not None, f"server did not seed a {_ACP_HARNESS} picker row: {agents}"
        with isolated_host(
            root / "host",
            base_url,
            host_id=host_id,
            name=f"traex-host-{host_id[:6]}",
            command=[
                runner_executable(),
                "-c",
                _HOST_BOOTSTRAP,
                json.dumps(acp_block),
                "host",
                "--server",
                base_url,
                "--non-interactive",
            ],
        ) as host:
            row = host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
            yield {"base_url": base_url, "agent": agent, "host": row, "workspace": str(workspace)}


def _notice_text(notice: Locator) -> str | None:
    """Return the rendered readiness notice, or ``None`` when none is shown."""
    if notice.count() == 0:
        return None
    try:
        return notice.first.inner_text().strip()
    except PlaywrightError:
        return "<notice detached before it could be read>"


def _select_host(page: Page, host_id: str) -> None:
    page.get_by_test_id("new-chat-landing-host-chip").click()
    expect(page.get_by_test_id("new-chat-landing-host-menu")).to_be_visible()
    page.get_by_test_id(f"new-chat-landing-host-{host_id}").click()


def test_configured_acp_slug_agent_shows_no_setup_notice(
    request: pytest.FixtureRequest,
    acp_host_stack: dict[str, Any],
    output_path: str,
) -> None:
    """A returning user with TraeX selected sees no readiness notice, and TraeX replies.

    ``omnigent:last-agent-id`` is the key the picker persists, so seeding it
    selects TraeX the way a real return visit would.
    """
    base_url = acp_host_stack["base_url"]
    host = acp_host_stack["host"]
    agent = acp_host_stack["agent"]
    agent_label = agent.get("display_name") or agent["name"]
    shots = Path(output_path)
    page: Page = request.getfixturevalue("page")
    page.add_init_script(
        "window.localStorage.setItem("
        '"omnigent:recent-workspaces", '
        f"JSON.stringify({json.dumps({host['host_id']: [acp_host_stack['workspace']]})}));"
        "window.localStorage.setItem("
        f'"omnigent:last-agent-id", {json.dumps(agent["id"])})'
    )
    page.goto(f"{base_url}/")
    page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    _select_host(page, host["host_id"])
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
        re.compile(re.escape(agent_label), re.IGNORECASE), timeout=15_000
    )

    # The notice derives synchronously from the selected host's readiness map,
    # which the SPA already holds once the host is selectable, so it is either
    # rendered with the selected agent or not at all.
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(2_500)  # hold on the composer so the recording captures it
    page.screenshot(path=str(shots / "composer-notice.png"))
    notice_text = _notice_text(page.get_by_test_id("new-chat-landing-harness-warning"))

    page.get_by_test_id("new-chat-landing-input").fill("Say hello")
    page.get_by_test_id("new-chat-landing-submit").click()
    page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
    session_id = page.url.rsplit("/c/", 1)[-1]
    expect(page.get_by_text(FAKE_ACP_AGENT_REPLY)).to_be_visible(timeout=_REPLY_TIMEOUT_MS)
    expect(page.locator('[data-testid="error-pill"]')).to_have_count(0)
    page.screenshot(path=str(shots / "agent-reply.png"))

    with httpx.Client(base_url=base_url, trust_env=False, timeout=10.0) as client:
        response = client.get(f"/v1/hosts/{host['host_id']}")
    response.raise_for_status()
    readiness = response.json().get("configured_harnesses") or {}
    assert notice_text is None, (
        f"{_ACP_AGENT_NAME} launched on {host['name']} (session {session_id}) and replied, "
        f"yet New Chat warned {notice_text!r}; host readiness: acp={readiness.get('acp')!r}, "
        f"{_ACP_HARNESS}={readiness.get(_ACP_HARNESS)!r}"
    )
    # The daemon's map must key the slug the picker row is filtered on, with the
    # same verdict as the generic acp harness its launch gate canonicalizes to.
    assert readiness.get(_ACP_HARNESS) is True, (
        f"host readiness map lacks {_ACP_HARNESS}=True: acp={readiness.get('acp')!r}, "
        f"{_ACP_HARNESS}={readiness.get(_ACP_HARNESS)!r}"
    )
