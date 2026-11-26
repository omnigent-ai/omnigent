"""Browser e2e: forking onto a new sandbox lists that sandbox's gateway models.

A sandbox the fork will create has no host to ask, so its models come from the
provider. The server, its sandbox provider's model discovery and the SPA are
real; the mock LLM is the provider's gateway and lists the test's models. The
source is a Claude Code session with a working directory and no host, so the
dialog starts on a new sandbox. Its exchange is seeded through the server's
store, and no sandbox is provisioned.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, Response, expect

from tests.e2e_ui.conftest import seed_committed_turn
from tests.e2e_ui.start_session.test_sandbox_gateway_models_without_binding import (
    SANDBOX_LABEL,
    SANDBOX_PROVIDER,
    agent_id_for,
    running_gateway_sandbox,
    sandbox_server_database_uri,
)

GATEWAY_MODELS = ("team-claude-sonnet", "team-gpt-codex")
_SOURCE_AGENT_NAME = "claude-native-ui"
# Fork target: its agent and the harness whose sandbox catalog the picker lists.
_TARGETS = {
    "Claude Code": ("claude-native-ui", "claude-native"),
    "Codex": ("codex-native-ui", "codex-native"),
}
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


@pytest.fixture(scope="module")
def fork_gateway_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """``(base_url, database_uri)`` of a hostless server whose sandbox lists gateway models."""
    workdir = tmp_path_factory.mktemp("fork_gateway_server")
    with running_gateway_sandbox(workdir, mock_llm_server_url, GATEWAY_MODELS) as base_url:
        yield base_url, sandbox_server_database_uri(workdir)


def _claude_code_source(base_url: str, database_uri: str, workspace: Path) -> str:
    """Create a hostless Claude Code session in *workspace* with one settled exchange."""
    response = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id_for(base_url, _SOURCE_AGENT_NAME), "workspace": str(workspace)},
        timeout=30.0,
    )
    response.raise_for_status()
    session_id = response.json()["id"]
    seed_committed_turn(
        session_id,
        prompt="Add retries to the checkout client.",
        reply="The checkout client now retries failed requests.",
        database_uri=database_uri,
    )
    return session_id


@pytest.mark.parametrize("target", list(_TARGETS))
def test_fork_onto_new_sandbox_lists_its_gateway_models(
    page: Page,
    fork_gateway_server: tuple[str, str],
    tmp_path: Path,
    target: str,
) -> None:
    """The Model picker of a fork onto a new sandbox offers Default and the gateway's models.

    :param page: Playwright page fixture (fresh context per test).
    :param fork_gateway_server: ``(base_url, database_uri)`` of the gateway server.
    :param tmp_path: The source session's working directory parent.
    :param target: The agent the fork binds, e.g. ``"Codex"``.
    """
    base_url, database_uri = fork_gateway_server
    session_id = _claude_code_source(base_url, database_uri, tmp_path / "checkout-service")
    agent_name, harness = _TARGETS[target]
    preview_path = f"/v1/sandbox-providers/{SANDBOX_PROVIDER}/harnesses/{harness}/model-options"
    previews: list[Response] = []

    def record_preview(response: Response) -> None:
        if preview_path in response.url:
            previews.append(response)

    page.on("response", record_preview)

    page.goto(f"{base_url}/c/{session_id}")
    assistant = page.locator(_ASSISTANT)
    expect(assistant).to_have_count(1, timeout=30_000)
    assistant.first.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog.get_by_text("Fork from this response")).to_be_visible()
    # With no host connected, the fork starts on a new sandbox.
    expect(dialog.get_by_test_id("fork-session-host-select")).to_have_text(SANDBOX_LABEL)
    agent_select = dialog.get_by_test_id("fork-session-agent-select")
    expect(agent_select).to_contain_text("Claude Code (same as original session)")
    if target != "Claude Code":
        agent_select.click()
        page.get_by_test_id(
            f"fork-session-agent-option-{agent_id_for(base_url, agent_name)}"
        ).click()
        expect(agent_select).to_have_text(target)

    model_select = dialog.get_by_test_id("fork-session-config-model")
    model_select.click()
    options = page.get_by_role("listbox").get_by_role("option")
    expect(options).to_have_text(["Default", *GATEWAY_MODELS])
    expect(page.get_by_text("Select a host to list models")).to_have_count(0)
    options.filter(has_text=GATEWAY_MODELS[1]).click()
    expect(model_select).to_have_text(GATEWAY_MODELS[1])

    # The rows are the server's gateway listing for this harness, not a host's.
    assert previews, f"the dialog never requested {preview_path}"
    preview = previews[-1].json()
    assert preview["status"] == "ready", preview
    assert [row["id"] for row in preview["models"]] == list(GATEWAY_MODELS), preview
