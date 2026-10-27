"""Browser e2e: the Clone dialog's repository field must refuse credential URLs.

The fork/clone dialog gates its sandbox repository input with the same
``isValidSandboxRepoUrl`` the new-chat landing uses, so it shares the landing's
secret-handling gap: a repository URL with embedded credentials
(``user:token@host``) keeps the Clone button enabled and the token rides the
fork request to the server, which rejects it with a 422 only after the secret
left the browser. Expected: the Clone button greys out inline, exactly as it
does for a malformed URL.

The managed deployment is stubbed the same way as
``test_fork_managed_sandbox.py`` (capability advertised, zero hosts, source
snapshot augmented into a sandbox session) because the harness runs no sandbox
provider; the dialog, its gating, and the transcript are real.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

_SOURCE_REPO_URL = "https://github.com/omnigent-ai/fixture-repo"
_SOURCE_REPO_BRANCH = "release-9.9"
_SOURCE_REPO = f"{_SOURCE_REPO_URL}#{_SOURCE_REPO_BRANCH}"

# Server label recording the source repository (MANAGED_REPO_LABEL_KEY,
# mirrored in the web bundle as SANDBOX_REPO_LABEL_KEY).
_SANDBOX_REPO_LABEL_KEY = "omnigent.sandbox.repo"

# In-sandbox workspace path; its presence makes the dialog treat the source as
# a coding source and render the host section at all.
_SOURCE_WORKSPACE = "/root/workspace/fixture-repo"

_CRED_HTTPS_URL = "https://user:s3cr3t-t0ken@github.com/org/repo"
_CRED_SCP_URL = "git@user:s3cr3t-t0ken@github.com:org/repo"

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


def _managed_info_body() -> str:
    """``GET /v1/info`` for a deployment that can provision sandboxes."""
    return json.dumps(
        {
            "accounts_enabled": False,
            "login_url": None,
            "needs_setup": False,
            "databricks_features": False,
            "managed_sandboxes_enabled": True,
            "sandbox_provider": "modal",
            "sandbox_providers": ["modal"],
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


def _route_managed_deployment(page: Page, session_id: str) -> None:
    """Make the SPA see a sandbox-only deployment holding a sandbox session.

    :param page: The page to install routes on.
    :param session_id: The seeded session, whose snapshot is augmented so the
        dialog sees a coding source with a recorded sandbox repository.
    """

    def handle_info(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=_managed_info_body())

    def handle_hosts(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"hosts": []}))

    def handle_session(route: Route) -> None:
        # Fetch the real snapshot, then make it look like a sandbox session so
        # only the two fields the dialog reads are synthetic.
        response = route.fetch()
        try:
            snapshot = response.json()
        except Exception:
            route.fulfill(response=response)
            return
        if isinstance(snapshot, dict):
            snapshot["workspace"] = _SOURCE_WORKSPACE
            labels = snapshot.get("labels")
            snapshot["labels"] = {
                **(labels if isinstance(labels, dict) else {}),
                _SANDBOX_REPO_LABEL_KEY: _SOURCE_REPO,
            }
        route.fulfill(response=response, body=json.dumps(snapshot))

    page.route("**/v1/info", handle_info)
    page.route("**/v1/hosts", handle_hosts)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), handle_session)


def test_fork_dialog_refuses_credential_repo_url_inline(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Typing a credential URL into the Clone dialog must grey the Clone button.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    :param mock_llm_server_url: Unused directly; forces the mock-LLM fixture
        so the seeded turn below completes.
    """
    del mock_llm_server_url
    base_url, session_id = seeded_session

    _route_managed_deployment(page, session_id)
    page.goto(f"{base_url}/c/{session_id}")

    # One committed turn so the per-message fork action has a bubble to anchor
    # on (the same setup the sibling fork tests use).
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill("Reply with just OK.")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(_ASSISTANT)
    expect(assistant).to_have_count(1, timeout=60_000)

    assistant.first.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    page.get_by_test_id("fork-session-host-select").click()
    page.get_by_test_id("fork-session-sandbox-option").click()
    page.get_by_test_id("fork-session-advanced-toggle").click()

    repo_input = page.get_by_test_id("fork-session-sandbox-repo-input")
    expect(repo_input).to_have_value(_SOURCE_REPO_URL)
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_be_enabled()

    repo_input.fill(_CRED_HTTPS_URL)
    expect(submit).to_be_disabled()
    repo_input.fill(_CRED_SCP_URL)
    expect(submit).to_be_disabled()

    # A well-formed URL must re-enable Clone — the fix must not over-reject.
    repo_input.fill(_SOURCE_REPO_URL)
    expect(submit).to_be_enabled()
