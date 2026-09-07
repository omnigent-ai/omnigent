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
round-trip: pick "Opus" → Save → the stored config carries ``model: "opus"``.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect


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


def test_project_settings_model_default_round_trips(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A chosen model default persists into the project config and re-seeds.

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
    page.get_by_test_id("project-settings-save").click()
    expect(page.get_by_test_id("project-settings-save")).to_have_count(0)

    # The stored config must carry the chosen model under the `model` key —
    # the contract the composer's prefill (and the create's model_override)
    # reads it back through.
    config = _get_project_config(base_url, project_id)
    assert config.get("model") == "opus", (
        f"saved project config carries no model default: {config!r}"
    )

    # Reopen — the stored default seeds the control back.
    _open_project_settings(page, project)
    expect(page.get_by_test_id("project-settings-model")).to_contain_text("Opus")
