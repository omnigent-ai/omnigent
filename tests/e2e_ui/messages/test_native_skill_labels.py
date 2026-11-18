"""Native skill invocations keep readable labels in live and reloaded chat."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec
from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec
from tests._helpers.host import isolated_host
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.helpers.ui_configuration import validate_native_claude_mock_routing

_SKILL = "build-summary"
_SKILL_MARKER = "native-skill-label-probe"
_REPLY = "Build summary complete"


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.timeout(240)
def test_native_skill_labels(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
    output_path: str,
    claude_managed_settings: None,
    harness: str,
) -> None:
    if shutil.which(harness) is None:
        pytest.skip(f"{harness} CLI required")
    if harness == "claude":
        validate_native_claude_mock_routing(mock_llm_server_url)
    protocol = "anthropic" if harness == "claude" else "openai"
    model = "claude-sonnet-4-20250514" if harness == "claude" else "gpt-4o"
    provider = {
        "base_url": mock_llm_server_url + ("" if harness == "claude" else "/v1"),
        "api_key": "mock-key",
        "models": {"default": model},
    }
    if harness == "codex":
        provider["wire_api"] = "responses"
    config = {"providers": {"mock": {"kind": "key", "default": [protocol], protocol: provider}}}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = (
        _materialize_claude_agent_spec(tmp_path)
        if harness == "claude"
        else _materialize_codex_agent_spec(tmp_path, model=model)
    )
    bundle = bundle_files(
        {
            spec.name: spec.read_bytes(),
            f"skills/{_SKILL}/SKILL.md": (
                f"---\nname: {_SKILL}\ndescription: Summarize a build\n---\n\n"
                f"{_SKILL_MARKER}: Reply with exactly '{_REPLY}'.\n"
            ).encode(),
        }
    )
    evidence = Path(output_path)
    evidence.mkdir(parents=True, exist_ok=True)
    reset_mock_llm(mock_llm_server_url)
    # Only the model response is stubbed; the browser, server, host and CLI are real.
    set_fallback_mock_llm(mock_llm_server_url, "default", _REPLY)
    set_fallback_mock_llm(mock_llm_server_url, model, _REPLY)
    with (
        httpx.Client(base_url=live_server, timeout=30) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            config=config,
            env={"OMNIGENT_RUNNER_ZYGOTE": "0"},
        ) as host,
    ):
        host.wait_online(client, timeout=60, poll_interval=0.2)
        response = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle,
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
                "labels": {
                    "omnigent.wrapper": (
                        "claude-code-native-ui" if harness == "claude" else "codex-native-ui"
                    ),
                    "omnigent.ui": "terminal",
                },
            },
        )
        response.raise_for_status()
        session_id = response.json()["session_id"]
        try:
            page.add_init_script(
                f"localStorage.setItem('omnigent:imports-reviewed:{host.host_id}', 'true')"
            )
            page.goto(f"{live_server}/c/{session_id}")
            expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
            _ensure_chat_view(page)
            composer = page.get_by_label("Message the agent")
            command = f"{'/' if harness == 'claude' else '$'}{_SKILL}"
            composer.fill(command)
            page.get_by_role("button", name="Send", exact=True).click()
            reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
                has_text=_REPLY
            )
            expect(reply.first).to_be_visible(timeout=90_000)
            requests = httpx.get(f"{mock_llm_server_url}/mock/requests").json()
            assert _SKILL_MARKER in json.dumps(requests), "native harness did not load the skill"
            labels = {}
            for phase in ("live", "history"):
                if phase == "history":
                    page.reload()
                    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
                    _ensure_chat_view(page)
                    expect(reply.first).to_be_visible(timeout=30_000)
                user_content = page.get_by_test_id("user-message-content")
                expect(user_content).to_have_count(1)
                receipts = page.get_by_test_id("slash-command-card")
                expect(receipts).to_have_count(1 if harness == "claude" else 0)
                labels[phase] = {
                    "user": user_content.inner_text(),
                    "receipts": receipts.all_text_contents(),
                    "tooltips": receipts.evaluate_all("cards => cards.map(card => card.title)"),
                }
                page.screenshot(path=str(evidence / f"{harness}-{phase}.png"))
            expected = {
                "user": command,
                "receipts": [f"Skill {_SKILL}"] if harness == "claude" else [],
                "tooltips": [f"Skill {_SKILL}"] if harness == "claude" else [],
            }
            assert labels == {"live": expected, "history": expected}
        finally:
            try:
                page.screenshot(path=str(evidence / f"{harness}-skill.png"))
                (evidence / f"{harness}-session.json").write_text(
                    json.dumps(client.get(f"/v1/sessions/{session_id}").json(), indent=2)
                )
                (evidence / f"{harness}-items.json").write_text(
                    json.dumps(client.get(f"/v1/sessions/{session_id}/items").json(), indent=2)
                )
            except Exception as error:
                print(f"Could not capture skill diagnostics: {error!r}")
            finally:
                client.delete(f"/v1/sessions/{session_id}")
