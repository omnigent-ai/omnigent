"""Browser e2e: project config default model for Claude Code / Codex.

A project's stored ``config`` (edited via the "Project settings" dialog,
``web/src/shell/ProjectSettingsDialog.tsx``) holds the defaults pre-filled
into the new-chat composer for sessions started in that project: host,
working directory, agent, worktree, base branch. This suite guards that it
also holds a **default model** when the project's default agent is a native
coding harness with a model picker (Claude Code / Codex) — so a project can
pin e.g. "Opus for this repo" the same way it pins an agent.

Journey this guards: open the project folder kebab → "Project settings" →
pick Claude Code (or Codex) as the default Agent → the dialog offers a Model
default control (``data-testid="project-settings-model"``). The guarded
defect: the dialog rendered no model control at all (``ProjectConfig`` in
``web/src/lib/projectsApi.ts`` had no model key), so a project could not
store a default model.

The project is created directly via ``POST /v1/projects`` (mirrors
``test_project_settings_dialog.py``), and the native built-ins
(``claude-native-ui`` / ``codex-native-ui``) are resolved from the live
``GET /v1/agents`` catalog — they are seeded unconditionally at startup.

The e2e_ui runner registers no host, so Codex (whose catalog is
host-resolved) has no models to offer — its control is present but disabled.
Claude Code carries a static version-agnostic alias catalog
(``CLAUDE_NATIVE_MODELS``), so its control is populated and drives the
round-trip: pick "Opus" and Effort "High" → Save → the stored config carries
both. A separate journey starts a session from the project against a real
host whose Claude Code CLI talks to the mock model, and checks the seeded
``reasoning_effort``.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.host import isolated_host

_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"


def _create_project(base_url: str, name: str) -> str:
    """Create an empty first-class project via the API; return its id."""
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["id"]


def _get_project_config(base_url: str, project_id: str) -> dict:
    """Read a project's stored config via ``GET /v1/projects/{id}``."""
    resp = httpx.get(f"{base_url}/v1/projects/{project_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json()["config"]


def _resolve_builtin_agent(base_url: str, name: str) -> dict:
    """Resolve a packaged built-in agent (e.g. ``claude-native-ui``) by name."""
    resp = httpx.get(f"{base_url}/v1/agents", params={"limit": 100}, timeout=30.0)
    resp.raise_for_status()
    agent = next((a for a in resp.json()["data"] if a["name"] == name), None)
    assert agent is not None, (
        f"{name} built-in not registered on the test server — it is seeded "
        "unconditionally at startup, so its absence is a server bug"
    )
    return agent


def _open_project_settings(page: Page, project: str) -> None:
    """Open the folder kebab → "Project settings" for *project*."""
    actions = page.get_by_role("button", name=f"Project actions for {project}", exact=True)
    expect(actions).to_be_visible()
    actions.click()
    page.get_by_test_id("project-settings").click()
    # The dialog's Save button confirms the editor mounted + the config fetch
    # settled (Save is disabled while loading).
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


def _pick_default_agent(page: Page, agent_id: str) -> None:
    """In the open settings dialog, pick *agent_id* as the default agent."""
    field = page.get_by_test_id("project-settings-agent")
    trigger = field.get_by_test_id("new-chat-landing-agent-select")
    expect(trigger).to_be_enabled()
    trigger.click()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    expect(row).to_be_visible()
    row.click()


def _session_effort(base_url: str, session_id: str) -> str | None:
    """Return the session's persisted ``reasoning_effort`` (``None`` = unset)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    effort = resp.json().get("reasoning_effort")
    return effort if isinstance(effort, str) else None


@pytest.mark.parametrize(
    ("builtin_name", "display_name"),
    [
        pytest.param("claude-native-ui", "Claude Code", id="claude-code"),
        pytest.param("codex-native-ui", "Codex", id="codex"),
    ],
)
def test_project_settings_offers_model_default_for_native_harness(
    page: Page,
    seeded_session: tuple[str, str],
    builtin_name: str,
    display_name: str,
) -> None:
    """Picking a native coding harness as the project's default agent must
    surface a Model default control in the Project settings dialog.

    Guards the defect where the dialog offered only Host / Working
    directory / Random worktree / Base branch / Agent — no model field — so a
    project cannot store a default model for Claude Code / Codex sessions.
    The ``project-settings-model`` visibility assertion is the concrete
    failure; a fix that adds the control (for harnesses with a model picker)
    flips this test to green.
    """
    base_url, session_id = seeded_session
    agent = _resolve_builtin_agent(base_url, builtin_name)
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)
    _pick_default_agent(page, agent["id"])

    # The picked harness is reflected in the trigger label, so the selection
    # itself definitely landed before we look for the model control.
    trigger = page.get_by_test_id("project-settings-agent").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    expect(trigger).to_contain_text(display_name)

    # ── The bug: no Model default control exists for the picked harness. ──
    # Claude Code / Codex both take a model override, so the settings dialog
    # should surface the control (populated for Claude's static catalog;
    # present-but-disabled for Codex, whose catalog needs a host).
    expect(page.get_by_test_id("project-settings-model")).to_be_visible(timeout=5_000)


def test_project_settings_model_and_effort_defaults_round_trip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Chosen model and effort defaults persist into the project config and re-seed.

    Extends the visibility guard into the full user journey the feature
    needs: pick Claude Code as the default agent, choose "Opus" in the model
    control, Save (``PATCH /v1/projects/{id}``), then reopen — the stored
    config must carry the chosen model and the reopened dialog must seed the
    control back to it. Today this fails at the same missing-control
    assertion as the visibility test; after the fix it proves the write
    actually reaches the server rather than a dummy field rendering.
    """
    base_url, session_id = seeded_session
    agent = _resolve_builtin_agent(base_url, "claude-native-ui")
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)
    _pick_default_agent(page, agent["id"])

    # The missing control — same concrete failure as the visibility test.
    model = page.get_by_test_id("project-settings-model")
    expect(model).to_be_visible(timeout=5_000)

    # Choose "Opus" (a version-agnostic Claude Code alias from
    # CLAUDE_NATIVE_MODELS) and save. The option row may render as a Radix
    # Select option or a dropdown menu item depending on the control.
    model.click()
    option = page.get_by_role("option", name="Opus", exact=True)
    if option.count() == 0:
        option = page.get_by_role("menuitem", name="Opus", exact=True)
    expect(option.first).to_be_visible()
    option.first.click()

    effort = page.get_by_test_id("project-settings-effort")
    expect(effort).to_contain_text("No default")
    effort.click()
    page.get_by_role("option", name="High", exact=True).click()
    expect(effort).to_contain_text("High")
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # The stored config must carry the chosen model under the `model` key —
    # the contract the composer's prefill (and the create's model_override)
    # reads it back through.
    config = _get_project_config(base_url, project_id)
    assert config.get("model") == "opus", (
        f"saved project config carries no model default: {config!r}"
    )
    assert config.get("effort") == "high", f"saved config carries no effort: {config!r}"

    # Reopen — the stored default seeds the control back.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-model")).to_contain_text("Opus")
    expect(page.get_by_test_id("project-settings-effort")).to_contain_text("High")


