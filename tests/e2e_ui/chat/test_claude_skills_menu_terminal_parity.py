"""Record native and bridged portable skills in the Claude composer menu.

Set ``CLAUDE_CONFIG_DIR`` to the isolated host's config directory to also
check its user skill tier. The single online host is selected automatically; set
``OMNIGENT_E2E_HOST_ID`` to select one when multiple hosts are connected.
The host-backed menu resolves skills without a model turn.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.native_session import create_native_session
from tests._helpers.session import post_session_bundle
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _focus_tui,
    _open_terminal_view,
    _wait_terminal_connected,
)

_CLAUDE_AGENT_YAML = """\
name: skills_parity
prompt: You are a friendly assistant.

executor:
  model: claude-sonnet-4-5
  harness: claude-native

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _bundle() -> bytes:
    """Gzipped tarball of the claude-family agent spec."""
    import gzip
    import io
    import tarfile

    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = _CLAUDE_AGENT_YAML.encode()
        info = tarfile.TarInfo(name="skills_parity.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _seed_skill(skills_dir: Path, name: str, description: str) -> None:
    """Write a minimal ``<skills_dir>/<name>/SKILL.md``."""
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n"
    )


def test_claude_menu_lists_only_terminal_loadable_skills(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """The ``/`` menu includes native and bridged ``.agents/skills`` tiers.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    :param tmp_path: Workspace root seeded with the two workspace tiers.
    """
    workspace = tmp_path / "workspace"
    _seed_skill(workspace / ".claude" / "skills", "claude-dir-skill", "workspace claude skill")
    _seed_skill(workspace / ".agents" / "skills", "agents-only-skill", "workspace agents skill")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    host_id = os.environ.get("OMNIGENT_E2E_HOST_ID")
    if not host_id:
        response = httpx.get(f"{live_server}/v1/hosts", timeout=10.0)
        response.raise_for_status()
        hosts = [host for host in response.json()["hosts"] if host.get("status") == "online"]
        if len(hosts) != 1:
            pytest.skip("set OMNIGENT_E2E_HOST_ID when there is not exactly one online host")
        host_id = hosts[0]["host_id"]
    if cfg:
        _seed_skill(Path(cfg) / "skills", "user-cfg-skill", "user config-dir skill")

    create = post_session_bundle(
        httpx.post,
        f"{live_server}/v1/sessions",
        _bundle(),
        metadata={"workspace": str(workspace), "host_id": host_id},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]

    page.add_init_script(
        f"localStorage.setItem({json.dumps(f'omnigent:imports-reviewed:{host_id}')}, 'true')"
    )
    page.goto(f"{live_server}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/")

    expect(page.get_by_test_id("slash-menu-item-claude-dir-skill")).to_be_visible(timeout=15_000)
    if cfg:
        expect(page.get_by_test_id("slash-menu-item-user-cfg-skill")).to_be_visible()
    expect(page.get_by_test_id("slash-menu-item-agents-only-skill")).to_be_visible()
    # Hold the corrected menu on screen so the clip ends on the outcome.
    page.wait_for_timeout(1_500)


_PLUGIN = "dev-productivity"
_MARKETPLACE = "repro-mkt"
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_TUI_MENU_TIMEOUT_S = 45.0


def _install_claude_plugin(claude_config_dir: Path, marketplace: Path) -> None:
    """Install a ``dev-productivity`` plugin with the real Claude CLI.

    The plugin ships ``review`` as a plugin command (``commands/review.md``)
    and ``simplify`` as a skill (``skills/simplify/SKILL.md``); Claude Code
    lists both as ``/dev-productivity:<name>``.
    """
    plugin = marketplace / "plugins" / _PLUGIN
    (marketplace / ".claude-plugin").mkdir(parents=True)
    (marketplace / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": _MARKETPLACE,
                "owner": {"name": "e2e"},
                "plugins": [{"name": _PLUGIN, "source": f"./plugins/{_PLUGIN}"}],
            }
        )
    )
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": _PLUGIN, "version": "1.0.0", "description": "e2e plugin"})
    )
    (plugin / "commands").mkdir()
    (plugin / "commands" / "review.md").write_text(
        "---\ndescription: Review the current diff.\n---\nReview the working-tree diff.\n"
    )
    _seed_skill(plugin / "skills", "simplify", "Simplify the selected code.")
    claude_config_dir.mkdir(parents=True)
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(claude_config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    for args in (
        ["plugin", "marketplace", "add", str(marketplace)],
        ["plugin", "install", f"{_PLUGIN}@{_MARKETPLACE}"],
    ):
        subprocess.run(
            ["claude", *args],
            check=True,
            env=env,
            cwd=marketplace,
            capture_output=True,
            timeout=120,
        )


def _host_command(base_url: str, mock_url: str) -> list[str]:
    """Route the host's Claude launches to the mock model, then start the daemon."""
    providers = {
        "e2e-claude": {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": mock_url,
                "api_key": "mock-key",
                "models": {"default": _CLAUDE_MOCK_MODEL},
            },
        }
    }
    bootstrap = (
        "import os, runpy, sys, yaml\n"
        "path = os.path.join(os.environ['OMNIGENT_CONFIG_HOME'], 'config.yaml')\n"
        "with open(path) as fh:\n"
        "    config = yaml.safe_load(fh) or {}\n"
        f"config['providers'] = {providers!r}\n"
        "with open(path, 'w') as fh:\n"
        "    yaml.safe_dump(config, fh)\n"
        f"sys.argv = ['omnigent.host._daemon_entry', '--server', {base_url!r}]\n"
        "runpy.run_module('omnigent.host._daemon_entry', run_name='__main__')\n"
    )
    return [runner_executable(), "-c", bootstrap]


