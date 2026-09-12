"""A loaded mobile SPA offers an explicit reload after a confirmed deploy."""

from __future__ import annotations

from playwright.sync_api import Page, Route, expect


def test_web_update_reload_and_later(page: Page, live_server: str) -> None:
    """Drive the built app, controlling only deploy responses and the native bridge."""
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script("""
        window.__webUpdateNotifications = [];
        window.omnigentNative = {
            kind: 'android',
            notify: async (params) => {
                window.__webUpdateNotifications.push(params);
                return true;
            },
            onNotificationActivated: (callback) => {
                window.__tapNotification = callback;
                return () => {};
            },
        };
    """)
    build_id = "loaded"
    reads = 0

    def version(route: Route) -> None:
        nonlocal reads
        reads += 1
        route.fulfill(json={"version": "0.14.0.dev0", "webapp_build_id": build_id})

    page.route("**/api/version", version)
    page.goto(live_server)
    expect(page.get_by_test_id("sidebar-wordmark")).to_be_attached()
    page.wait_for_function("document.readyState === 'complete'")

    def poll(event: str = "online") -> None:
        with page.expect_response("**/api/version"):
            page.evaluate(
                """event => {
                    const target = event === 'visibilitychange' ? document : window;
                    target.dispatchEvent(new Event(event));
                }""",
                event,
            )
        # A following task lets the fetch continuation consume the response.
        page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")

    poll()
    banner = page.get_by_role("status", name="Web app update")
    expect(banner).not_to_be_visible()
    build_id = "deployed"
    poll("visibilitychange")
    expect(banner).not_to_be_visible()
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    # The follow-up confirms the deploy without another foreground/network event.
    expect(banner).to_be_visible()
    assert page.evaluate("window.__webUpdateNotifications") == [
        {
            "title": "Update available",
            "body": "Reload Omnigent to use the latest web app.",
            "navigatePath": "/",
        }
    ]
    page.evaluate("window.__tapNotification(window.__webUpdateNotifications[0].navigatePath)")
    expect(banner).to_be_visible()
    assert page.evaluate("window.__webUpdateNotifications.length") == 1
    page.get_by_role("button", name="Later", exact=True).click()
    expect(banner).not_to_be_visible()
    poll()
    poll()
    expect(banner).not_to_be_visible()
    assert page.evaluate("window.__webUpdateNotifications.length") == 1

    build_id = "deployed-again"
    poll()
    expect(banner).to_be_visible()
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.get_by_role("button", name="Reload", exact=True).click()
    poll()
    expect(banner).not_to_be_visible()
    assert page.evaluate("window.__webUpdateNotifications") == []
    assert reads >= 9
