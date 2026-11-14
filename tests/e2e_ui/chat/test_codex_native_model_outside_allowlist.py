"""E2E: a Codex session pinned outside codex-native's model list must not fail opaquely."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.config import global_config_path
from tests._helpers.live_server import terminate_process
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ALLOWED_MODEL = "system.ai.gpt-5-6-sol"
_PINNED_MODEL = "system.ai.gpt-6-astra"
_OUTCOME_TIMEOUT_S = 180.0
_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_HEADLINE = '[data-testid="error-headline"]'

_client = httpx.Client(trust_env=False)


@contextlib.contextmanager
def _codex_native_model_allowlist(mock_llm_server_url: str) -> Iterator[None]:
    """Bind codex-native to the mock provider with only ``_ALLOWED_MODEL`` allowed."""
    with _temp_omnigent_mock_config(mock_llm_server_url, "codex"):
        config_path = global_config_path()
        config = yaml.safe_load(config_path.read_text())
        config["inference"] = {
            "harnesses": {
                "codex-native": {
                    "provider": next(iter(config["providers"])),
                    "default_model": _ALLOWED_MODEL,
                    "model_allowlist": [_ALLOWED_MODEL],
                }
            }
        }
        config_path.write_text(yaml.safe_dump(config))
        yield


def _launch_error(base_url: str, session_id: str) -> str | None:
    response = _client.get(f"{base_url}/v1/sessions/{session_id}", timeout=5.0)
    if response.status_code != 200:
        return None
    error = response.json().get("last_task_error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    return None


@pytest.mark.timeout(600)
def test_codex_native_pin_outside_model_allowlist_names_allowed_models(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The chat's startup error names the rejected pin and codex-native's allowed models."""
    if _server_state.get("workflow_owned") or request.config.getoption("--ui-base-url"):
        pytest.skip("requires a spawned server whose runner config this test controls")
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        with _codex_native_model_allowlist(mock_llm_server_url):
            session_id = str(
                create_native_session(
                    _client,
                    live_server,
                    harness="codex",
                    model=_PINNED_MODEL,
                    metadata={"workspace": str(_REPO_ROOT)},
                )["session_id"]
            )
            try:
                page: Page = request.getfixturevalue("page")
                page.goto(f"{live_server}/c/{session_id}")
                expect(page.get_by_label("Message the agent")).to_be_visible(timeout=60_000)
                _bind_session_runner(live_server, session_id, runner_id)

                poll_until(
                    lambda: _launch_error(live_server, session_id) is not None,
                    timeout_s=_OUTCOME_TIMEOUT_S,
                    interval_s=2.0,
                )

                pill = page.locator(_ERROR_PILL).first
                expect(pill).to_be_visible(timeout=30_000)
                page.locator(_ERROR_HEADLINE).first.click()
                expect(pill).to_contain_text(
                    f"Model '{_PINNED_MODEL}' is not in the configured model list for "
                    f"'codex-native'. Allowed models: '{_ALLOWED_MODEL}'.",
                    timeout=10_000,
                )
                expect(pill).not_to_contain_text("see the runner log")
            finally:
                with contextlib.suppress(httpx.HTTPError):
                    _client.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        terminate_process(respawned)
