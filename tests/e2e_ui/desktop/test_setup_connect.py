"""Desktop setup-page connect flow (Electron shell).

The desktop shell's setup page (``web/electron/setup/index.html``) is the
user-facing "connect to a server" screen. This exercises its preload contract
in a real browser, including recoverable main-process URL rejection.

URL normalization belongs to the Electron main process. It is exercised at its
CommonJS module boundary rather than exposed to the setup renderer.

These tests drive only the static page plus that shared module; they do not need
the ``live_server`` omnigent backend.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from playwright.sync_api import Page, expect

_SETUP_PAGE = Path(__file__).resolve().parents[3] / "web" / "electron" / "setup" / "index.html"

# The setup page expects the Electron preload bridge (window.omnigentSetup),
# which is absent in a plain browser. Stub it: reads feed page load, while
# setServerUrl/copyText record native actions without navigating or touching
# the system clipboard.
_PRELOAD_STUB = """
  window.__connectCalls = [];
  window.__copiedTexts = [];
  window.omnigentSetup = {
    getServerUrl: () => Promise.resolve(""),
    getRecentServers: () => Promise.resolve(__RECENT_SERVERS__),
    setServerUrl: (value) => {
      window.__connectCalls.push(value);
      if (value === "http://example.databricks.com") {
        return Promise.resolve({
          loaded: false,
          error: "Remote servers require HTTPS. Update the server address and try again.",
        });
      }
      return Promise.resolve({ loaded: true });
    },
    copyText: (value) => { window.__copiedTexts.push(value); return Promise.resolve(); },
  };
"""

# With no saved URL, the page prefills the input with this default. Waiting for
# it to land keeps a later fill() from racing the async prefill.
_DEFAULT_PREFILL = "http://localhost:6767"


def _open_setup_page(page: Page, recent_servers: Sequence[str] = ()) -> None:
    """Load the setup page with the preload bridge stubbed and prefill settled.

    :param page: Playwright page fixture.
    """
    page.add_init_script(
        _PRELOAD_STUB.replace("__RECENT_SERVERS__", json.dumps(list(recent_servers)))
    )
    page.goto(_SETUP_PAGE.as_uri())
    # getServerUrl() populates the input asynchronously; wait for that so the
    # per-test fill() below overwrites a settled value rather than racing it.
    expect(page.locator("#url")).to_have_value(_DEFAULT_PREFILL)


def test_bare_workspace_url_connects_without_http_warning(page: Page) -> None:
    """A schemeless ``<ws>/omnigent`` connects on the first click, no warning.

    Before the scheme default, a schemeless remote host was treated as
    ``http://`` and tripped the unencrypted-remote warning, forcing a second
    click. It now defaults to https, so the first click connects directly.
    """
    _open_setup_page(page)

    page.fill("#url", "dbc-x.cloud.databricks.com/omnigent")
    page.click("#connect")

    page.wait_for_function("() => window.__connectCalls.length === 1")
    assert page.evaluate("() => window.__connectCalls") == ["dbc-x.cloud.databricks.com/omnigent"]
    expect(page.locator("#err")).to_have_text("")


def test_explicit_http_remote_surfaces_main_process_rejection(page: Page) -> None:
    """The setup renderer surfaces a recoverable ``setServerUrl`` rejection."""
    _open_setup_page(page)

    page.fill("#url", "http://example.databricks.com")
    page.click("#connect")

    page.wait_for_function("() => window.__connectCalls.length === 1")
    assert page.evaluate("() => window.__connectCalls") == ["http://example.databricks.com"]
    expect(page.locator("#err")).to_have_text(
        "Remote servers require HTTPS. Update the server address and try again."
    )
    expect(page.locator("#connect")).to_be_enabled()


def test_loopback_connects_over_http_without_warning(page: Page) -> None:
    """A bare loopback host stays http:// and connects without a warning.

    Loopback is the local-dev case the scheme default intentionally leaves on
    http; it must connect on the first click with no unencrypted-remote warning.
    """
    _open_setup_page(page)

    page.fill("#url", "localhost:6767")
    page.click("#connect")

    page.wait_for_function("() => window.__connectCalls.length === 1")
    assert page.evaluate("() => window.__connectCalls") == ["localhost:6767"]
    expect(page.locator("#err")).to_have_text("")


def test_recent_server_connect_and_copy_actions_are_independent(page: Page) -> None:
    """The URL connects immediately, while its clipboard icon only copies."""
    recent_url = "https://dbc-x.cloud.databricks.com/omnigent?o=12345678901234567890"
    _open_setup_page(page, [recent_url])

    recent = page.locator(".recent-btn")
    label = "dbc-x.cloud.databricks.com/?o=12345678901234567890"
    expect(recent).to_have_text(label)
    expect(recent).to_have_attribute("title", label)

    page.click(".recent-copy")
    page.wait_for_function("() => window.__copiedTexts.length === 1")
    assert page.evaluate("() => window.__copiedTexts") == [recent_url]
    assert page.evaluate("() => window.__connectCalls") == []
    expect(page.locator(".recent-copy")).to_have_attribute("title", "Copied")
    expect(page.locator(".recent-copy")).to_have_attribute("data-copied", "true")

    recent.click()
    page.wait_for_function("() => window.__connectCalls.length === 1")
    assert page.evaluate("() => window.__connectCalls") == [recent_url]


def test_shared_url_module_defaults_scheme_in_browser(page: Page) -> None:
    """The shared url.js (also used by the main process) defaults the scheme.

    The setup page loads ``web/electron/src/url.js`` as
    ``window.omnigentUrl`` — the exact module the Electron main process uses to
    normalize the URL it navigates to. Exercising it here covers the
    main-process scheme logic the web-only e2e harness cannot otherwise reach.
    """
    _open_setup_page(page)

    # Remote host → https root while retaining the Databricks organization;
    # the main process then probes and appends the canonical /omnigent mount.
    assert (
        page.evaluate(
            """() => window.omnigentUrl.normalizeUrl(
              'dbc-x.cloud.databricks.com/omnigent?ignored=yes&o=1965859176160743#page'
            )"""
        )
        == "https://dbc-x.cloud.databricks.com/?o=1965859176160743"
    )
    # Loopback stays http for local dev.
    assert (
        page.evaluate("() => window.omnigentUrl.normalizeUrl('localhost:6767')")
        == "http://localhost:6767/"
    )
