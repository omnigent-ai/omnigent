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
    page.evaluate(
        """async () => {
          const xhr = new Promise(resolve => {
            const request = new XMLHttpRequest();
            request.open("GET", "/v1/unregistered-xhr");
            request.onerror = resolve;
            request.send();
          });
          const events = new Promise(resolve => {
            const source = new EventSource("/v1/unregistered-events");
            source.onerror = () => { source.close(); resolve(); };
          });
          const socket = new Promise(resolve => {
            const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
            const ws = new WebSocket(`${wsProtocol}//${location.host}/v1/unregistered-websocket`);
            ws.onclose = resolve;
            ws.onerror = resolve;
          });
          await Promise.race([
            Promise.all([
              fetch("/v1/unregistered-fetch").catch(() => undefined),
              xhr,
              events,
              socket,
            ]),
            new Promise(resolve => setTimeout(resolve, 2000)),
          ]);
        }"""
    )

    violations = "\n".join(browser_contract.violations)
    assert "fetch" in violations and "/v1/unregistered-fetch" in violations
    assert "xhr" in violations and "/v1/unregistered-xhr" in violations
    assert "eventsource" in violations and "/v1/unregistered-events" in violations
    assert "WEBSOCKET" in violations and "/v1/unregistered-websocket" in violations
    browser_contract.violations.clear()
