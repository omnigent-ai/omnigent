"""Desktop setup-page recent-servers nicknames (Electron shell).

The setup page (``web/electron/setup/index.html``) lists recently-connected
servers and lets the user give each one an optional nickname, so a list of
near-identical URLs can be told apart. This exercises the rename flow in a real
browser: entries render their nickname (with the host trailing) or the bare
URL, the pencil opens an inline field, Enter saves through the shell bridge,
Escape cancels, and a blank value clears the nickname.

Like ``test_setup_connect``, these drive only the static page plus its shared
modules (``web/electron/src/url.js`` + ``src/recents.js``) with the Electron
preload bridge (``window.omnigentSetup``) stubbed — no ``live_server`` backend.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page, expect

_SETUP_PAGE = Path(__file__).resolve().parents[3] / "web" / "electron" / "setup" / "index.html"

# Stub the preload bridge. getRecentServers seeds a labelled + an unlabelled
# entry; setRecentServerLabel records the call and mutates an in-page store the
# way the main process mutates settings.json, returning the updated list so the
# page re-renders — mirroring the real IPC contract.
_PRELOAD_STUB = """
  window.__connectCalls = [];
  window.__labelCalls = [];
  window.__recents = [
    { url: "https://prod-abc.example.com/ml/omnigent", label: "Prod (west)" },
    { url: "http://localhost:6767", label: "" },
  ];
  window.omnigentSetup = {
    getServerUrl: () => Promise.resolve(""),
    getRecentServers: () => Promise.resolve(window.__recents.map((e) => ({ ...e }))),
    setServerUrl: (value) => { window.__connectCalls.push(value); return Promise.resolve(); },
    setRecentServerLabel: (url, label) => {
      window.__labelCalls.push([url, label]);
      const trimmed = (label || "").trim();
      window.__recents = window.__recents.map(
        (e) => (e.url === url ? { url: e.url, label: trimmed } : e),
      );
      return Promise.resolve(window.__recents.map((e) => ({ ...e })));
    },
  };
"""

_DEFAULT_PREFILL = "http://localhost:6767"


def _open_setup_page(page: Page) -> None:
    """Load the setup page with the preload bridge stubbed and prefill settled.

    :param page: Playwright page fixture.
    """
    page.add_init_script(_PRELOAD_STUB)
    page.goto(_SETUP_PAGE.as_uri())
    expect(page.locator("#url")).to_have_value(_DEFAULT_PREFILL)


def test_recents_render_nickname_and_bare_url(page: Page) -> None:
    """A labelled entry shows its nickname + host; an unlabelled one, its URL."""
    _open_setup_page(page)

    rows = page.locator(".recent-row")
    expect(rows).to_have_count(2)

    # Labelled entry: nickname leads, host trails.
    expect(rows.nth(0).locator(".rb-label")).to_have_text("Prod (west)")
    expect(rows.nth(0).locator(".rb-url")).to_have_text("prod-abc.example.com")

    # Unlabelled entry: the bare URL, no nickname span.
    expect(rows.nth(1).locator(".recent-btn")).to_have_text("http://localhost:6767")
    expect(rows.nth(1).locator(".rb-label")).to_have_count(0)


def test_rename_saves_nickname_and_rerenders(page: Page) -> None:
    """The pencil opens an inline field; Enter saves through the bridge."""
    _open_setup_page(page)

    # Rename the unlabelled localhost entry (row 2).
    page.locator(".recent-row").nth(1).locator(".recent-rename").click()
    field = page.locator(".recent-edit input")
    expect(field).to_be_focused()
    # A nickname is a display label, so any characters are allowed — only the
    # length is capped, to protect the stored file and the list layout.
    expect(field).to_have_attribute("maxlength", "60")
    field.fill("Local dev")
    field.press("Enter")

    # The bridge was called with the exact url + new nickname.
    page.wait_for_function("() => window.__labelCalls.length === 1")
    assert page.evaluate("() => window.__labelCalls") == [["http://localhost:6767", "Local dev"]]

    # The row now renders the nickname + host instead of the bare URL.
    row = page.locator(".recent-row").nth(1)
    expect(row.locator(".rb-label")).to_have_text("Local dev")
    expect(row.locator(".rb-url")).to_have_text("localhost:6767")


def test_rename_escape_cancels_without_saving(page: Page) -> None:
    """Escape leaves edit mode without calling the bridge or changing the row."""
    _open_setup_page(page)

    page.locator(".recent-row").nth(1).locator(".recent-rename").click()
    field = page.locator(".recent-edit input")
    field.fill("Discarded")
    field.press("Escape")

    expect(page.locator(".recent-edit")).to_have_count(0)
    assert page.evaluate("() => window.__labelCalls") == []
    expect(page.locator(".recent-row").nth(1).locator(".recent-btn")).to_have_text(
        "http://localhost:6767"
    )


def test_switching_rows_mid_edit_moves_edit_on_first_click(page: Page) -> None:
    """Clicking another row's pencil mid-edit switches editors on the first click.

    The open field cancels on blur; without preempting that blur, clicking a
    different row's pencil tears the pencil down before its click lands, so the
    first click is swallowed and edit mode never moves. This asserts the switch
    happens on a single click and discards the in-progress (unsaved) edit.
    """
    _open_setup_page(page)

    # Start editing the labelled entry (row 1) and type an unsaved value.
    page.locator(".recent-row").nth(0).locator(".recent-rename").click()
    field = page.locator(".recent-edit input")
    expect(field).to_be_focused()
    field.fill("Unsaved edit")

    # One click on row 2's pencil must move edit mode there.
    page.locator(".recent-row").nth(1).locator(".recent-rename").click()

    # Exactly one editor, on row 2, seeded from its stored (blank) label — the
    # unsaved row-1 text did not leak across, and nothing was persisted.
    expect(page.locator(".recent-edit")).to_have_count(1)
    moved = page.locator(".recent-row").nth(1).locator(".recent-edit input")
    expect(moved).to_be_focused()
    expect(moved).to_have_value("")
    assert page.evaluate("() => window.__labelCalls") == []

    # Row 1 reverted to its unchanged nickname.
    expect(page.locator(".recent-row").nth(0).locator(".rb-label")).to_have_text("Prod (west)")


def test_blank_value_clears_the_nickname(page: Page) -> None:
    """Saving a blank value clears the nickname, reverting to the bare URL."""
    _open_setup_page(page)

    # Clear the nickname on the labelled entry (row 1).
    page.locator(".recent-row").nth(0).locator(".recent-rename").click()
    field = page.locator(".recent-edit input")
    field.fill("")
    field.press("Enter")

    page.wait_for_function("() => window.__labelCalls.length === 1")
    assert page.evaluate("() => window.__labelCalls") == [
        ["https://prod-abc.example.com/ml/omnigent", ""]
    ]

    row = page.locator(".recent-row").nth(0)
    expect(row.locator(".rb-label")).to_have_count(0)
    expect(row.locator(".recent-btn")).to_have_text("https://prod-abc.example.com/ml/omnigent")
