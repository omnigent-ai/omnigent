"""E2E: HTML artifact preview — scripts run, links open in a new tab, pop-out.

Regression coverage for two bugs in the HTML artifact preview
(``shell/CodeViewer.tsx`` + ``shell/FileViewer.tsx``):

  * #778 — JavaScript in a rendered HTML file did not run. The preview iframe
    used ``sandbox=""`` (the most restrictive setting), which blocks scripts.
  * #777 — links in a rendered HTML file did not open. The same empty sandbox
    blocked popups/navigation; the fix injects ``<base target="_blank">`` and
    relaxes the sandbox so links open a real new tab.
  * Same-page ``#fragment`` links escaped to a new window at the host page's
    URL (a ``srcdoc`` frame resolves fragment-only links against its embedder,
    and the base target sent them out); they must scroll the preview in place,
    while a handler the artifact registers on ``window`` afterwards must still
    be able to cancel such a click.

It also covers the new "Open in new tab" toolbar button, which pops the
artifact into a blank, app-controlled tab and renders it inside the same
sandboxed (opaque-origin) iframe — full-window viewing without giving the
artifact the app's own origin.

The file is seeded via the filesystem PUT endpoint (no agent run), so the test
is deterministic, and the fixture's JavaScript is self-contained (no network),
so the "did JS run" assertions never depend on external connectivity.

Playwright drives the browser via CDP and is not bound by the same-origin
policy, so it can read into the sandboxed ``srcdoc`` iframe (opaque origin) to
prove the script actually executed.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

# The hello_world agent spec uses ``os_env.cwd: .``, so the runner writes
# seeded files into the server process's cwd — the repo root (this file is
# ``<repo>/tests/e2e_ui/files/...``, so the repo root is ``parents[3]``).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Must stay in sync with ``HTML_PREVIEW_SANDBOX`` in
# ``web/src/shell/codeViewerHelpers.ts``. ``allow-scripts`` re-enables JS
# (#778); the popup flags let links escape into a real new tab (#777); we
# deliberately do NOT include ``allow-same-origin`` (would let untrusted
# artifact JS reach the parent app's origin).
_EXPECTED_SANDBOX = (
    "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-modals"
)

_HTML_PATH = "preview_artifact.html"

# Tall filler so the same-page anchor target starts well below the preview fold.
_FILLER = "\n".join(
    f"    <p>Filler paragraph {i} providing vertical space.</p>" for i in range(60)
)

# Self-contained fixture: a script flips a sentinel element from a "blocked"
# marker to a "ran" marker, and creates a link at runtime. No network needed.
_HTML_CONTENT = f"""\
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Preview fixture</title>
  </head>
  <body>
    <h1>HTML preview fixture</h1>
    <p id="js-status">js-blocked</p>
    <a id="static-link" href="https://example.com/static">static link</a>
    <p id="dynamic-link-host"></p>
    <a id="toc-link" href="#section-3">Jump to section 3</a>
    <a id="guarded-link" href="#section-3">Guarded jump</a>
    <a id="relative-link" href="other.html">relative link</a>
    <script>
      // An artifact handler registered on window after the preview's own script must still
      // be able to cancel a same-page link, here through the legacy return-false form.
      window.onclick = function (event) {{
        return !(event.target && event.target.id === "guarded-link");
      }};
      // Proof that scripts run (#778).
      document.getElementById("js-status").textContent = "js-ran";
      // A link created at runtime — covered by the injected <base target>.
      var a = document.createElement("a");
      a.id = "dynamic-link";
      a.href = "https://example.com/dynamic";
      a.textContent = "dynamic link";
      document.getElementById("dynamic-link-host").appendChild(a);
    </script>
{_FILLER}
    <h2 id="section-3">Section 3</h2>
    <p>Target of the in-page link.</p>
  </body>
