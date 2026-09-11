"""Recording driver: the claude-native composer menu matches the terminal.

Drives the fixed journey in a real browser for the after-fix clip: a
``claude-native`` session whose workspace carries skills under both
``.claude/skills`` and ``.agents/skills``, with a user skill in
``$CLAUDE_CONFIG_DIR/skills`` (the env var must be exported to the spawned
runner before pytest starts, with the skill seeded inside it). Opening the
composer's ``/`` menu must list the ``.claude`` tier and the config-dir
user tier — the skills the Claude terminal itself loads — and must NOT
list the ``.agents/skills`` entry the terminal cannot invoke.

The harness is ``claude-native`` deliberately: the terminal-matching menu
resolution is native-only, so a native spec is what exercises it. The
``/skills`` endpoint resolves from the spec's harness independent of whether
the CLI terminal actually launches, so the composer menu is populated even
under the suite's mock LLM.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

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
    runner_id: str,
    tmp_path: Path,
) -> None:
    """The ``/`` menu shows the Claude tiers and omits ``.agents/skills``.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    :param runner_id: Token-bound id of the spawned runner to bind to.
    :param tmp_path: Workspace root seeded with the two workspace tiers.
    """
    workspace = tmp_path / "workspace"
    _seed_skill(workspace / ".claude" / "skills", "claude-dir-skill", "workspace claude skill")
    _seed_skill(workspace / ".agents" / "skills", "agents-only-skill", "workspace agents skill")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if not cfg:
        pytest.skip("export CLAUDE_CONFIG_DIR to a writable dir before pytest")
    _seed_skill(Path(cfg) / "skills", "user-cfg-skill", "user config-dir skill")

    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(workspace)})},
        files={"bundle": ("agent.tar.gz", _bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    ).raise_for_status()

    page.goto(f"{live_server}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/")

    # Both tiers Claude Code itself loads are listed…
    expect(page.get_by_test_id("slash-menu-item-claude-dir-skill")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("slash-menu-item-user-cfg-skill")).to_be_visible()
    # …and the .agents/skills entry the terminal can't invoke is not.
    expect(page.get_by_test_id("slash-menu-item-agents-only-skill")).to_have_count(0)
    # Hold the corrected menu on screen so the clip ends on the outcome.
    page.wait_for_timeout(1_500)
