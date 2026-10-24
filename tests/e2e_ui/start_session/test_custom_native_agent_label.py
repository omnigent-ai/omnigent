"""A user-registered (``builtin: false``) agent whose ``executor.harness`` is a
native coding harness (``claude-native``) must show its OWN name in the New
Chat picker's Harnesses section -- not the harness's generic "Claude Code"
label. When it inherits the generic label it becomes visually indistinguishable
from the seeded ``claude-native-ui`` built-in, and a deployment that registers
several such agents cannot tell them apart in the list.

The rig boots a real ``omnigent server --agent <spec>`` so ``GET /v1/agents``
is the genuine server catalog (the custom row arrives ``builtin: false``,
``harness: claude-native``). Only the orthogonal host requirement is stubbed
(the picker disables with no host), matching the ``start_session`` fixtures;
no agent turn runs, so no runner is bound.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _find_free_port,
)

# Distinct name so its own label ("Autoresearch") is unmistakably different
# from the harness's generic "Claude Code".
_CUSTOM_AGENT_NAME = "autoresearch"
_CUSTOM_AGENT_YAML = f"""\
name: {_CUSTOM_AGENT_NAME}
description: Custom research agent wrapping Claude Code.
prompt: You are {_CUSTOM_AGENT_NAME}, a research agent.

executor:
  harness: claude-native
  context_window: 200000
"""

_SEEDED_NATIVE_LABEL = "Claude Code"

_HOST_ID = "host_e2e"
_HOSTS_BODY = json.dumps(
    {
        "hosts": [
            {
                "host_id": _HOST_ID,
                "name": "e2e-host",
                "owner": "e2e",
                "status": "online",
                "configured_harnesses": {"claude-native": True},
            }
        ]
    }
)


@pytest.fixture(scope="module")
def custom_native_agent_server(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn ``omnigent server --agent autoresearch.yaml``; yield its base URL."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_custom_native_agent")
    agent_path = server_tmp / f"{_CUSTOM_AGENT_NAME}.yaml"
    agent_path.write_text(_CUSTOM_AGENT_YAML)
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = server_tmp / "server.log"
    db_path = server_tmp / "test.db"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    env: dict[str, str] = {**os.environ, "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT)}
    apply_server_env(env, _REPO_ROOT)

    log_handle = open(log_path, "w")  # noqa: SIM115 -- lives for the Popen; closed in finally
    proc = subprocess.Popen(
        [
            server_executable(),
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
            "--agent",
            str(agent_path),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    ready = True
                    break
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        if not ready:
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"`omnigent server` did not become healthy within {_HEALTH_TIMEOUT_S:.0f}s "
                f"on {base_url} (last_error={last_error}).\n{log_text[-3000:]}"
            )
        yield base_url
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()


def _agent_ids(base_url: str) -> tuple[str, str]:
    """Return ``(custom_agent_id, seeded_claude_native_id)`` from the live catalog."""
    rows = httpx.get(f"{base_url}/v1/agents", timeout=10).json()["data"]
    custom = next(a for a in rows if a["name"] == _CUSTOM_AGENT_NAME)
    seeded = next(a for a in rows if a["name"] == "claude-native-ui")
    assert custom["harness"] == "claude-native"
    assert custom["builtin"] is False
    assert seeded["builtin"] is True
    return custom["id"], seeded["id"]


def test_custom_native_agent_shows_own_name(custom_native_agent_server: str, page: Page) -> None:
    """The custom claude-native row must show its own name, not "Claude Code".

    Fails on the buggy build: ``displayNameForAgent`` resolves the harness's
    generic label first, so the ``builtin: false`` custom row renders "Claude
    Code" -- identical to the seeded ``claude-native-ui`` row and with its own
    name ("autoresearch") nowhere in the picker.
    """
    base_url = custom_native_agent_server
    custom_id, seeded_id = _agent_ids(base_url)

    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(status=200, content_type="application/json", body=_HOSTS_BODY),
    )
    page.route(
        "**/v1/hosts/*/harnesses/*/model-options",
        lambda route: route.fulfill(json={"models": []}),
    )
    page.route(
        re.compile(r"/v1/hosts/[^/]+/worktrees"),
        lambda route: route.fulfill(json={"data": []}),
    )
    page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ "{_HOST_ID}": ["/work/repo"] }})
        );"""
    )

    page.goto(f"{base_url}/")
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    expect(trigger).to_be_visible(timeout=30_000)
    expect(trigger).to_be_enabled(timeout=60_000)
    trigger.click()

    custom_row = page.get_by_test_id(f"new-chat-landing-agent-{custom_id}")
    seeded_row = page.get_by_test_id(f"new-chat-landing-agent-{seeded_id}")
    expect(custom_row).to_be_visible(timeout=30_000)
    expect(seeded_row).to_be_visible()

    # The seeded native built-in legitimately reads "Claude Code".
    expect(seeded_row).to_contain_text(_SEEDED_NATIVE_LABEL)

    # The bug: the custom row inherits the generic harness label instead of
    # its own name, so it is indistinguishable from the seeded row above.
    expect(custom_row).to_contain_text(re.compile(_CUSTOM_AGENT_NAME, re.IGNORECASE))

    # Corroborating check: exactly one picker row reads exactly "Claude Code"
    # (the seeded built-in) -- two means the custom row is masquerading as it.
    expect(page.get_by_text(_SEEDED_NATIVE_LABEL, exact=True)).to_have_count(1)
