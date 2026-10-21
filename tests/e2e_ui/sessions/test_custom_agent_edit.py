"""Settings edits cross the real config/ETag/PATCH boundary without bundle replacement."""

from __future__ import annotations

import uuid

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests._helpers.session import bundle_files


@pytest.mark.parametrize("strip_if_match", [False, True], ids=["if-match", "no-if-match"])
@pytest.mark.parametrize("strip_etag", [False, True], ids=["etag", "no-etag"])
@pytest.mark.parametrize("mobile", [False, True], ids=["desktop", "mobile"])
def test_custom_agent_edit_preserves_fields_and_recovers_conflict(
    page: Page, live_server: str, mobile: bool, strip_etag: bool, strip_if_match: bool
) -> None:
    name = f"edit-{uuid.uuid4().hex[:8]}"
    bundle = bundle_files(
        {
            "config.yaml": (
                f"spec_version: 1\nname: {name}\ndescription: Original description\n"
                "instructions: notes.md\nexecutor:\n  config:\n    harness: claude-sdk\n"
                "tools:\n  docs:\n    type: mcp\n    url: https://example.com/mcp\n"
                "    headers:\n      Authorization: test-credential\n"
            ).encode(),
            "notes.md": b"Keep these instructions.\n",
            "files/asset.txt": b"Keep this asset.",
        }
    )

    # Only enable the release-gated UI; all agent reads and writes use the real server.
    def enable_editor(route: Route) -> None:
        response = route.fetch()
        info = response.json()
        info["features"] = {**info.get("features", {}), "custom_agents_settings_ui": True}
        route.fulfill(response=response, json=info)

    page.route("**/v1/info", enable_editor)
    page.set_viewport_size({"width": 390 if mobile else 1280, "height": 844 if mobile else 900})
    with httpx.Client(base_url=live_server, timeout=20) as api:
        installed = api.post("/v1/agents", files={"bundle": ("agent.tar.gz", bundle)})
        installed.raise_for_status()
        agent_id = installed.json()["id"]
        path = f"/v1/agents/{agent_id}"

        def strip_revision_headers(route: Route) -> None:
            request_headers = route.request.all_headers()
            if strip_if_match:
                request_headers.pop("if-match", None)
            response = route.fetch(headers=request_headers)
            response_headers = response.headers
            if strip_etag:
                response_headers.pop("etag", None)
            route.fulfill(response=response, headers=response_headers)

        if strip_etag or strip_if_match:
            page.route(f"**{path}{{,/config}}", strip_revision_headers)
        try:
            page.goto(f"{live_server}/settings/custom-agents" + ("" if mobile else f"/{agent_id}"))
            if mobile:
                page.get_by_role("link", name=name, exact=True).click()
            form = page.get_by_role("form", name="Agent configuration")
            expect(page.get_by_role("dialog")).to_have_count(0)
            expect(form.get_by_label("Description", exact=True)).to_have_value(
                "Original description"
            )
            expect(form.get_by_role("button", name="Save changes")).to_be_disabled()
            expect(form.get_by_text("Authorization", exact=True)).to_be_visible()
            expect(form.get_by_label("Action for Authorization")).to_have_value("keep")
            save_button = form.get_by_role("button", name="Save changes")
            save_button.scroll_into_view_if_needed()
            expect(save_button).to_be_in_viewport()
            assert form.evaluate("element => element.scrollWidth <= element.clientWidth")

            form.get_by_label("Description", exact=True).fill("Updated from Settings")
            with page.expect_request(
                lambda request: request.method == "PATCH" and request.url.endswith(path)
            ) as saving:
                form.get_by_role("button", name="Save changes").click()
            assert saving.value.post_data_json == {"description": "Updated from Settings"}
            if strip_etag:
                assert "if-match" not in saving.value.headers
            else:
                assert saving.value.headers["if-match"].startswith('"')
            expect(form.get_by_label("Description", exact=True)).to_have_value(
                "Updated from Settings"
            )
            expect(form.get_by_role("button", name="Cancel", exact=True)).to_be_enabled()
            expect(form.get_by_role("button", name="Save changes")).to_be_disabled()
            saved = api.get(f"{path}/config")
            saved.raise_for_status()
            assert saved.json()["id"] == agent_id
            assert saved.json()["instructions"] == "Keep these instructions.\n"
            assert saved.json()["mcp_servers"]["docs"]["configured_header_names"] == [
                "Authorization"
            ]

            expect(form.get_by_label("Description", exact=True)).to_have_value(
                "Updated from Settings"
            )
            form.get_by_label("Description", exact=True).fill("Unsaved local draft")
            competing = api.patch(
                path,
                headers={"If-Match": saved.headers["etag"]},
                json={"description": "Concurrent winner"},
            )
            competing.raise_for_status()
            form.get_by_role("button", name="Save changes").click()
            if strip_etag or strip_if_match:
                expect(form.get_by_role("button", name="Save changes")).to_be_disabled()
                expect(form.get_by_role("button", name="Cancel", exact=True)).to_be_enabled()
                latest = api.get(f"{path}/config")
                latest.raise_for_status()
                assert latest.json()["description"] == "Unsaved local draft"
                assert latest.json()["instructions"] == "Keep these instructions.\n"
            else:
                expect(form.get_by_role("alert")).to_contain_text("Your draft is still here")
                expect(form.get_by_label("Description", exact=True)).to_have_value(
                    "Unsaved local draft"
                )
                form.get_by_role("button", name="Reload latest").click()
                page.get_by_role("button", name="Discard changes").click()
                expect(form.get_by_label("Description", exact=True)).to_have_value(
                    "Concurrent winner"
                )
            form.get_by_role("button", name="Cancel", exact=True).click()
            expect(page.get_by_role("heading", name="Custom agents", exact=True)).to_be_visible()
        finally:
            api.delete(path)
