"""E2E: HTML artifact preview — scripts run, links open in a new tab, pop-out.

Regression coverage for two bugs in the HTML artifact preview
(``shell/CodeViewer.tsx`` + ``shell/FileViewer.tsx``):

  * #778 — JavaScript in a rendered HTML file did not run. The preview iframe
    used ``sandbox=""`` (the most restrictive setting), which blocks scripts.
  * #777 — links in a rendered HTML file did not open. The same empty sandbox
    blocked popups/navigation; the fix injects ``<base target="_blank">`` and
    relaxes the sandbox so links open a real new tab.

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
from playwright.sync_api import Page, expect

# The hello_world agent spec uses ``os_env.cwd: .``, so the runner writes
# seeded files into the server process's cwd — the repo root (this file is
# ``<repo>/tests/e2e_ui/files/...``, so the repo root is ``parents[3]``).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Must stay in sync with ``HTML_PREVIEW_SANDBOX`` in
# ``ap-web/src/shell/codeViewerHelpers.ts``. ``allow-scripts`` re-enables JS
# (#778); the popup flags let links escape into a real new tab (#777); we
# deliberately do NOT include ``allow-same-origin`` (would let untrusted
# artifact JS reach the parent app's origin).
_EXPECTED_SANDBOX = (
    "allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-modals"
)

_HTML_PATH = "preview_artifact.html"

# Self-contained fixture: a script flips a sentinel element from a "blocked"
# marker to a "ran" marker, and creates a link at runtime. No network needed.
_HTML_CONTENT = """\
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
    <script>
      // Proof that scripts run (#778).
      document.getElementById("js-status").textContent = "js-ran";
      // A link created at runtime — covered by the injected <base target>.
      var a = document.createElement("a");
      a.id = "dynamic-link";
      a.href = "https://example.com/dynamic";
      a.textContent = "dynamic link";
      document.getElementById("dynamic-link-host").appendChild(a);
    </script>
  </body>
</html>
"""


def _cleanup_session_workdir(session_id: str) -> None:
    shutil.rmtree(_REPO_ROOT / session_id, ignore_errors=True)


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


def test_html_preview_runs_scripts_and_targets_links(
    page: Page,
    seeded_html: tuple[str, str],
) -> None:
    """HTML preview runs JS (#778) and forces links to open in a new tab (#777)."""
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


def test_html_preview_open_in_new_tab_button(
    page: Page,
    seeded_html: tuple[str, str],
) -> None:
    """The "Open in new tab" button pops the artifact into an isolated, sandboxed tab.

    Security regression guard: the artifact must render inside a *sandboxed*
    (opaque-origin) iframe in an app-controlled blank tab — NOT at the app's own
    origin (which a ``blob:``/``data:`` URL would do, exposing the app's storage
    and credentialed API to untrusted artifact JS).
    """
    base_url, session_id = seeded_html
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(f"{base_url}/c/{session_id}?file={_HTML_PATH}")

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    expect(file_viewer.locator('iframe[title="HTML preview"]')).to_be_visible(timeout=10_000)

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

    popped.close()