def _terminal_pane(base_url: str, session_id: str) -> str:
    """Capture the session terminal's tmux pane, the TUI's own screen."""
    rows = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0).json()
    terminal = next((r for r in rows.get("data", []) if r.get("type") == "terminal"), None)
    if terminal is None:
        return ""
    meta = terminal["metadata"]
    proc = subprocess.run(
        ["tmux", "-S", meta["tmux_socket"], "capture-pane", "-t", meta["tmux_target"], "-p"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _wait_pane_contains(base_url: str, session_id: str, needle: str) -> str:
    deadline = time.monotonic() + _TUI_MENU_TIMEOUT_S
    pane = ""
    while time.monotonic() < deadline:
        pane = _terminal_pane(base_url, session_id)
        if needle in pane:
            return pane
        time.sleep(1)
    raise AssertionError(f"terminal never rendered {needle!r}; pane:\n{pane}")


@pytest.mark.timeout(600)
def test_claude_menu_lists_plugin_commands_the_terminal_lists(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A plugin command the terminal offers as ``/plugin:name`` is also in the chat menu.

    Installs the plugin with the real Claude CLI, runs a host whose Claude
    config dir carries it, opens the production native Claude session on that
    host, confirms the TUI lists both plugin entries, then reads the chat menu.
    """
    if shutil.which("claude") is None or shutil.which("tmux") is None:
        pytest.skip("needs the claude CLI and tmux")
    claude_config_dir = tmp_path / "claude-config"
    _install_claude_plugin(claude_config_dir, tmp_path / "marketplace")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_env = {
        "CLAUDE_CONFIG_DIR": str(claude_config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    with (
        httpx.Client(base_url=live_server, timeout=20) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            env=host_env,
            command=_host_command(live_server, mock_llm_server_url),
        ) as host,
    ):
        host.wait_online(client, timeout=120, poll_interval=0.5)
        created = create_native_session(
            client,
            live_server,
            harness="claude",
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
        )
        session_id = str(created["session_id"])
        try:
            reviewed_key = json.dumps(f"omnigent:imports-reviewed:{host.host_id}")
            page = request.getfixturevalue("page")
            page.add_init_script(f"localStorage.setItem({reviewed_key}, 'true')")
            page.goto(f"{live_server}/c/{session_id}")
            composer = page.get_by_label("Message the agent")
            expect(composer).to_be_visible(timeout=60_000)

            evidence_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")

            _open_terminal_view(page)
            _wait_terminal_connected(page)
            _focus_tui(page)
            page.keyboard.type(f"/{_PLUGIN}", delay=30)
            _wait_pane_contains(live_server, session_id, f"{_PLUGIN}:review")
            pane = _wait_pane_contains(live_server, session_id, f"{_PLUGIN}:simplify")
            page.wait_for_timeout(1_500)
            if evidence_dir:
                Path(evidence_dir, "terminal-pane.txt").write_text(pane)
                page.screenshot(path=f"{evidence_dir}/terminal-menu.png")
            for _ in range(len(_PLUGIN) + 1):
                page.keyboard.press("Backspace")

            _select_view_mode(page, "Chat")
            expect(composer).to_be_visible(timeout=30_000)
            composer.fill(f"/{_PLUGIN}")
            expect(page.get_by_test_id(f"slash-menu-item-{_PLUGIN}:simplify")).to_be_visible(
                timeout=30_000
            )
            page.wait_for_timeout(1_500)
            if evidence_dir:
                page.screenshot(path=f"{evidence_dir}/chat-menu.png")
            review_row = page.get_by_test_id(f"slash-menu-item-{_PLUGIN}:review")
            try:
                expect(review_row).to_be_visible(timeout=30_000)
            except AssertionError:
                listed = client.get("/v1/skills", params={"session_id": session_id}).json()
                names = [skill["name"] for skill in listed.get("skills", [])]
                raise AssertionError(
                    f"chat menu lacks /{_PLUGIN}:review although the terminal lists it; "
                    f"/v1/skills names={names}"
                ) from None
        finally:
            client.delete(f"/v1/sessions/{session_id}")
