"""Browser e2e: a manually typed working directory must enable "Clone & start".

In the clone/fork dialog, manually typing the working directory as a
home-relative path (``~/git/omnigent``) never enabled the "Clone & start"
button — the user had to open the tree browser and click "Select" before the
form accepted the very directory they already typed.

Root cause: ``ForkSessionForm`` validated the raw input with
``isValidWorkspace``, which accepts only fully-absolute paths (``/…``) —
tilde paths are rejected because the server never expands ``~``.  Nothing
expanded the typed ``~`` client-side, so the form sat on an "invalid" tilde
value with the submit greyed out and no hint why.

The fix resolves a typed ``~``-path against the host's home (derived from the
host's home listing) so it becomes the absolute path the server needs —
directly, without the user opening the browser at all.

Test shape
----------
No real ``omnigent host`` is needed — the host-side wire is stubbed at the
network layer (same pattern as ``test_fork_deleted_worktree_recreate``):

- ``GET /v1/hosts`` → one online host.
- ``GET /v1/sessions/{id}`` → patched with host + workspace so the dialog
  treats the source as a coding session on that host.
- ``GET /v1/hosts/{id}/filesystem[/**]`` → 200 listings; ``~``-relative
  paths are expanded against a fake home, mirroring the host's behavior.  The
  home listing is what lets the dialog resolve ``~`` to its absolute form.

The test opens the fork dialog, confirms the prefilled absolute directory
enables the submit (sanity that the host/dir geometry is right), then types
``~/git/omnigent`` into the working-directory field.  It asserts the submit
button becomes enabled from the typing alone — no Enter, no tree browser.  On
the buggy build that assertion fails: the button stays disabled until the
browser's "Select" button is clicked.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry

# Unique marker so other tests' transcripts can't satisfy this test's
# content assertions.
_MARKER = "quince-typed-workspace-marker"

# Fake host geometry: the source session appears bound to this host in a
# plain working directory (no worktree), so the dialog prefills that
# directory and the typed-path journey starts from a valid, enabled form.
_HOST_ID = "host_e2e_typed_workspace"
_HOME = "/home/e2euser"
_SRC_DIR = f"{_HOME}/work"
# What the user manually types — the reported "~/git/omnigent" journey.
_TYPED = "~/git/omnigent"


def test_typed_tilde_workspace_enables_clone(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Typing ``~/git/omnigent`` must enable "Clone & start".

    The failure mode this catches: the form keeps the raw ``~/…`` text as its
    workspace value, ``isValidWorkspace`` rejects it, and the submit stays
    greyed out even though the typed directory is real and listable.  The
    user-facing contract is that typing a real directory — resolved against
    the host's home — leaves the form submittable without opening the browser.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    :param mock_llm_server_url: Session-scoped mock LLM server URL; used to
        script the seed turn so no real credentials are needed.
    """
    base_url, session_id = seeded_session

    # ── Network stubs ──────────────────────────────────────────────

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": "e2e-typed-workspace-host",
                            "owner": "e2e",
                            "status": "online",
                            "configured_harnesses": {},
                        }
                    ]
                }
            ),
        )

    def handle_session_detail(route: Route) -> None:
        # Patch the real session so it reads as a coding session bound to
        # the fake host.  Non-GET traffic (e.g. PATCH) passes through
        # untouched.
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        body = response.json()
        body["host_id"] = _HOST_ID
        body["workspace"] = _SRC_DIR
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle_filesystem(route: Route) -> None:
        # Every directory exists and is listable.  ``~``-relative paths are
        # expanded against the fake home, mirroring what the host does, so
        # the tree browser can resolve the typed path to its absolute form.
        decoded = urllib.parse.unquote(route.request.url)
        match = re.search(rf"/v1/hosts/{_HOST_ID}/filesystem([^?]*)", decoded)
        seg = (match.group(1) if match else "").strip("/")
        if seg in ("", "~"):
            absolute = _HOME
        elif seg.startswith("~/"):
            absolute = f"{_HOME}/{seg[2:]}"
        else:
            absolute = f"/{seg}"
        children = ["git", "work"] if absolute == _HOME else ["src", "docs"]
        entries = [
            {
                "name": name,
                "path": f"{absolute}/{name}",
                "type": "directory",
                "bytes": None,
                "modified_at": 1_700_000_000,
            }
            for name in children
        ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"object": "list", "data": entries, "has_more": False}),
        )

    page.route("**/v1/hosts", handle_hosts)
    # Regex so the slim snapshot variant (``?include_items=false&…``) is also
    # patched — otherwise the props feeding the dialog see the unpatched
    # session and take the non-coding path.
    page.route(
        re.compile(rf".*/v1/sessions/{re.escape(session_id)}(\?.*)?$"),
        handle_session_detail,
    )
    page.route(
        re.compile(rf".*/v1/hosts/{_HOST_ID}/filesystem([/?].*)?$"),
        handle_filesystem,
    )

    # ── Seed one turn so the dialog has an assistant bubble to anchor on ──

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key="typed-workspace-seed",
        match=_MARKER,
    )

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    # ── Open the clone/fork dialog ─────────────────────────────────────

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # A coding source (workspace present + online host) shows "Clone & start".
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone & start")
    # Sanity: with the prefilled absolute source directory the form is
    # submittable — host online, directory valid.  This pins the final
    # assertion's failure on the typed path alone, not on broken geometry.
    expect(submit).to_be_enabled(timeout=15_000)

    # ── The user journey: manually type a directory (no Enter, no browser) ──

    page.get_by_test_id("fork-session-advanced-toggle").click()
    workspace_input = page.get_by_test_id("workspace-path-input")
    expect(workspace_input).to_have_value(_SRC_DIR)

    workspace_input.fill(_TYPED)

    # THE BUG: the submit must be enabled once a real ``~``-path has been
    # typed.  The dialog resolves it against the host's home (from the home
    # listing) to the absolute directory the server needs — no Enter, no tree
    # browser.  On the buggy build the form keeps the raw "~/…" text as its
    # (invalid) workspace value, so the button stays greyed out until the
    # browser's "Select" button is clicked.
    expect(submit).to_be_enabled(timeout=5_000)
    # The tree browser was never opened — resolution is purely from typing.
    expect(page.get_by_test_id("workspace-picker")).not_to_be_visible()
