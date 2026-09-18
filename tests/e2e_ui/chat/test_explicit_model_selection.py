"""Web model picks persist explicit IDs, including the catalog's default row.

The real SPA and PATCH endpoint run against an isolated server. Only native
catalogs/model reports are simulated; this does not exercise native CLIs.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import (
    _install_stream_controller,
    _open_gear_model_dropdown,
    _push_sse,
)
from tests.e2e_ui.conftest import fetch_with_retry


@pytest.mark.parametrize(
    ("harness", "wrapper", "primary", "alternate"),
    [
        ("codex", "codex-native-ui", "gpt-5.6-sol", "gpt-6-astra"),
        ("claude", "claude-code-native-ui", "haiku", "sonnet"),
        ("pi", "pi-native-ui", "anthropic/claude-opus-5", "anthropic/claude-opus-4-7"),
    ],
    ids=["codex", "claude", "pi"],
)
def test_switch_back_to_default_marked_model_persists_explicit_id(
    page: Page,
    seeded_session: tuple[str, str],
    harness: str,
    wrapper: str,
    primary: str,
    alternate: str,
) -> None:
    """Start unpinned, select another model, return explicitly, then reload."""
    base_url, session_id = seeded_session
    session_path = f"/v1/sessions/{session_id}"
    patch_bodies: list[dict] = []
    reported_model = primary
    _install_stream_controller(page, session_id)
    page.route_web_socket("**/v1/sessions/updates*", lambda _: None)

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path:
            route.continue_()
            return
        if route.request.method == "PATCH":
            patch_bodies.append(route.request.post_data_json)
        response = fetch_with_retry(route)
        payload = response.json()
        payload.update(
            harness=harness,
            llm_model=reported_model,
            labels={**payload.get("labels", {}), "omnigent.wrapper": wrapper},
            model_options=[
                {"id": primary, "displayName": "Primary", "isDefault": True},
                {"id": alternate, "displayName": "Alternate"},
            ],
        )
        route.fulfill(response=response, json=payload)

    page.route(f"**{session_path}*", snapshot)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        _open_gear_model_dropdown(page)
        for model, name in ((alternate, "Alternate"), (primary, "Primary")):
            expect(page.get_by_role("menuitemcheckbox", name="Default", exact=True)).to_have_count(
                0
            )
            with page.expect_response(
                lambda response: (
                    response.request.method == "PATCH"
                    and urlparse(response.url).path == session_path
                    and response.status == 200
                )
            ):
                page.get_by_role("menuitemcheckbox", name=name, exact=True).click()
            assert patch_bodies[-1] == {"model_override": model}
            reported_model = model
            _push_sse(page, "session.model", {"conversation_id": session_id, "model": model})
            expect(page.get_by_test_id("composer-model-pending")).to_have_count(0)
            expect(page.get_by_test_id("composer-agent-config-value")).to_contain_text(name)

        persisted = httpx.get(f"{base_url}{session_path}", timeout=10)
        persisted.raise_for_status()
        assert persisted.json()["model_override"] == primary
        assert patch_bodies == [{"model_override": alternate}, {"model_override": primary}]

        page.reload()
        _open_gear_model_dropdown(page)
        expect(page.get_by_role("menuitemcheckbox", name="Primary", exact=True)).to_have_attribute(
            "aria-checked", "true"
        )
    finally:
        page.unroute_all(behavior="wait")
