"""Codex composers list workspace and repo ``.agents/skills`` the way Claude's composer does."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.db.utils import builtin_agent_id
from tests._helpers.host import isolated_host
from tests._helpers.native_session import create_native_session
from tests._helpers.session import bundle_files, post_session_bundle

WORKSPACE_SKILL = "workspace-agents-skill"
REPO_ROOT_SKILL = "repo-root-agents-skill"
CODEX_HOME_SKILL = "codex-home-skill"
WRAPPED_CODEX_SPEC = b"""\
name: codex_agents_skills
prompt: You are a friendly assistant.

executor:
  model: gpt-4o
  harness: codex

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _write_skill(skills_dir: Path, name: str) -> None:
    directory = skills_dir / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n\nBody of {name}.\n"
    )


def _mock_providers(mock_url: str) -> dict[str, object]:
    return {
        "providers": {
            "e2e-claude": {
                "kind": "key",
                "default": ["anthropic"],
                "anthropic": {
                    "base_url": mock_url,
                    "api_key": "mock-key",
                    "models": {"default": "claude-sonnet-4-20250514"},
                },
            },
            "e2e-openai": {
                "kind": "key",
                "default": ["openai"],
                "openai": {
                    "base_url": f"{mock_url}/v1",
                    "api_key": "mock-key",
                    "wire_api": "responses",
                    "models": {"default": "gpt-4o"},
                },
            },
        }
    }


def _missing_rows(
    page: Page, composer: Locator, sigil: str, control: str, expected: list[str]
) -> list[str]:
    composer.fill(sigil)
    expect(page.get_by_test_id(f"slash-menu-item-{control}")).to_be_visible(timeout=60_000)
    missing = []
    for name in expected:
        composer.fill(f"{sigil}{name}")
        try:
            expect(page.get_by_test_id(f"slash-menu-item-{name}")).to_be_visible(timeout=5_000)
        except AssertionError:
            expect(page.get_by_text("No matching skills")).to_be_visible()
            missing.append(name)
    composer.fill("")
    return missing


@pytest.mark.browser_context_args(record_video_size={"width": 1280, "height": 720})
def test_codex_skill_menus_list_workspace_and_repo_agents_skills(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    output_path: str,
) -> None:
    codex = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if not codex or not shutil.which("claude"):
        pytest.skip("Codex and Claude CLIs required for host skill discovery")
    repo = tmp_path / "repo"
    workspace = repo / "app"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=10)
    _write_skill(workspace / ".agents" / "skills", WORKSPACE_SKILL)
    _write_skill(repo / ".agents" / "skills", REPO_ROOT_SKILL)
    codex_home = tmp_path / "host" / "home" / ".codex"
    _write_skill(codex_home / "skills", CODEX_HOME_SKILL)
    evidence = Path(output_path)
    evidence.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    with (
        httpx.Client(base_url=live_server, timeout=30) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            name="Codex agents skills host",
            config=_mock_providers(mock_llm_server_url),
            env={
                "OMNIGENT_RUNNER_ZYGOTE": "0",
                "OMNIGENT_CODEX_PATH": codex,
                "CODEX_HOME": str(codex_home),
                "OPENAI_API_KEY": "mock-key",
                "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            },
        ) as host,
    ):
        host.wait_online(client, timeout=60, poll_interval=0.2)
        metadata = {"host_id": host.host_id, "workspace": str(workspace)}
        try:
            sessions = {}
            for label, harness in (("codex-native", "codex"), ("claude-native", "claude")):
                sessions[label] = create_native_session(
                    client, live_server, harness=harness, metadata=metadata
                )["session_id"]
                created.append(sessions[label])
            response = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files({"codex_agents_skills.yaml": WRAPPED_CODEX_SPEC}),
                metadata=metadata,
                timeout=30.0,
            )
            response.raise_for_status()
            sessions["codex"] = response.json()["session_id"]
            created.append(sessions["codex"])

            page = request.getfixturevalue("page")
            page.add_init_script(
                f"localStorage.setItem('omnigent:imports-reviewed:{host.host_id}', 'true')"
            )
            expected = [WORKSPACE_SKILL, REPO_ROOT_SKILL]
            missing: dict[str, list[str]] = {}
            for label, sigil, control in (
                ("claude-native", "/", WORKSPACE_SKILL),
                ("codex-native", "$", CODEX_HOME_SKILL),
                ("codex", "/", CODEX_HOME_SKILL),
            ):
                page.goto(f"{live_server}/c/{sessions[label]}")
                composer = page.get_by_label("Message the agent")
                expect(composer).to_be_visible(timeout=60_000)
                missing[label] = _missing_rows(page, composer, sigil, control, expected)
                catalog = client.get("/v1/skills", params={"session_id": sessions[label]})
                (evidence / f"skills-{label}.json").write_text(catalog.text)

            preferences = {
                "omnigent:recent-workspaces": json.dumps({host.host_id: [str(workspace)]}),
                "omnigent:last-agent-id": builtin_agent_id("codex-native-ui"),
                "omnigent:last-host-choice": host.host_id,
            }
            page.add_init_script(
                f"for (const [key, value] of Object.entries({json.dumps(preferences)})) "
                "localStorage.setItem(key, value);"
            )
            page.goto(live_server)
            landing = page.get_by_test_id("new-chat-landing-input")
            expect(landing).to_be_visible(timeout=30_000)
            missing["codex-native landing"] = _missing_rows(
                page, landing, "$", CODEX_HOME_SKILL, expected
            )
            page.context.close()
        finally:
            for session_id in created:
                client.delete(f"/v1/sessions/{session_id}", timeout=10)
    (evidence / "missing-rows.json").write_text(json.dumps(missing, indent=1))
    assert missing == {label: [] for label in missing}
