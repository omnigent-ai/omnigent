"""Desktop setup-page connect flow (Electron shell).

The desktop shell's setup page (``web/electron/setup/index.html``) is the
user-facing "connect to a server" screen. This exercises it in a real browser:
the scheme-defaulting this change added means a bare (or ``/omnigent``)
Databricks workspace URL now connects over https on the first click instead of
tripping the unencrypted-http warning that the old http:// default produced.

The setup page and the Electron main process share one module
(``web/electron/src/url.js``), loaded here as ``window.omnigentUrl``, so the
same ``normalizeUrl`` the main process navigates with is also verified in the
browser — coverage the web-only harness cannot otherwise reach.

These tests drive only the static page plus that shared module; they do not need
the ``live_server`` omnigent backend.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from playwright.sync_api import Page, expect

# Repo-root-relative path to the Electron setup page. Loading it via file://
# resolves the page's relative ``<script src="../src/url.js">`` against
# web/electron/src/url.js, so window.omnigentUrl is the real shared module.
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
    getManagedServers: () => Promise.resolve(__MANAGED_SERVERS__),
    getRecentServers: () => Promise.resolve(__RECENT_SERVERS__),
    setServerUrl: (value) => { window.__connectCalls.push(value); return Promise.resolve(); },
    copyText: (value) => { window.__copiedTexts.push(value); return Promise.resolve(); },
  };
"""

# With no saved URL, the page prefills the input with this default. Waiting for
# it to land keeps a later fill() from racing the async prefill.
_DEFAULT_PREFILL = "http://localhost:6767"


def _open_setup_page(
    page: Page,
    recent_servers: Sequence[str] = (),
    managed_servers: Sequence[str] = (),
) -> None:
    """Load the setup page with the preload bridge stubbed and prefill settled.

    :param page: Playwright page fixture.
    """
    preload = _PRELOAD_STUB.replace(
        "__MANAGED_SERVERS__", json.dumps(list(managed_servers))
    ).replace("__RECENT_SERVERS__", json.dumps(list(recent_servers)))
    page.add_init_script(preload)
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


def test_explicit_http_remote_still_warns_then_proceeds(page: Page) -> None:
    """Explicit ``http://`` to a remote host still warns once, then proceeds.

    The security warning must survive the scheme-default change: a user who
    types ``http://`` to a remote host is warned on the first click and only
    connects when they click again.
    """
    _open_setup_page(page)

    page.fill("#url", "http://example.databricks.com")
    page.click("#connect")

    # First click: warned, not connected.
    expect(page.locator("#err")).to_contain_text("unencrypted")
    assert page.evaluate("() => window.__connectCalls") == []

    # Second click on the same value: proceeds past the warning.
    page.click("#connect")
    page.wait_for_function("() => window.__connectCalls.length === 1")
    assert page.evaluate("() => window.__connectCalls") == ["http://example.databricks.com"]


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


def test_managed_server_is_offered_without_auto_connecting(page: Page) -> None:
    """An MDM server is separate from recents and connects with its exact path."""
    managed_url = "https://mdm.example.com/ml/omnigents"
    recent_url = "https://recent.example.com/"
    _open_setup_page(page, [recent_url], [managed_url])

    managed = page.locator("#managed")
    expect(managed).to_be_visible()
    expect(managed.locator(".recents-title")).to_have_text("Provided by your organization")
    managed_button = page.locator("#managed-list .recent-btn")
    expect(managed_button).to_have_text("mdm.example.com")
    expect(managed_button).to_have_attribute("title", managed_url)

    # MDM offers a choice; it never connects without the user's click.
    assert page.evaluate("() => window.__connectCalls") == []
    expect(page.locator("#recents")).to_be_visible()
    expect(page.locator("#recents-list .recent-btn")).to_have_text("recent.example.com")

    managed_button.click()
    page.wait_for_function("() => window.__connectCalls.length === 1")
    assert page.evaluate("() => window.__connectCalls") == [managed_url]
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
