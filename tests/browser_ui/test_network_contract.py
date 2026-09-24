"""Self-tests for the browser lane's default-deny network boundary."""

from __future__ import annotations

from playwright.sync_api import Page

from tests.browser_ui.conftest import BrowserContract


def test_unregistered_same_origin_backends_are_blocked(
    page: Page,
    browser_contract: BrowserContract,
) -> None:
    page.goto(browser_contract.base_url)
    page.wait_for_timeout(2000)
    browser_contract.violations.clear()
    settled = page.evaluate(
        """async () => {
          const xhr = new Promise(resolve => {
            const request = new XMLHttpRequest();
            request.open("GET", "/v1/unregistered-xhr");
            request.onerror = () => resolve("error");
            request.send();
          });
          const events = new Promise(resolve => {
            const source = new EventSource("/v1/unregistered-events");
            source.onerror = () => { source.close(); resolve(); };
          });
          const socket = new Promise(resolve => {
            const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
            const ws = new WebSocket(`${wsProtocol}//${location.host}/v1/unregistered-websocket`);
            ws.onclose = event => resolve(event.code);
          });
          return await Promise.race([
            Promise.all([
              fetch("/v1/unregistered-fetch").catch(() => undefined),
              xhr,
              events,
              socket,
            ]),
            new Promise(resolve => setTimeout(() => resolve("timeout"), 5000)),
          ]);
        }"""
    )

    assert settled != "timeout", "a rejected dependency never settled in the page"
    assert settled[3] == 1008, "rejected WebSocket was left open instead of closed"

    violations = "\n".join(browser_contract.violations)
    assert "fetch" in violations and "/v1/unregistered-fetch" in violations
    assert "xhr" in violations and "/v1/unregistered-xhr" in violations
    assert "eventsource" in violations and "/v1/unregistered-events" in violations
    assert "WEBSOCKET" in violations and "/v1/unregistered-websocket" in violations
    browser_contract.violations.clear()


def test_string_mocks_match_only_the_exact_same_origin_path(
    page: Page,
    browser_contract: BrowserContract,
) -> None:
    page.goto(browser_contract.base_url)
    page.wait_for_timeout(2000)
    browser_contract.violations.clear()
    browser_contract.json("/v1/contract-probe", {"ok": True})

    statuses = page.evaluate(
        """async () => {
          const status = path => fetch(path).then(r => r.status, () => "blocked");
          return [
            await status("/v1/contract-probe?limit=1"),
            await status("/unexpected/v1/contract-probe"),
          ];
        }"""
    )

    assert statuses == [200, "blocked"]
    assert [v for v in browser_contract.violations if "contract-probe" in v] == [
        f"GET fetch {browser_contract.base_url}/unexpected/v1/contract-probe"
    ]
    browser_contract.violations.clear()
