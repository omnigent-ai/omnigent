"""Record native and bridged portable skills in the Claude composer menu.

Set ``CLAUDE_CONFIG_DIR`` to the isolated host's config directory to also
check its user skill tier. The single online host is selected automatically; set
``OMNIGENT_E2E_HOST_ID`` to select one when multiple hosts are connected.
The host-backed menu resolves skills without a model turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import post_session_bundle

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
