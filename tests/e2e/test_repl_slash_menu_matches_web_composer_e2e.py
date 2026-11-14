"""The terminal REPL's slash-command menu offers what the web composer offers.

The web composer resolves a Claude session's commands through the harness-aware
provider (host skills, the project's ``.claude/skills`` and enabled plugin skills
as ``<plugin>:<skill>``). The REPL built by ``omnigent run <agent> --harness
claude-sdk`` must list and dispatch the same commands. Mock LLM only: the menu
and the ``Unknown command`` reply are rendered locally.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.omnigent._pexpect_harness import (
    STATE_RUNNING,
    clean_exit,
    ensure_repl_test_theme_env,
    spawn_omnigent_run,
    strip_ansi,
    submit_prompt,
    wait_for_ready,
)
from tests.e2e.omnigent._repl_test_helpers import drain_for

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELP_ROW_RE = re.compile(r"^\s{2,}(/[\w:?-]+)\s{2,}\S", re.M)

pytestmark = pytest.mark.timeout(300)


def _write_skill(skills_dir: Path, name: str, description: str) -> None:
    (skills_dir / name).mkdir(parents=True)
    (skills_dir / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nSay {name.upper()} and stop.\n"
    )


def _seed_claude_home(home: Path) -> None:
    """A user-tier skill plus one installed, enabled Claude Code plugin with a skill."""
    claude = home / ".claude"
    _write_skill(claude / "skills", "user-tier-skill", "user-tier skill")
    _write_skill(claude / "plugins" / "cache" / "figplug" / "skills", "plug-skill", "plugin skill")
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"figplug@local": True}}))
    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {"figplug@local": [{"scope": "user", "installPath": "cache/figplug"}]},
            }
        )
    )


def _seed_project(project: Path) -> None:
    """An agent spec next to the project's own ``.claude/skills``."""
    project.mkdir()
    (project / "agent.yaml").write_text("name: slash-demo\nprompt: Answer in one sentence.\n")
    _write_skill(project / ".claude" / "skills", "ws-proj-skill", "project skill")


def _read_until(child: pexpect.spawn, pattern: str, *, timeout: float, settle: float = 0.0) -> str:
    """ANSI-stripped PTY output consumed until *pattern* matches, plus *settle* seconds more.

    The raw stream is stripped once, so an escape sequence split across reads
    cannot leave stray bytes inside a row.
    """
    raw = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            raw += drain_for(child, 0.5)
        except pexpect.EOF:
            raw += child.before or ""
            exited = True
        else:
            exited = False
        if re.search(pattern, strip_ansi(raw)):
            if settle and not exited:
                raw += drain_for(child, settle)
            return strip_ansi(raw)
        if exited:
            break
    raise AssertionError(f"{pattern!r} not rendered within {timeout}s:\n{strip_ansi(raw)[-3000:]}")


@pytest.fixture
def repl_env(mock_llm_server_url: str, tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    _seed_claude_home(home)
    config_home = tmp_path / "omnigent-config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text("auth:\n  type: none\n")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            ("ANTHROPIC_", "OPENAI_", "DATABRICKS_", "CLAUDE_", "CODEX_", "OMNIGENT_")
        )
    }
    env.update(
        HOME=str(home),
        OMNIGENT_CONFIG_HOME=str(config_home),
        OMNIGENT_DATA_DIR=str(tmp_path / "omnigent-data"),
        OMNIGENT_NO_UPDATE_CHECK="1",
        OMNIGENT_SKIP_ONBOARD="1",
        PYTHONPATH=f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        TERM="xterm-256color",
        PROMPT_TOOLKIT_NO_CPR="1",
        ANTHROPIC_API_KEY="mock-key",
        ANTHROPIC_BASE_URL=mock_llm_server_url,
    )
    return ensure_repl_test_theme_env(env)


def test_claude_repl_menu_lists_plugin_and_project_skills(
    repl_env: dict[str, str], tmp_path: Path
) -> None:
    project = tmp_path / "project"
    _seed_project(project)
    child = spawn_omnigent_run(
        Path(sys.executable),
        project / "agent.yaml",
        model="claude-sonnet-4-5",
        harness="claude-sdk",
        env=repl_env,
        cwd=project,
        timeout=120,
    )
    try:
        wait_for_ready(child, timeout=120)

        submit_prompt(child, "/help")
        # The built-in groups print first; settle so the skill rows registered
        # after them have arrived before parsing the listing.
        help_text = _read_until(child, r"/quit\s+Exit", timeout=20, settle=2.0)
        listed = set(_HELP_ROW_RE.findall(help_text))
        assert "/user-tier-skill" in listed, help_text
        # The web composer lists these for the same session; the REPL must too.
        assert {"/figplug:plug-skill", "/ws-proj-skill"} <= listed, sorted(listed)

        submit_prompt(child, "/figplug:plug-skill hi")
        # Stop on the complete rejection line so the negative check sees the full text.
        reply = _read_until(
            child, rf"Unknown command: \S+ · /help for list|{STATE_RUNNING}", timeout=20
        )
        assert "Unknown command: /figplug:plug-skill" not in reply, reply
    finally:
        clean_exit(child, timeout=15)
