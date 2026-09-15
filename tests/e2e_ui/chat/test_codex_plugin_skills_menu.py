"""E2E: enabled Codex plugin skills appear in the composer's ``/`` menu.

Guards plugin-skill discovery: Omnigent must discover skills bundled by
installed Codex plugins. Codex installs a plugin into
``$CODEX_HOME/plugins/cache/<marketplace>/<plugin>/<version>/`` and records
its enabled state in ``config.toml`` under ``[plugins."<plugin>@<marketplace>"]``
(layout verified against the real ``codex plugin add``, codex-cli 0.139.0).
Discovery that enumerates only ``<bundle>/skills`` and ``$CODEX_HOME/skills``
never lets an enabled plugin's ``skills/`` enter the session skill inventory
the composer menu draws from — the regression this test pins.

Journey (the reporter's): install + enable a Codex plugin that provides a
skill, add a standalone skill under ``$CODEX_HOME/skills`` as a control,
start an Omnigent session on the Codex Native harness, open the composer's
``/`` skill menu. The standalone skill appears; the enabled plugin skill
must too (the final assertion is the one that fails on a build missing
plugin discovery). A second, disabled plugin is seeded alongside so the
enabled-state resolution is guarded: its skill must never appear.

``CODEX_HOME`` must be exported to a writable dir before pytest starts (the
spawned runner resolves the host Codex home from its own environment via
``_codex_home_config_source_from_env``, so a per-test monkeypatch cannot
reach it — same convention as ``CLAUDE_CONFIG_DIR`` in
``test_claude_skills_menu_terminal_parity.py``). The test seeds the plugin
cache/registry inside it idempotently, so a home prepared with the real
``codex plugin marketplace add`` + ``codex plugin add`` works unchanged.

The harness is ``codex-native`` deliberately: only the native provider reads
``$CODEX_HOME`` (mirroring the terminal). The ``/skills`` endpoint resolves
from the spec's harness independent of whether a Codex CLI actually
launches, so the menu is populated under the suite's mock LLM.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

# Strict spec_version-1 bundle with a native Codex executor — the same shape
# test_custom_codex_native_controls.py drives (chat-first, no wrapper labels),
# so the composer renders the plain chat UI with the slash menu.
_CODEX_AGENT_YAML = """\
spec_version: 1
name: codex-plugin-skills

executor:
  type: omnigent
  model: gpt-5.6-sol
  config:
    harness: codex-native

prompt: |
  You are a friendly assistant.
"""

_MARKETPLACE = "local-market"
_ENABLED_PLUGIN = "demo-plugin"
_DISABLED_PLUGIN = "off-plugin"
_ENABLED_PLUGIN_SKILL = "plugin-demo-skill"
_DISABLED_PLUGIN_SKILL = "disabled-plugin-skill"
_STANDALONE_SKILL = "standalone-skill"


def _bundle() -> bytes:
    """Gzipped tarball of the codex-native agent spec."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CODEX_AGENT_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _seed_skill(skills_dir: Path, name: str, description: str) -> None:
    """Write a minimal ``<skills_dir>/<name>/SKILL.md`` (idempotent)."""
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n"
    )


def _seed_plugin(codex_home: Path, plugin: str, version: str, skill: str) -> None:
    """Materialize an installed plugin in Codex's content-addressed cache.

    Mirrors exactly what ``codex plugin add`` produces (verified against
    codex-cli 0.139.0): the plugin root lands at
    ``plugins/cache/<marketplace>/<plugin>/<version>/`` with its
    ``.codex-plugin/plugin.json`` manifest and its ``skills/`` payload.
    """
    root = codex_home / "plugins" / "cache" / _MARKETPLACE / plugin / version
    manifest_dir = root / ".codex-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": plugin, "version": version, "description": f"{plugin} test plugin"})
    )
    _seed_skill(root / "skills", skill, f"Skill provided by the {plugin} Codex plugin.")


def _register_plugin(codex_home: Path, plugin: str, enabled: bool) -> None:
    """Record *plugin*'s install/enabled state in ``config.toml``.

    Appends the ``[plugins."<plugin>@<marketplace>"]`` table ``codex plugin
    add`` writes, skipping it when already present so a home prepared by the
    real CLI is left untouched.
    """
    config = codex_home / "config.toml"
    key = f'[plugins."{plugin}@{_MARKETPLACE}"]'
    existing = config.read_text() if config.is_file() else ""
    if key in existing:
        return
    block = f"{key}\nenabled = {'true' if enabled else 'false'}\n"
    config.write_text(f"{existing.rstrip()}\n\n{block}" if existing.strip() else block)


def test_codex_menu_lists_enabled_plugin_skills(
    page: Page,
    live_server: str,
    runner_id: str,
    tmp_path: Path,
) -> None:
    """The ``/`` menu lists an enabled Codex plugin's skill, not a disabled one's.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    :param runner_id: Token-bound id of the spawned runner to bind to.
    :param tmp_path: Per-test workspace root for the session.
    """
    codex_home_env = os.environ.get("CODEX_HOME", "")
    if not codex_home_env:
        pytest.skip("export CODEX_HOME to a writable dir before pytest")
    codex_home = Path(codex_home_env)

    # The control the report names: a standalone skill under
    # $CODEX_HOME/skills that discovery already finds.
    _seed_skill(
        codex_home / "skills",
        _STANDALONE_SKILL,
        "Control skill installed under CODEX_HOME/skills.",
    )
    # An installed AND enabled plugin providing a skill — the bug's subject.
    _seed_plugin(codex_home, _ENABLED_PLUGIN, "1.0.0", _ENABLED_PLUGIN_SKILL)
    _register_plugin(codex_home, _ENABLED_PLUGIN, enabled=True)
    # An installed but DISABLED plugin — its skill must never surface.
    _seed_plugin(codex_home, _DISABLED_PLUGIN, "1.0.0", _DISABLED_PLUGIN_SKILL)
    _register_plugin(codex_home, _DISABLED_PLUGIN, enabled=False)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    create = httpx.post(
        f"{live_server}/v1/sessions",
        # Runner-owned Codex sessions hard-require a workspace
        # (_codex_session_workspace raises without one).
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

    # Control: the standalone $CODEX_HOME/skills entry is discovered, proving
    # the menu resolved skills from this test's Codex home.
    expect(page.get_by_test_id(f"slash-menu-item-{_STANDALONE_SKILL}")).to_be_visible(
        timeout=15_000
    )
    # A disabled plugin's skill must never appear (any name spelling).
    disabled_pattern = re.compile(rf"slash-menu-item-.*{_DISABLED_PLUGIN_SKILL}")
    expect(page.get_by_test_id(disabled_pattern)).to_have_count(0)
    # The bug: the enabled plugin's skill must be offered too. The name may be
    # surfaced bare or plugin-namespaced (e.g. "demo-plugin:plugin-demo-skill");
    # accept either so the assertion pins discovery, not the naming choice.
    # On a buggy build this is the assertion that fails: the menu shows the
    # standalone control while the enabled plugin skill is absent.
    plugin_item = page.get_by_test_id(re.compile(rf"slash-menu-item-.*{_ENABLED_PLUGIN_SKILL}"))
    expect(plugin_item).to_be_visible(timeout=10_000)
    # Hold the menu on screen so a recording ends on the outcome.
    page.wait_for_timeout(1_500)
