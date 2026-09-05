"""E2E: plugin slash-commands must appear in the ``/`` menu next to plugin skills.

Reproduces a plugin-command discovery gap: after installing/enabling a Claude Code plugin that
ships both **commands** (``<plugin>/commands/<name>.md``) and **skills**
(``<plugin>/skills/<dir>/SKILL.md``), typing ``/`` in the web composer lists
only the plugin's *skills* — its *commands* never show up, so a user cannot
discover (or tab-complete) ``/kb-review``-style plugin commands they can run
in Claude Code directly.

Journey (the reporter's, with the knowledge-base plugin):

1. install + enable a Claude Code plugin carrying a command
   (``commands/kb-review.md``) and a skill (``skills/kb-search/SKILL.md``)
   in the host's ``~/.claude``
2. start an Omnigent session on a claude harness against that host
3. type ``/`` in the composer
4. the suggestions menu lists the plugin's skill
   (``/knowledge-base:kb-search``) but NOT its command (``kb-review``)

The rig spawns a dedicated runner whose ``HOME`` points at a seeded fake
home directory (the same pattern as ``test_absolute_watchdog_spares_active_turn``),
so the runner's harness-aware skill discovery
(``omnigent/spec/skill_sources.py``) walks a hermetic ``~/.claude`` carrying
exactly one enabled plugin. The session's harness is ``claude-sdk`` (family
"claude"), which routes discovery through ``claude_host_skills`` — the
provider whose plugin walk only scans ``skills/`` and never ``commands/``,
which is the suspected root cause. No agent turn ever runs: the ``/skills``
resolution is spec+filesystem only, so the test needs no model or credentials.

The command-row assertion accepts either menu spelling (bare ``kb-review``
or namespaced ``knowledge-base:kb-review``) so the test pins the user-visible
outcome — the command is discoverable — without prescribing how a fix
namespaces it.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
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

# Boot budget for the dedicated runner to register with the live server.
_RUNNER_ONLINE_TIMEOUT_S = 60.0
# Budget for the server's background runner-skills fetch to land in the
# session snapshot (single-flight kicked by the SPA's own snapshot polls).
_SNAPSHOT_SKILLS_TIMEOUT_S = 90.0

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
    """Seed a hermetic ``$HOME`` with one enabled Claude Code plugin.

    Layout mirrors the real Claude Code plugin install: the plugin lives
    under the plugins cache (``~/.claude/plugins/cache/<marketplace>/<plugin>/
    <version>``), ``installed_plugins.json`` maps the ``<plugin>@<marketplace>``
    key to that installPath, and ``settings.json`` enables it. The plugin
    carries BOTH a command (``commands/kb-review.md``) and a skill
    (``skills/kb-search/SKILL.md``) so the menu assertion can separate
    "plugin discovery ran" (skill row present) from the bug ("commands are
    missing").

    :param home: The fake home directory to create.
    """
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
def plugin_claude_runner(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn a dedicated runner whose ``HOME`` carries the seeded plugin.

    The runner's skill discovery reads ``Path.home()``, so pointing the
    subprocess ``HOME`` at the seeded directory makes the enabled plugin the
    only host skill source it sees — hermetic from both the CI machine's real
    home and the shared ``live_server`` runner. ``OMNIGENT_CONFIG_HOME`` is
    a fresh empty dir so ambient CI config can't interfere.

    Yields the runner id to bind sessions to.
    """
    from omnigent.runner.identity import token_bound_runner_id

    runner_tmp = tmp_path_factory.mktemp("plugin_home_runner")
    home = runner_tmp / "home"
    _seed_claude_plugin_home(home)
    config_home = runner_tmp / "config-home"
    config_home.mkdir()
    log_path = runner_tmp / "runner.log"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": live_server,
        # The reproduction's variable: the runner's home is the seeded one.
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # child holds its own dup of the fd

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"plugin-home runner exited early (code {proc.returncode}); "
                f"log:\n{log_path.read_text()[-3000:]}"
            )
        try:
            resp = httpx.get(f"{live_server}/v1/runners/{runner_id}/status", timeout=2)
            if resp.status_code == 200 and resp.json().get("online") is True:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)

    if not ready:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        raise RuntimeError(
            f"plugin-home runner did not register within "
            f"{_RUNNER_ONLINE_TIMEOUT_S:.0f}s; log:\n{log_path.read_text()[-3000:]}"
        )

    try:
        yield runner_id
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def plugin_session(
    live_server: str,
    plugin_claude_runner: str,
) -> Iterator[tuple[str, str]]:
    """Create a claude-sdk session bound to the plugin-home runner.

    :returns: ``(base_url, session_id)``.
    """
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
    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": plugin_claude_runner},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _wait_for_plugin_skills_in_snapshot(base_url: str, session_id: str) -> list[dict]:
    """Poll the session snapshot until the runner-resolved skills land.

    The server fetches runner-owned skills in the background off the
    snapshot's critical path, so the first polls legitimately return an
    empty list. Waiting API-side (rather than retyping ``/`` in the
    browser) keeps the menu assertion deterministic.

    :param base_url: Live server base URL.
    :param session_id: The bound session id.
    :returns: The snapshot's ``skills`` list once the plugin skill appears.
    :raises AssertionError: If the plugin skill never arrives — that would
        be a rig failure (plugin discovery never ran), not the bug itself.
    """
    deadline = time.monotonic() + _SNAPSHOT_SKILLS_TIMEOUT_S
    last: list[dict] = []
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if resp.status_code == 200:
            last = resp.json().get("skills") or []
            if any(s.get("name", "").endswith(_SKILL) for s in last):
                return last
        time.sleep(1.0)
    raise AssertionError(
        f"plugin skill {_SKILL!r} never reached the session snapshot within "
        f"{_SNAPSHOT_SKILLS_TIMEOUT_S:.0f}s (rig failure, not the bug); "
        f"last skills: {last!r}"
    )


@pytest.mark.timeout(600)
def test_plugin_commands_listed_in_slash_menu(
    page: Page,
    plugin_session: tuple[str, str],
) -> None:
    """Typing ``/`` must surface the enabled plugin's commands, not only its skills.

    The skill row (``/knowledge-base:kb-search``) is asserted first as the
    control: it proves the runner's plugin discovery ran and the menu is
    populated from it. The command row (``kb-review``, bare or namespaced)
    must then be present too — pre-fix it never appears because plugin
    discovery only walks ``skills/`` and ignores ``commands/``.

    :param page: Playwright page (fresh context per test).
    :param plugin_session: ``(base_url, session_id)`` bound to the
        plugin-home runner.
    """
    base_url, session_id = plugin_session
    _wait_for_plugin_skills_in_snapshot(base_url, session_id)

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
