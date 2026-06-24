"""Desktop setup-page connect flow (Electron shell).

The desktop shell's setup page (``ap-web/electron/setup/index.html``) is the
user-facing "connect to a server" screen. This exercises it in a real browser:
the scheme-defaulting this change added means a bare (or ``/omnigent``)
Databricks workspace URL now connects over https on the first click instead of
tripping the unencrypted-http warning that the old http:// default produced.

The setup page and the Electron main process share one module
(``ap-web/electron/src/url.js``), loaded here as ``window.omnigentUrl``, so the
same ``normalizeUrl`` the main process navigates with is also verified in the
browser — coverage the web-only harness cannot otherwise reach.

These tests drive only the static page plus that shared module; they do not need
the ``live_server`` omnigent backend.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

# Repo-root-relative path to the Electron setup page. Loading it via file://
# resolves the page's relative ``<script src="../src/url.js">`` against
# ap-web/electron/src/url.js, so window.omnigentUrl is the real shared module.
_SETUP_PAGE = Path(__file__).resolve().parents[3] / "ap-web" / "electron" / "setup" / "index.html"

# The setup page expects the Electron preload bridge (window.omnigentSetup),
# which is absent in a plain browser. Stub it: getServerUrl/getRecentServers
# feed page load, and setServerUrl records the value the page would hand the
# main process while resolving WITHOUT navigating, so the page stays put for
# assertions.
_PRELOAD_STUB = """
  window.__connectCalls = [];
  window.omnigentSetup = {
    getServerUrl: () => Promise.resolve(""),
    getRecentServers: () => Promise.resolve([]),
    setServerUrl: (value) => { window.__connectCalls.push(value); return Promise.resolve(); },
  };
"""

# With no saved URL, the page prefills the input with this default. Waiting for
# it to land keeps a later fill() from racing the async prefill.
_DEFAULT_PREFILL = "http://localhost:6767"


def _open_setup_page(page: Page) -> None:
    """Load the setup page with the preload bridge stubbed and prefill settled.

    :param page: Playwright page fixture.
    """
    page.add_init_script(_PRELOAD_STUB)
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


def test_shared_url_module_defaults_scheme_in_browser(page: Page) -> None:
    """The shared url.js (also used by the main process) defaults the scheme.

    The setup page loads ``ap-web/electron/src/url.js`` as
    ``window.omnigentUrl`` — the exact module the Electron main process uses to
    normalize the URL it navigates to. Exercising it here covers the
    main-process scheme logic the web-only e2e harness cannot otherwise reach.
    """
    _open_setup_page(page)

    # Remote host → https; the guide's /omnigent suffix is preserved.
    assert (
        page.evaluate(
            "() => window.omnigentUrl.normalizeUrl('dbc-x.cloud.databricks.com/omnigent')"
        )
        == "https://dbc-x.cloud.databricks.com/omnigent"
    )
    # Loopback stays http for local dev.
    assert (
        page.evaluate("() => window.omnigentUrl.normalizeUrl('localhost:6767')")
        == "http://localhost:6767/"
    )
