"""Unreadable Codex plugin skills must not read as an empty, healthy skill catalog.

The host's ``codex plugin list`` and its ``codex app-server`` skill catalog are
scripted to fail before the session is opened, so the composer's ``$`` picker
must explain the incomplete catalog instead of showing the empty-catalog row,
and ``GET /v1/skills`` must carry that warning.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from tests._helpers.host import isolated_host
from tests._helpers.native_session import create_native_session

_PLUGIN_SKILL = "toolkit:review"
_EMPTY_CATALOG_ROW = "No skills available"
_INVENTORY_ERROR = "failed to load plugin inventory (injected by test)"
_WARNING_ROW = "Codex plugin skills unavailable"
_EXIT_SUMMARY = "`codex plugin list` exited 1"
_INVENTORY = {
    "installed": [
        {"marketplaceName": "market", "name": "toolkit", "version": "1.0.0", "enabled": True}
    ],
    "available": [],
}
# Stand-in codex binary: the plugin inventory is scripted (mode/inventory files), and
# fail mode also fails app-servers for the seeded home, which only skill discovery
# starts. Every other invocation, including session app-servers with private homes,
# reaches the real CLI when one is installed.
_SHIM = """#!/bin/sh
if [ "$(cat {mode} 2>/dev/null)" = "fail" ]; then
  failing=
  if [ "$1" = "plugin" ] && [ "$2" = "list" ]; then failing=1; fi
  home=$(cd "$CODEX_HOME" 2>/dev/null && pwd -P)
  if [ "$1" = "app-server" ] && [ "$home" = {codex_home} ]; then failing=1; fi
  if [ -n "$failing" ]; then
    echo "Error:" {error} >&2
    exit 1
  fi
fi
if [ "$1" = "plugin" ] && [ "$2" = "list" ]; then
  cat {inventory}
  exit 0
fi
if [ -n {real} ]; then exec {real} "$@"; fi
if [ "$1" = "--version" ]; then echo "codex-cli 0.0.0-test"; exit 0; fi
echo "codex CLI is not installed in this environment" >&2
exit 127
"""


def _seed_plugin_cache(codex_home: Path) -> None:
    plugin = codex_home / "plugins" / "cache" / "market" / "toolkit" / "1.0.0"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "toolkit", "version": "1.0.0"})
    )
    skill = plugin / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review a diff.\n---\nReview instructions.\n"
    )


def _write_codex_shim(root: Path, codex_home: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    mode = root / "mode"
    mode.write_text("ok")
    inventory = root / "inventory.json"
    inventory.write_text(json.dumps(_INVENTORY))
    shim = root / "codex"
    shim.write_text(
        _SHIM.format(
            mode=shlex.quote(str(mode)),
            codex_home=shlex.quote(str(codex_home.resolve())),
            inventory=shlex.quote(str(inventory)),
            error=shlex.quote(_INVENTORY_ERROR),
            real=shlex.quote(shutil.which("codex") or ""),
        )
    )
    shim.chmod(0o755)
    return shim, mode


def _skill_names(client: httpx.Client, **params: str) -> list[str]:
    response = client.get("/v1/skills", params=params)
    response.raise_for_status()
    return [skill["name"] for skill in response.json()["skills"]]


def test_failed_plugin_inventory_is_not_shown_as_an_empty_catalog(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = tmp_path / "codex-home"
    _seed_plugin_cache(codex_home)
    shim, mode = _write_codex_shim(tmp_path / "codex-shim", codex_home)
    host_env = {
        "OMNIGENT_RUNNER_ZYGOTE": "0",
        "OMNIGENT_CODEX_PATH": str(shim),
        "CODEX_HOME": str(codex_home),
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
    }
    with (
        httpx.Client(base_url=live_server, timeout=30.0) as client,
        isolated_host(tmp_path / "host", live_server, env=host_env) as host,
    ):
        host.wait_online(client, timeout=60, poll_interval=0.2)
        assert _PLUGIN_SKILL in _skill_names(
            client, host_id=host.host_id, harness="codex-native", path=str(workspace)
        )
        mode.write_text("fail")
        session_id = create_native_session(
            client,
            live_server,
            harness="codex",
            metadata={"host_id": host.host_id, "workspace": str(workspace)},
        )["session_id"]
        try:
            page = request.getfixturevalue("page")
            page.add_init_script(
                "localStorage.setItem("
                f"{json.dumps(f'omnigent:imports-reviewed:{host.host_id}')}, 'true')"
            )
            page.goto(f"{live_server}/c/{session_id}")
            composer = page.get_by_label("Message the agent")
            expect(composer).to_be_visible(timeout=60_000)
            composer.click()
            composer.press_sequentially("$")
            skills_rows = page.get_by_role("status").filter(has_text=re.compile("skills", re.I))
            skill_items = page.locator('[data-testid^="slash-menu-item-"]')
            expect(skills_rows.or_(skill_items).first).to_be_visible(timeout=20_000)
            expect(skills_rows.filter(has_text="Loading skills")).to_have_count(0, timeout=20_000)
            # A failed inventory must read as an incomplete catalog, never as an empty one.
            expect(skills_rows.filter(has_text=_EMPTY_CATALOG_ROW)).to_have_count(0)
            warning_rows = skills_rows.filter(has_text=_WARNING_ROW)
            expect(warning_rows).to_have_count(1)
            expect(warning_rows.first.get_by_role("button", name="Retry")).to_be_visible()
            observed_rows = skills_rows.all_inner_texts()
            # Skills the inventory could not read are absent, not served stale beside the warning.
            plugin_rows = page.get_by_test_id(f"slash-menu-item-{_PLUGIN_SKILL}").count()
            api = client.get("/v1/skills", params={"session_id": session_id})
            host_log = host.log_path.read_text(errors="replace")
        finally:
            client.delete(f"/v1/sessions/{session_id}", timeout=10.0)

    observed = (
        f"session {session_id}: picker rows {observed_rows!r}, plugin rows {plugin_rows}, "
        f"GET /v1/skills -> {api.status_code} {api.text}"
    )
    assert any(_EXIT_SUMMARY in row for row in observed_rows), (
        f"{observed}: the picker's explanation does not name the failed command"
    )
    # Host paths and CLI output stay out of the menu; the host log keeps them for investigation.
    assert _INVENTORY_ERROR not in " ".join(observed_rows), observed
    assert _INVENTORY_ERROR in host_log, (
        f"{observed}: host.log lacks the `codex plugin list` output"
    )
    assert plugin_rows == 0, f"{observed}: a plugin skill was listed despite the failed inventory"
    assert api.status_code == 200, observed
    body = api.json()
    assert body["skills"] == [], observed
    assert [w for w in body["warnings"] if _WARNING_ROW in w and _EXIT_SUMMARY in w], (
        f"{observed}: a failed plugin inventory was answered as a successful empty catalog"
    )