</html>
"""


def _cleanup_session_workdir(session_id: str) -> None:
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


def _click_without_popup(
    page: Page, link: Locator, target: Locator, *, scrolls: bool = True
) -> None:
    """Click ``link`` with no new page opening; ``target`` scrolls into view, or stays put."""
    opened: list[Page] = []
    pages_before = len(page.context.pages)

    def note_popup(popup: Page) -> None:
        opened.append(popup)

    page.context.on("page", note_popup)
    try:
        link.click()
        if scrolls:
            expect(target).to_be_in_viewport()
        # The click itself would have created a popup; a brief settle catches a late event.
        page.wait_for_timeout(500)
        if not scrolls:
            expect(target).not_to_be_in_viewport()
    finally:
        page.context.remove_listener("page", note_popup)
    assert len(page.context.pages) == pages_before
    assert not opened, "same-page anchor opened a new window at " + ", ".join(
        p.url for p in opened
    )


@pytest.fixture
def seeded_html(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed the HTML artifact and yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_HTML_PATH}",
        json={"content": _HTML_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    try:
        yield (base_url, session_id)
    finally:
        _cleanup_session_workdir(session_id)


def test_html_preview_links_and_sandboxed_popout(
    page: Page,
    seeded_html: tuple[str, str],
) -> None:
    """Preview links work inline and in an isolated, sandboxed pop-out."""
    base_url, session_id = seeded_html
    # Keep the viewport wide so the responsive toolbar renders its actions
    # inline (the "Open in new tab" button is found by role, not via overflow).
    page.set_viewport_size({"width": 1600, "height": 900})
    # HTML files default to the preview view, so the iframe mounts directly.
    page.goto(f"{base_url}/c/{session_id}?file={_HTML_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()

    iframe_el = file_viewer.locator('iframe[title="HTML preview"]')
    expect(iframe_el).to_be_visible(timeout=10_000)

    # The sandbox must re-enable scripts + popups but NOT same-origin.
    expect(iframe_el).to_have_attribute("sandbox", _EXPECTED_SANDBOX)
    assert "allow-same-origin" not in _EXPECTED_SANDBOX  # guards the constant above

    # The fix injects <base target="_blank"> so every link (#777), including
    # ones built at runtime, opens in a new tab.
    srcdoc = iframe_el.get_attribute("srcdoc")
    assert srcdoc is not None
    assert '<base target="_blank">' in srcdoc

    # #778: the script ran — the sentinel flipped from "js-blocked" to "js-ran".
    # Playwright reaches into the sandboxed (opaque-origin) srcdoc frame via CDP.
    preview = file_viewer.frame_locator('iframe[title="HTML preview"]')
    expect(preview.locator("#js-status")).to_have_text("js-ran", timeout=10_000)
    # The runtime-created link is present, confirming the script fully executed.
    expect(preview.locator("#dynamic-link")).to_have_text("dynamic link")

    # A same-page ``#fragment`` link must scroll the preview: a srcdoc frame otherwise
    # resolves it against the host page's URL and the ``_blank`` base target opens it
    # externally.
    target = preview.locator("#section-3")
    expect(target).not_to_be_in_viewport()
    # The artifact's own ``window.onclick`` (registered after the preview's script) cancels the
    # guarded link, so nothing scrolls or opens: artifact handlers keep precedence.
    _click_without_popup(
        page, preview.get_by_role("link", name="Guarded jump"), target, scrolls=False
    )
    _click_without_popup(page, preview.get_by_role("link", name="Jump to section 3"), target)
    expect(page).to_have_url(f"{base_url}/c/{session_id}?file={_HTML_PATH}")

    # Every other link still opens in a new tab (#777), also after a same-page activation:
    # the handler touches neither the links nor the injected base. A relative one resolves
    # against the embedder, as before.
    with page.context.expect_page() as popup_info:
        preview.get_by_role("link", name="relative link").click()
    popup = popup_info.value
    expect(popup).to_have_url(f"{base_url}/c/other.html")
    popup.close()

    open_btn = file_viewer.get_by_role("button", name="Open in new tab")
    expect(open_btn).to_be_visible()

    # The button opens a blank, app-controlled tab and injects a sandboxed
    # iframe. Capture the new page on the context.
    with page.context.expect_page() as new_page_info:
        open_btn.click()
    popped = new_page_info.value
    popped.wait_for_load_state("domcontentloaded")

    # The shell tab itself is a blank, app-controlled document — the artifact is
    # never the top-level page (which would put it at the app's origin).
    assert popped.url == "about:blank"

    # The artifact lives only inside a sandboxed iframe whose sandbox matches the
    # in-app preview and, critically, withholds ``allow-same-origin``.
    shell_iframe = popped.locator("iframe")
    expect(shell_iframe).to_be_visible(timeout=10_000)
    sandbox = shell_iframe.get_attribute("sandbox")
    assert sandbox == _EXPECTED_SANDBOX
    assert "allow-same-origin" not in (sandbox or "")

    # Scripts still run inside the popped iframe (#778 holds in the pop-out too).
    preview = popped.frame_locator("iframe")
    expect(preview.locator("#js-status")).to_have_text("js-ran", timeout=10_000)

    # Isolation proof: the iframe has an opaque origin and cannot reach the
    # parent document — so artifact JS can't touch the app's storage/cookies/API.
    child_frame = shell_iframe.element_handle().content_frame()
    assert child_frame is not None
    assert child_frame.evaluate("() => window.origin") == "null"
    parent_access_blocked = child_frame.evaluate(
        """() => {
            try {
                void window.parent.document.cookie;
                return false;
            } catch (e) {
                return true;
            }
        }"""
    )
    assert parent_access_blocked

    # Same-page links stay inside the popped tab's frame too (its host page is
    # ``about:blank``, so the fragment would otherwise resolve there).
    target = preview.locator("#section-3")
    expect(target).not_to_be_in_viewport()
    _click_without_popup(page, preview.get_by_role("link", name="Jump to section 3"), target)
    assert popped.url == "about:blank"

    popped.close()