@pytest.mark.timeout(600)
def test_project_effort_default_seeds_new_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A new session started in the project comes up with the project's effort.

    The project's default agent is Claude Code with effort "high"; "New session
    in <project>" must show "High" in the composer, and the created session must
    persist ``reasoning_effort == "high"``.
    """
    if shutil.which("claude") is None or shutil.which("tmux") is None:
        pytest.skip("needs the claude CLI and tmux")
    agent = _resolve_builtin_agent(live_server, "claude-native-ui")
    project = f"Project {uuid.uuid4().hex[:6]}"
    project_id = _create_project(live_server, project)
    httpx.patch(
        f"{live_server}/v1/projects/{project_id}",
        json={"config": {"agent_id": agent["id"], "effort": "high"}},
        timeout=10.0,
    ).raise_for_status()
    providers = {
        "e2e-claude": {
            "kind": "key",
            "default": ["anthropic"],
            "anthropic": {
                "base_url": mock_llm_server_url,
                "api_key": "mock-key",
                "models": {"default": _CLAUDE_MOCK_MODEL},
            },
        }
    }
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        httpx.Client(base_url=live_server, timeout=20) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            config={"providers": providers},
            env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            cwd=workspace,
        ) as host,
    ):
        host.wait_online(client, timeout=120, poll_interval=0.5)
        created_id: str | None = None
        page: Page = request.getfixturevalue("page")
        reviewed_key = json.dumps(f"omnigent:imports-reviewed:{host.host_id}")
        page.add_init_script(f"localStorage.setItem({reviewed_key}, 'true')")
        try:
            page.goto(f"{live_server}/")
            new_in_project = page.get_by_role("link", name=f"New session in {project}", exact=True)
            expect(new_in_project).to_be_visible(timeout=30_000)
            new_in_project.hover()
            new_in_project.click()

            agent_picker = page.get_by_test_id("new-chat-landing-agent-select")
            expect(agent_picker).to_be_enabled(timeout=60_000)
            expect(agent_picker).to_have_attribute(
                "aria-label", re.compile("Claude Code"), timeout=30_000
            )
            expect(page.get_by_test_id("new-chat-landing-agent-effort-value")).to_have_text(
                "High", timeout=30_000
            )

            page.get_by_test_id("new-chat-landing-input").fill("Say hello.")
            submit = page.get_by_test_id("new-chat-landing-submit")
            expect(submit).to_be_enabled(timeout=30_000)
            submit.click()
            page.wait_for_url(lambda url: "/c/" in url and "temp:" not in url, timeout=90_000)
            created_id = page.url.rstrip("/").split("/c/")[-1].split("?")[0]

            deadline = time.time() + 30
            while time.time() < deadline and _session_effort(live_server, created_id) is None:
                time.sleep(0.5)
            assert _session_effort(live_server, created_id) == "high"
            expect(page.get_by_test_id("composer-agent-effort-value")).to_have_text(
                "High", timeout=60_000
            )
        finally:
            page.context.close()
            if created_id:
                client.delete(f"/v1/sessions/{created_id}")
