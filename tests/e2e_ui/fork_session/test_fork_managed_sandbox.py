"""Sandbox fork controls across the shared browser entry points.

Sandbox metadata and managed request fields are mocked; the session fork,
transcript, and navigation use the real server. Provider and route tests cover
sandbox provisioning and filesystem authorization separately.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.sessions.test_host_badge import _FAKE_HOST_ID, _patch_host_view

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


def _managed_info_body(fork_available: bool) -> str:
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
            "sandbox_provider_capabilities": {"modal": {"fork_sandbox": fork_available}},
            "server_version": "0.0.0-e2e",
            "smart_routing_enabled": False,
        }
    )


def _route_managed_deployment(
    page: Page, session_id: str, *, fork_available: bool, reconnect: bool
) -> None:
    """Make the SPA see a sandbox-only deployment holding a sandbox session.

    :param page: The page to install routes on.
    :param session_id: The seeded session, whose snapshot is augmented so the
        dialog sees a coding source with a recorded sandbox repository.
    """

    def handle_info(route: Route) -> None:
        """Advertise the managed-sandbox capability."""
        route.fulfill(
            status=200, content_type="application/json", body=_managed_info_body(fork_available)
        )

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
            snapshot["sandbox_fork_provider"] = "modal" if fork_available else None
            if reconnect:
                snapshot.update(
                    host_id=_FAKE_HOST_ID, host_resumable=False, created_at=1_700_000_000
                )
            labels = snapshot.get("labels")
            snapshot["labels"] = {
                **(labels if isinstance(labels, dict) else {}),
                _SANDBOX_REPO_LABEL_KEY: _SOURCE_REPO,
            }
        route.fulfill(response=response, body=json.dumps(snapshot))

    page.route("**/v1/info", handle_info)
    if not reconnect:
        page.route("**/v1/hosts", handle_hosts)
    # Anchored on the id so the collection route (``/v1/sessions?…``) and the
    # sub-resources (``/items``, ``/agent``, …) are left alone.
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), handle_session)


@pytest.mark.parametrize(
    ("entrypoint", "copy_sandbox", "viewport_width"),
    [
        ("message", None, 1280),
        ("message", True, 1280),
        ("header", True, 1280),
        ("header", False, 390),
        ("sidebar", True, 1280),
        ("sidebar_context", True, 390),
        ("reconnect", True, 1280),
    ],
)
def test_fork_onto_managed_sandbox_with_no_host_online(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    entrypoint: str,
    copy_sandbox: bool | None,
    viewport_width: int,
) -> None:
    """Fork with no online host, copying current files only when requested."""
    base_url, session_id = seeded_session
    page.set_viewport_size({"width": viewport_width, "height": 900})
    fork_bodies: list[dict[str, Any]] = []
    runner_launches: list[str] = []

    def handle_fork(route: Route) -> None:
        """Record managed intent; forward an ordinary fork to the local server."""
        body = json.loads(route.request.post_data or "{}")
        fork_bodies.append(body)
        forwarded = {
            key: value
            for key, value in body.items()
            if key not in {"host_type", "sandbox_provider", "workspace", "fork_sandbox"}
        }
        response = route.fetch(post_data=json.dumps(forwarded))
        route.fulfill(response=response)

    def handle_runner_launch(route: Route) -> None:
        runner_launches.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps({}))

    if entrypoint == "reconnect":
        _patch_host_view(
            page,
            session_id,
            host={
                "name": "Sandbox",
                "owner": "e2e",
                "status": "offline",
                "sandbox_provider": "modal",
            },
            host_online=False,
            runner_online=True,
            host_resumable=False,
        )
    _route_managed_deployment(
        page,
        session_id,
        fork_available=copy_sandbox is not None,
        reconnect=entrypoint == "reconnect",
    )
    page.route(re.compile(r"/v1/sessions/[0-9a-f]{32}/fork$"), handle_fork)
    page.route(re.compile(r"/v1/hosts/[^/]+/runners$"), handle_runner_launch)

    configure_mock_llm(
        mock_llm_server_url, [{"text": "Sandbox fork conversation."}], key="default"
    )
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill("Reply with just OK.")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator(_ASSISTANT)
    expect(assistant).to_have_count(1, timeout=60_000)

    if entrypoint == "message":
        assistant.first.hover()
        page.get_by_test_id("fork-from-response").first.click()
    elif entrypoint == "header":
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_test_id("header-fork-conversation").click()
    elif entrypoint in {"sidebar", "sidebar_context"}:
        if viewport_width < 768:
            page.get_by_role("button", name="Open sidebar", exact=True).click()
        row = page.locator(f'li[data-sidebar-session-id="{session_id}"]')
        if entrypoint == "sidebar_context":
            row.locator(f'a[href="/c/{session_id}"]').click(button="right")
        else:
            row.hover()
            row.get_by_test_id("conversation-actions").click()
        page.get_by_test_id("fork-conversation").click()
    else:
        page.get_by_test_id("composer-host-select").click()
        page.get_by_role("menuitem", name="Reconnect host", exact=True).click()
        page.get_by_test_id("reconnect-session-tab-clone").click()

    dialog = page.get_by_test_id(
        "reconnect-session-dialog" if entrypoint == "reconnect" else "fork-session-dialog"
    )
    expect(dialog).to_be_visible()
    checkbox = dialog.get_by_role("checkbox", name="Fork sandbox as well")
    if copy_sandbox is None:
        expect(checkbox).to_have_count(0)
    else:
        expect(checkbox).to_be_checked()
        expect(dialog.get_by_test_id("fork-session-host-select")).to_have_count(0)
        expect(dialog).to_contain_text("original sandbox's current files")
        if entrypoint == "message":
            expect(dialog).to_contain_text(
                "Files reflect the current sandbox, not the earlier message."
            )
        if not copy_sandbox:
            checkbox.uncheck()

    dialog.get_by_test_id("fork-session-advanced-toggle").click()
    if copy_sandbox:
        expect(dialog.get_by_test_id("fork-session-sandbox-repo-input")).to_have_count(0)
    else:
        host_select = dialog.get_by_test_id("fork-session-host-select")
        expect(host_select).to_be_visible()
        host_select.click()
        sandbox_row = page.get_by_test_id("fork-session-sandbox-option")
        expect(sandbox_row).to_contain_text("Modal Sandbox")
        sandbox_row.click()
        expect(dialog.get_by_test_id("fork-session-sandbox-repo-input")).to_have_value(
            _SOURCE_REPO_URL
        )
        expect(dialog.get_by_test_id("fork-session-sandbox-branch-input")).to_have_value(
            _SOURCE_REPO_BRANCH
        )
    expect(dialog.get_by_test_id("fork-session-branch-input")).to_have_count(0)
    page.screenshot(path=str(tmp_path / f"{entrypoint}-{copy_sandbox}-fork-sandbox.png"))
    submit = dialog.get_by_test_id("fork-session-submit")
    expect(submit).to_be_enabled()
    submit.click()

    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"), timeout=30_000
    )
    expect(dialog).not_to_be_visible()
    assert len(fork_bodies) == 1, fork_bodies
    body = fork_bodies[0]
    assert body["host_type"] == "managed", body
    assert body["sandbox_provider"] == "modal", body
    assert bool(body.get("up_to_response_id")) == (entrypoint == "message"), body
    if copy_sandbox:
        assert body["fork_sandbox"] is True, body
        assert "workspace" not in body, body
    else:
        assert "fork_sandbox" not in body, body
        assert body["workspace"] == _SOURCE_REPO, body
    assert runner_launches == [], runner_launches
    fork_id = page.url.rsplit("/", 1)[-1]
    snapshot = page.request.get(f"{base_url}/v1/sessions/{fork_id}").json()
    assert snapshot["id"] == fork_id
    assert snapshot["items"], snapshot
    page.unroute_all(behavior="ignoreErrors")
