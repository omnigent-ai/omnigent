"""Browser e2e: cloning a session onto a server-provisioned sandbox.

Before this flow existed, the Clone dialog could only ever target a host the
caller had already connected with ``omni host``. On a deployment whose only
compute is managed sandboxes there is never such a host, so the dialog's host
section collapsed to "No hosts online. Reconnect from your terminal…" and the
Clone button stayed greyed — the whole clone workflow was unreachable.

This drives the real chain with ZERO hosts online: seeded session → the
per-message fork action → the dialog's host picker → the sandbox row →
``POST /v1/sessions/{id}/fork`` carrying ``host_type: "managed"`` → navigate
into the clone. It asserts the three things the UI is responsible for:

1. The sandbox row is offered and Clone is enabled with no host online (the
   regression this flow fixes — pre-fix there was no row at all).
2. Picking it swaps the host-directory chrome for the repository fields, and
   those prefill from the repository the SOURCE session recorded — so cloning
   a sandbox session lands in the same checkout.
3. The fork request carries ``host_type`` / ``sandbox_provider`` / the
   composed ``workspace``, and no ``POST /v1/hosts/{id}/runners`` follows
   (the server provisions the host, so a host-bind would target a host that
   does not exist).

Three server responses are stubbed, because the e2e_ui harness deliberately
runs no sandbox provider: ``/v1/info`` advertises the capability, ``/v1/hosts``
reports none connected, and the session snapshot is AUGMENTED (fetched for
real, then given a workspace + the sandbox repository label) so the source
looks like a sandbox session. Everything else — the transcript, the dialog,
the fork itself, the navigation — is real. Same approach the New Chat sandbox
coverage takes in ``tests/e2e_ui/start_session/test_start_session.py``, which
also cannot provision a real sandbox in CI.
"""

from __future__ import annotations

import json
import re
from typing import Any

from playwright.sync_api import Page, Route, expect

# Repository the SOURCE session records. The dialog must prefill from it, and
# the fork request must carry it back composed with its branch.
_SOURCE_REPO_URL = "https://github.com/omnigent-ai/fixture-repo"
_SOURCE_REPO_BRANCH = "release-9.9"
_SOURCE_REPO = f"{_SOURCE_REPO_URL}#{_SOURCE_REPO_BRANCH}"

# Server label recording that repository (the server's MANAGED_REPO_LABEL_KEY,
# mirrored in the web bundle as SANDBOX_REPO_LABEL_KEY).
_SANDBOX_REPO_LABEL_KEY = "omnigent.sandbox.repo"

# In-sandbox workspace path a managed session carries. Its presence is what
# makes the dialog treat the source as a *coding* source and render the host
# section at all.
_SOURCE_WORKSPACE = "/root/workspace/fixture-repo"

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


def _managed_info_body() -> str:
    """``GET /v1/info`` for a deployment that can provision sandboxes.

    ``managed_sandboxes_enabled`` is the gate the dialog reads; naming
    ``modal`` as the provider is what labels the row "Modal Sandbox" and what
    must ride into the fork request as ``sandbox_provider``.

    :returns: The JSON body.
    """
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
        """Advertise the managed-sandbox capability."""
        route.fulfill(status=200, content_type="application/json", body=_managed_info_body())

    def handle_hosts(route: Route) -> None:
        """Report zero connected hosts — the dead end this flow fixes."""
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"hosts": []}))

    def handle_session(route: Route) -> None:
        """Fetch the real snapshot, then make it look like a sandbox session.

        Augmenting rather than replacing keeps the transcript, agent binding,
        and status real, so only the two fields under test are synthetic.
        """
        response = route.fetch()
        try:
            snapshot = response.json()
        except Exception:
            # A non-JSON body (an error page) isn't ours to rewrite — pass it
            # through so the failure surfaces as itself.
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
    # Anchored on the id so the collection route (``/v1/sessions?…``) and the
    # sub-resources (``/items``, ``/agent``, …) are left alone.
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), handle_session)


