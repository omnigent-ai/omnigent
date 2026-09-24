"""Browser e2e: the sandbox repository field must refuse credential URLs.

``isValidSandboxRepoUrl`` gates the Add button (and the paste-then-Enter
shortcut) of the new-chat landing's sandbox repository popover. The server
treats an embedded-credential repository URL (``user:token@host``) as a
secret-handling boundary: ``parse_repo_workspace`` rejects it at request
validation (422) and keeps the URL out of the error message so the token
never lands in labels, Pod specs, or logs. The client is the last place the
token can be stopped BEFORE it leaves the browser, so the field must refuse
such a URL inline: Add stays disabled, Enter adds nothing, and no request
ever carries the token.

The managed-sandbox capability is stubbed (``/v1/info`` + zero connected
hosts), the same way the rest of the suite fakes it — the harness runs no
sandbox provider. The create ``POST /v1/sessions`` is NOT stubbed: when the
client gate fails, the request really leaves the browser and the live server
answers it, which is exactly the leak under test.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, Request, expect

# Credential-embedded forms the server rejects with 422. The https form hides
# the userinfo in the authority; the scp form smuggles a second ``@`` after
# the ``git@`` user. The token marker is what must never appear in a request.
_TOKEN_MARKER = "s3cr3t-t0ken"
_CRED_HTTPS_URL = f"https://user:{_TOKEN_MARKER}@github.com/org/repo"
_CRED_SCP_URL = f"git@user:{_TOKEN_MARKER}@github.com:org/repo"

# Forms both sides accept — the guard that the fix does not over-tighten.
_VALID_HTTPS_URL = "https://github.com/org/repo"
_VALID_SCP_URL = "git@github.com:org/repo.git"


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


def _agents_body() -> str:
    """``GET /v1/agents``: one agent, so it auto-selects and no pick is needed."""
    return json.dumps(
        {
            "data": [
                {
                    "id": "ag_claude_e2e",
                    "name": "claude-native-ui",
                    "display_name": "Claude Code",
                    "description": "Anthropic's coding agent",
                    "harness": None,
                    "skills": [],
                }
            ]
        }
    )


def _route_managed_sandbox_deployment(page: Page) -> None:
    """Make the SPA see a sandbox-only deployment with zero hosts online.

    With no connected host and the managed capability advertised, the landing
    composer defaults to the sandbox target and renders the repository chip.
    The create endpoint is deliberately left un-routed so it reaches the real
    server.

    :param page: The Playwright page to install routes on.
    """
    page.route(
        "**/v1/info",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_managed_info_body()
        ),
    )
    page.route(
        "**/v1/hosts",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"hosts": []})
        ),
    )
    page.route(
        "**/v1/agents",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=_agents_body()
        ),
    )
    # The harness runs no sandbox provider, so its model-options probe is faked
    # as unconfigured — same stub the rest of the suite uses.
    page.route(
        "**/v1/sandbox-providers/*/harnesses/*/model-options*",
        lambda route: route.fulfill(
            json={
                "configured": False,
                "status": "unconfigured",
                "models": [],
                "configuration_revision": None,
                "provider_label": None,
                "default_model": None,
            }
        ),
    )


def _open_repo_popover(page: Page, base_url: str) -> None:
    """Open the landing composer's sandbox repository popover.

    :param page: The Playwright page.
    :param base_url: The live server's base URL.
    """
    page.goto(f"{base_url}/")
    page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    chip = page.get_by_test_id("new-chat-landing-repo-chip")
    expect(chip).to_be_visible(timeout=15_000)
    chip.click()
    expect(page.get_by_test_id("new-chat-landing-repo-input")).to_be_visible()


def test_credential_repo_url_never_leaves_the_browser(page: Page, live_server: str) -> None:
    """A pasted credential URL must be refused inline, not shipped to the server.

    Drives the reported journey end to end: paste
    ``https://user:token@github.com/org/repo`` into the repository field, add
    it (Enter), and start the session. Expected: the field refuses the URL
    inline, so no request ever contains the token. Pre-fix the client gate
    accepts the URL, the repo is added, and the create request carries the
    token to the server — which rejects it with a 422 only AFTER the secret
    left the browser.
    """
    leaked: list[str] = []

    def record_leak(request: Request) -> None:
        if _TOKEN_MARKER in (request.post_data or "") or _TOKEN_MARKER in request.url:
            leaked.append(f"{request.method} {request.url}")

    page.on("request", record_leak)
    _route_managed_sandbox_deployment(page)
    _open_repo_popover(page, live_server)

    repo_input = page.get_by_test_id("new-chat-landing-repo-input")
    repo_input.fill(_CRED_HTTPS_URL)
    repo_input.press("Enter")
    page.keyboard.press("Escape")

    page.get_by_test_id("new-chat-landing-input").fill("Audit this repository.")
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.split("?")[0].rstrip("/").endswith("/v1/sessions")
        ),
        timeout=30_000,
    ) as create_info:
        page.get_by_test_id("new-chat-landing-submit").click()
    create_status = create_info.value.status

    # Let the create's outcome render so a failure is visible on screen (and
    # in recordings) before the verdict below.
    page.wait_for_timeout(1_500)
    assert leaked == [], (
        "the embedded credential left the browser "
        f"(create answered HTTP {create_status}): {leaked}"
    )


def test_add_button_refuses_credential_repo_urls_inline(page: Page, live_server: str) -> None:
    """The Add button (and Enter) must stay inert for credential URLs.

    Mirrors the server's boundary in ``parse_repo_workspace``: any ``@`` after
    the scheme (https) or the ``git@`` user (scp) is embedded userinfo. Valid
    URLs of both forms must keep enabling Add — the fix must not over-reject.
    """
    _route_managed_sandbox_deployment(page)
    _open_repo_popover(page, live_server)

    repo_input = page.get_by_test_id("new-chat-landing-repo-input")
    add = page.get_by_test_id("new-chat-landing-repo-add")

    repo_input.fill(_VALID_HTTPS_URL)
    expect(add).to_be_enabled()
    repo_input.fill(_VALID_SCP_URL)
    expect(add).to_be_enabled()

    repo_input.fill(_CRED_HTTPS_URL)
    expect(add).to_be_disabled()
    repo_input.fill(_CRED_SCP_URL)
    expect(add).to_be_disabled()

    repo_input.press("Enter")
    expect(page.get_by_test_id("new-chat-landing-repo-row")).to_have_count(0)
