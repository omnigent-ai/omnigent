"""Enabled Claude plugin commands and skills both appear in the composer menu.

A real server and host daemon discover a plugin in an isolated home; the browser
checks both menu entries. No model turn or provider credentials are needed."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Host registration budget.
_HOST_ONLINE_TIMEOUT_S = 60.0
# Host discovery budget.
_SKILLS_TIMEOUT_S = 90.0

# Mirrors the reported plugin: knowledge-base from plugin-marketplace.
_PLUGIN = "knowledge-base"
_MARKETPLACE = "plugin-marketplace"
_PLUGIN_KEY = f"{_PLUGIN}@{_MARKETPLACE}"
# The plugin command the reporter invoked (``commands/kb-review.md``).
_COMMAND = "kb-review"
# A sibling plugin skill — the control that proves plugin discovery ran.
_SKILL = "kb-search"

# Claude Code plugin command file: ``commands/<name>.md`` with YAML
# frontmatter (description + argument hint), body is the prompt template.
_COMMAND_MD = """\
---
description: Review a knowledge-base PR
argument-hint: --pr <number>
---
Review the knowledge-base pull request given as $ARGUMENTS.
"""

_SKILL_MD = """\
---
name: kb-search
description: Search the knowledge base
---
Search the knowledge base for the user's query.
"""

# claude-sdk brain harness: keeps the slash menu enabled (native-terminal
# wrapper sessions suppress it) and maps to the "claude" skill-source family.
# No turn is ever driven, so no auth/model wiring is needed.
_AGENT_YAML = """\
name: {name}
prompt: You are a terse assistant. Answer in as few words as possible.

executor:
  harness: claude-sdk
  model: claude-sonnet-4-20250514
"""


def _seed_claude_plugin_home(home: Path) -> None:
    """Create one enabled plugin with a command and a sibling skill.

    :param home: Isolated home directory."""
    install = home / ".claude" / "plugins" / "cache" / _MARKETPLACE / _PLUGIN / "1.0.0"
    (install / ".claude-plugin").mkdir(parents=True)
    (install / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": _PLUGIN, "description": "Knowledge base tooling"})
    )
    (install / "commands").mkdir()
    (install / "commands" / f"{_COMMAND}.md").write_text(_COMMAND_MD)
    (install / "skills" / _SKILL).mkdir(parents=True)
    (install / "skills" / _SKILL / "SKILL.md").write_text(_SKILL_MD)

    (home / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {_PLUGIN_KEY: True}})
    )
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    _PLUGIN_KEY: [
                        {"scope": "user", "installPath": str(install), "version": "1.0.0"}
                    ]
                },
            }
        )
    )


def _agent_bundle(name: str) -> bytes:
    """Gzip-tar the inline claude-sdk agent YAML for multipart upload.

    Uses a non-``config.yaml`` archive name so the bundle routes through the
    omnigent compat adapter (same convention as the suite's other inline
    bundles).

    :param name: Agent name (unique per test run).
    :returns: The ``.tar.gz`` bundle bytes.
    """
    yaml_text = _AGENT_YAML.format(name=name)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(scope="module")
def plugin_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, Path]]:
    """Register an isolated host with one plugin for live host-owned discovery."""
    root = tmp_path_factory.mktemp("plugin_home_host")
    home = root / "home"
    _seed_claude_plugin_home(home)
    workspace = root / "workspace"
    workspace.mkdir()
    config_home = root / "config"
    config_home.mkdir()
    host_id = uuid.uuid4().hex
    (config_home / "config.yaml").write_text(
        json.dumps({"host": {"host_id": host_id, "name": "plugin-menu-test"}})
    )
    env = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
        if key in os.environ
    }
    env.update(
        HOME=str(home),
        PYTHONPATH=str(_REPO_ROOT),
        OMNIGENT_CONFIG_HOME=str(config_home),
        OMNIGENT_DATA_DIR=str(root / "data"),
        OPENAI_API_KEY="mock-key",
        OPENAI_BASE_URL="http://127.0.0.1:9/v1",
    )
    with (root / "host.log").open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
            while time.monotonic() < deadline:
                assert proc.poll() is None, "plugin host exited before registration"
                response = httpx.get(f"{live_server}/v1/hosts/{host_id}", timeout=2)
                if response.status_code == 200 and response.json().get("status") == "online":
                    break
                time.sleep(0.25)
            else:
                pytest.fail("plugin host did not register")
            yield host_id, workspace
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def plugin_session(
    live_server: str,
    plugin_host: tuple[str, Path],
) -> Iterator[tuple[str, str]]:
    """Create a claude-sdk session bound to the plugin host.

    :returns: ``(base_url, session_id)``.
    """
    host_id, workspace = plugin_host
    name = f"kb-plugin-{uuid.uuid4().hex[:8]}"
    bundle = _agent_bundle(name)
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    try:
        bind = httpx.post(
            f"{live_server}/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(workspace)},
            timeout=90.0,
        )
        bind.raise_for_status()
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _wait_for_plugin_skills(base_url: str, session_id: str) -> list[dict]:
    """Wait for the server to fetch the bound host’s skills.

    :param base_url: Local server URL.
    :param session_id: Session bound to the plugin host.
    :returns: Skills once the control skill appears.
    :raises AssertionError: If plugin discovery never reaches the discovery route."""
    deadline = time.monotonic() + _SKILLS_TIMEOUT_S
    last: list[dict] = []
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/skills", params={"session_id": session_id}, timeout=20.0)
        if resp.status_code == 200:
            last = resp.json().get("skills") or []
            if any(s.get("name", "").endswith(_SKILL) for s in last):
                return last
        time.sleep(1.0)
    raise AssertionError(
        f"plugin skill {_SKILL!r} never reached host discovery within "
        f"{_SKILLS_TIMEOUT_S:.0f}s (rig failure, not the bug); "
        f"last skills: {last!r}"
    )


@pytest.mark.timeout(600)
def test_plugin_commands_listed_in_slash_menu(
    page: Page,
    plugin_session: tuple[str, str],
) -> None:
    """The menu contains both the control skill and its sibling command.

    :param page: Fresh browser page.
    :param plugin_session: Server URL and session bound to the plugin host."""
    base_url, session_id = plugin_session
    _wait_for_plugin_skills(base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # Open the slash-command menu with an empty query (matches everything).
    composer.fill("/")

    # Control: the plugin's SKILL is listed (namespaced <plugin>:<skill>),
    # proving the menu is fed by the runner's plugin discovery.
    skill_row = page.get_by_test_id(f"slash-menu-item-{_PLUGIN}:{_SKILL}")
    expect(skill_row).to_be_visible(timeout=30_000)

    # The bug: the plugin's COMMAND is missing from the same menu. Accept
    # either the bare or the plugin-namespaced spelling so the assertion
    # pins discoverability, not the fix's namespace choice.
    command_row = page.locator(
        f'[data-testid="slash-menu-item-{_COMMAND}"], '
        f'[data-testid="slash-menu-item-{_PLUGIN}:{_COMMAND}"]'
    )
    expect(command_row.first).to_be_visible(timeout=10_000)