def test_fork_onto_managed_sandbox_with_no_host_online(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Clone a sandbox session onto a fresh sandbox with no host connected.

    Failure modes this catches:

    - The sandbox row is missing, or the picker collapses into the connect-
      from-your-terminal instructions when no host is online (the reported
      dead end — the clone is then impossible on a sandbox-only deployment).
    - The repository fields don't appear, or don't prefill from the source, so
      a clone silently lands in an empty sandbox and its copied transcript
      references files that aren't there.
    - The fork request omits ``host_type`` / ``sandbox_provider`` / the
      composed ``workspace``, so the server creates an unbound clone that
      never starts.
    - The dialog still fires the host-bind ``POST /v1/hosts/{id}/runners``,
      which would target a host that does not exist.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` session.
    :param mock_llm_server_url: Unused directly; forces the mock-LLM fixture
        so the seeded turn below completes.
    """
    del mock_llm_server_url
    base_url, session_id = seeded_session

    fork_bodies: list[dict[str, Any]] = []
    runner_launches: list[str] = []

    def handle_fork(route: Route) -> None:
        """Record the fork request, then forward it as an ordinary fork.

        The managed fields are stripped before forwarding: this harness runs
        no sandbox provider, so a genuinely managed fork would 400 on the
        server's "managed hosts are not configured" guard. Stripping keeps the
        clone, the navigation, and the copied transcript real while the
        recorded body carries the contract under test.
        """
        raw = route.request.post_data or "{}"
        body = json.loads(raw)
        fork_bodies.append(body)
        forwarded = {
            key: value
            for key, value in body.items()
            if key not in {"host_type", "sandbox_provider", "workspace"}
        }
        response = route.fetch(post_data=json.dumps(forwarded))
        route.fulfill(response=response)

    def handle_runner_launch(route: Route) -> None:
        """Record any host-bind attempt — a sandbox clone must make none."""
        runner_launches.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({}))

    _route_managed_deployment(page, session_id)
    page.route(re.compile(r"/v1/sessions/[0-9a-f]{32}/fork$"), handle_fork)
    page.route(re.compile(r"/v1/hosts/[^/]+/runners$"), handle_runner_launch)

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

    # (1) With zero hosts online the picker still offers a target. Pre-fix the
    # host section rendered only the connect instructions and Clone was greyed.
    host_select = page.get_by_test_id("fork-session-host-select")
    expect(host_select).to_be_visible()
    host_select.click()
    sandbox_row = page.get_by_test_id("fork-session-sandbox-option")
    expect(sandbox_row).to_contain_text("Modal Sandbox")
    sandbox_row.click()

    # (2) Picking the sandbox swaps in the repository chrome, prefilled from
    # the source's own repository.
    expect(page.get_by_test_id("fork-session-sandbox-hint")).to_be_visible()
    page.get_by_test_id("fork-session-advanced-toggle").click()
    expect(page.get_by_test_id("fork-session-sandbox-repo-input")).to_have_value(_SOURCE_REPO_URL)
    expect(page.get_by_test_id("fork-session-sandbox-branch-input")).to_have_value(
        _SOURCE_REPO_BRANCH
    )
    # The host-directory chrome is gone: a sandbox has no path to browse yet.
    expect(page.get_by_test_id("fork-session-branch-input")).to_have_count(0)

    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_be_enabled()
    submit.click()

    # Land in a DIFFERENT session — still on the source means navigation never
    # fired; a visible dialog means the fork call failed.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()

    # (3) The request the dialog actually produced.
    assert len(fork_bodies) == 1, f"expected exactly one fork request, got {fork_bodies}"
    body = fork_bodies[0]
    assert body["host_type"] == "managed", body
    assert body["sandbox_provider"] == "modal", body
    assert body["workspace"] == _SOURCE_REPO, body
    # The server provisions the host, so the dialog must not also bind one.
    assert runner_launches == [], runner_launches
