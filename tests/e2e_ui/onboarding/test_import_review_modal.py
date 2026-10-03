"""E2E: the import modal opens only for the host desktop onboarding set up.

``/v1/hosts``, host-scoped ``/v1/skills``, and the host MCP inventory are
stubbed so the test controls exactly what the host's harnesses report while
the rest of the real UI runs against the live e2e server. ``/v1/info`` is
patched to toggle the default-off ``import_review`` release feature, and a
minimal ``window.omnigentDesktop`` stub stands in for the shell's one-time
onboarding handoff.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Route, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests.e2e_ui.start_session.helpers import stub_empty_host_picker_data

_HOST_ID = "host_import_e2e"


_HOSTS = {
    "hosts": [
        {
            "host_id": _HOST_ID,
            "name": "import-e2e-host",
            "owner": "e2e",
            "status": "online",
            "configured_harnesses": {"claude-native": True, "codex-native": "needs-auth"},
            "gateway_inference": {"claude-native": True},
        }
    ]
}

_SKILLS = {
    "claude-native": ["review", "toolkit:lint", "toolkit:ship"],
    "codex-native": ["fix-ci"],
}

_MCP_SERVERS = {
    "mcp_servers": [
        {"name": "github", "harness": "claude", "transport": "stdio", "scope": "user"},
        {
            "name": "linear",
            "harness": "claude",
            "transport": "http",
            "scope": "user",
            "url_host": "mcp.linear.app",
        },
        {"name": "docs", "harness": "codex", "transport": "stdio", "scope": "user"},
    ]
}


async def _set_import_review(page: Page, *, enabled: bool) -> None:
    """Patch the live ``/v1/info`` so ``import_review`` has a fixed value."""

    async def handle_info(route: Route) -> None:
        response = await route.fetch()
        info = await response.json()
        info["features"] = {**info.get("features", {}), "import_review": enabled}
        await route.fulfill(status=response.status, json=info)

    await page.route("**/v1/info", handle_info)


async def _register_routes(page: Page) -> None:
    async def handle_hosts(route: Route) -> None:
        await route.fulfill(json=_HOSTS)

    async def handle_skills(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        if query.get("host_id") != [_HOST_ID]:
            await route.fallback()
            return
        names = _SKILLS.get(query.get("harness", [""])[0], [])
        await route.fulfill(json={"skills": [{"name": n, "description": ""} for n in names]})

    async def handle_mcp_servers(route: Route) -> None:
        await route.fulfill(json=_MCP_SERVERS)

    await _set_import_review(page, enabled=True)
    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/skills?*", handle_skills)
    await page.route(f"**/v1/hosts/{_HOST_ID}/mcp-servers", handle_mcp_servers)
    await stub_empty_host_picker_data(page, _HOST_ID)


# Like the shell, hand over the onboarding runner once; later loads get null.
_ONBOARDING_HANDOFF = """
window.omnigentDesktop = {
  kind: "electron",
  takeOnboardingRunner: async () => {
    if (sessionStorage.getItem("e2e-onboarding-taken")) return null;
    sessionStorage.setItem("e2e-onboarding-taken", "1");
    return "remote";
  },
  getHostIdentity: async () => ({ cliInstalled: true, hostId: null }),
};
"""

_TITLE = "Your imports are ready"


def test_import_modal_opens_after_onboarding(live_server: str) -> None:
    """Onboarding's host shows its imports once, then reopens from Settings."""
    _run_in_fresh_loop(_drive(live_server))


async def _drive(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await page.add_init_script(_ONBOARDING_HANDOFF)
            await _register_routes(page)
            await page.goto(f"{base_url}/")

            dialog = page.get_by_role("dialog", name=_TITLE)
            await expect(dialog).to_be_visible(timeout=30_000)
            await expect(dialog).to_contain_text("These carry over automatically.")
            await expect(dialog).to_contain_text("Databricks AI Gateway")
            # Review only: nothing to select.
            await expect(dialog.get_by_role("checkbox")).to_have_count(0)

            assets = dialog.get_by_role("tablist", name="Asset type")
            await expect(assets.get_by_role("tab")).to_have_text(
                ["MCPs 2", "Skills 1", "Plugins 1"]
            )
            await expect(dialog.get_by_role("list", name="MCPs")).to_contain_text("mcp.linear.app")
            await assets.get_by_role("tab", name="Plugins").click()
            await expect(dialog.get_by_role("list", name="Plugins")).to_contain_text("toolkit")
            await expect(dialog.get_by_role("list", name="Plugins")).to_contain_text("2 skills")

            await dialog.get_by_role("tab", name="Codex").click()
            await expect(assets.get_by_role("tab")).to_have_text(["MCPs 1", "Skills 1"])
            await assets.get_by_role("tab", name="Skills").click()
            await expect(dialog.get_by_role("list", name="Skills")).to_contain_text("fix-ci")
            await expect(dialog).not_to_contain_text("Databricks AI Gateway")

            await dialog.get_by_role("button", name="Confirm").click()
            await expect(dialog).to_be_hidden()
            reviewed = await page.evaluate(
                f"window.localStorage.getItem('omnigent:imports-reviewed:{_HOST_ID}')"
            )
            assert reviewed is not None

            # The handoff is spent, so a reload opens nothing.
            await page.reload()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await page.wait_for_timeout(1_000)
            await expect(dialog).to_be_hidden()

            await page.goto(f"{base_url}/settings/import")
            await page.get_by_role("button", name="Review imports on import-e2e-host").click()
            await expect(dialog).to_be_visible()
            await expect(dialog.get_by_role("list", name="MCPs")).to_contain_text("github")
            await dialog.get_by_role("button", name="Close").click()
            await expect(dialog).to_be_hidden()
        finally:
            await browser.close()


def test_import_modal_never_opens_without_onboarding(live_server: str) -> None:
    """An unreviewed online host with imports doesn't open the modal on its own."""
    _run_in_fresh_loop(_drive_no_handoff(live_server))


async def _drive_no_handoff(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(page)
            async with page.expect_response(lambda r: r.url.endswith("/v1/hosts")):
                await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # Give the gate a moment to act on the hosts it has.
            await page.wait_for_timeout(1_000)
            await expect(page.get_by_role("dialog", name=_TITLE)).to_be_hidden()
            reviewed = await page.evaluate(
                f"window.localStorage.getItem('omnigent:imports-reviewed:{_HOST_ID}')"
            )
            assert reviewed is None
        finally:
            await browser.close()


def test_import_review_is_hidden_while_the_feature_is_off(live_server: str) -> None:
    """With ``import_review`` off, neither onboarding nor Settings shows the review."""
    _run_in_fresh_loop(_drive_feature_off(live_server))


async def _drive_feature_off(base_url: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await page.add_init_script(_ONBOARDING_HANDOFF)
            await _register_routes(page)
            await _set_import_review(page, enabled=False)
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await page.wait_for_timeout(1_000)
            await expect(page.get_by_role("dialog", name=_TITLE)).to_be_hidden()

            await page.goto(f"{base_url}/settings/import")
            await expect(page.get_by_text("Import from a machine")).to_be_visible()
            await expect(page.get_by_text("Harness imports")).to_have_count(0)
        finally:
            await browser.close()
