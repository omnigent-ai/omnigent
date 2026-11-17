"""The built SPA runs under the server's opt-in Content-Security-Policy.

The static lane serves the build without the server, so the document response
gets the exact header the server would send, built by the same function.
"""

from __future__ import annotations

from playwright.sync_api import ConsoleMessage, Page, Route, expect

from omnigent.server.data_controls import content_security_policy, inline_script_hashes
from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _mock_markdown_files,
    _post_message,
)
from tests.browser_ui.files.test_file_line_navigation import (
    seeded_session as seeded_session_fixture,
)

seeded_session = seeded_session_fixture

_REPLY = """Here is the query, the flow and the formula.

```python
def total(rows):
    return sum(row["amount"] for row in rows)
```

```mermaid
flowchart LR
  A[Load rows] --> B[Sum amounts]
```

$$
\\sum_{i=1}^{n} a_i
$$
"""
_FILE_PATH = "src/report.py"
_FILE = "def report(rows):\n    return len(rows)\n"
_HTML_PATH = "src/report.html"
_HTML = (
    "<!doctype html><html><head><title>Report</title></head><body>"
    '<a href="#totals">Jump to totals</a><div style="height:3000px"></div>'
    '<h2 id="totals">Totals</h2></body></html>'
)
_COLLECT_VIOLATIONS = """
window.__cspViolations = [];
document.addEventListener("securitypolicyviolation", (event) => {
  const where = `${event.sourceFile}:${event.lineNumber}:${event.columnNumber}`;
  window.__cspViolations.push(`${event.violatedDirective} ${event.blockedURI} ${where}`);
});
"""


def _serve_documents_with_policy(session: BrowserSession) -> list[str]:
    """Answer every page load with the server's enforced policy; return the policies sent."""
    sent: list[str] = []

    def add_policy(route: Route) -> None:
        if route.request.resource_type != "document":
            route.fallback()
            return
        response = route.fetch()
        policy = content_security_policy(inline_script_hashes(response.text()))
        sent.append(policy)
        route.fulfill(
            response=response,
            headers={**response.headers, "content-security-policy": policy},
        )

    session.contract.route("**/*", add_policy)
    return sent


def _violations(page: Page) -> list[str]:
    """Reports from the page and every frame it holds, such as an HTML preview."""
    reports: list[str] = []
    for frame in page.frames:
        reports.extend(frame.evaluate("() => window.__cspViolations ?? []"))
    return reports


def test_spa_core_pages_load_without_policy_violations(
    page: Page, seeded_session: BrowserSession
) -> None:
    """Chat Markdown (Shiki, Mermaid, KaTeX), Monaco and the home page render with no reports."""
    console_reports: list[str] = []

    def record(message: ConsoleMessage) -> None:
        if "Content Security Policy" in message.text:
            console_reports.append(message.text)

    page.on("console", record)
    page.add_init_script(_COLLECT_VIOLATIONS)
    sent = _serve_documents_with_policy(seeded_session)
    seeded_session.info["data_controls"] = {"content_security_policy": "enforce"}
    _post_message(seeded_session, _REPLY)
    _mock_markdown_files(page, seeded_session, {_FILE_PATH: _FILE, _HTML_PATH: _HTML})
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": 1600, "height": 1000})

    page.goto(f"{base_url}/c/{session_id}?file={_FILE_PATH}")

    assert sent, "the document was not served with the policy"
    # Shiki's WebAssembly highlighter colors the code block.
    code = page.locator('[data-streamdown="code-block"]').first
    expect(code.locator("span[style*='--sdm-c']").first).to_be_attached(timeout=30_000)
    expect(
        page.locator('[data-streamdown="mermaid-block"] svg[aria-roledescription]')
    ).to_be_visible(timeout=30_000)
    expect(page.locator(".katex").first).to_be_visible()
    editor = page.locator('[data-testid="file-viewer"]:visible .monaco-editor')
    expect(editor.locator(".view-line", has_text="def report")).to_be_visible(timeout=30_000)
    page.evaluate("() => document.fonts.ready")
    assert _violations(page) == []
    assert console_reports == []

    # The policy is live: a script injected inline is refused and reported.
    assert not page.evaluate(
        """() => {
          const script = document.createElement("script");
          script.textContent = "window.__inlineRan = true";
          document.head.append(script);
          return window.__inlineRan === true;
        }"""
    )
    assert any(report.startswith("script-src") for report in _violations(page))
    console_reports.clear()

    page.goto(f"{base_url}/")
    expect(page.get_by_role("textbox").first).to_be_visible(timeout=30_000)
    page.goto(f"{base_url}/c/{session_id}")
    expect(
        page.locator('[data-streamdown="mermaid-block"] svg[aria-roledescription]')
    ).to_be_visible(timeout=30_000)

    assert _violations(page) == []
    assert console_reports == []

    # An HTML preview inherits the policy: its comment bridge and link helper still run.
    page.goto(f"{base_url}/c/{session_id}?file={_HTML_PATH}")
    preview = page.frame_locator('iframe[title="HTML preview"]')
    expect(preview.get_by_role("link", name="Jump to totals")).to_be_visible(timeout=30_000)
    srcdoc = page.locator('iframe[title="HTML preview"]').get_attribute("srcdoc") or ""
    assert "htmlCommentBridgeRuntime" in srcdoc
    pages_before = len(page.context.pages)
    preview.get_by_role("link", name="Jump to totals").click()
    frame = next(f for f in page.frames if f.parent_frame is not None)
    expect(preview.get_by_role("heading", name="Totals")).to_be_in_viewport()
    assert frame.evaluate("() => location.hash") == "#totals"
    assert len(page.context.pages) == pages_before
    assert _violations(page) == []
    assert console_reports == []
