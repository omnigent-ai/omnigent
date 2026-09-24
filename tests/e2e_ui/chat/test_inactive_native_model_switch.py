"""Model switching on an *inactive* native session (one that has stopped and has
no running terminal) must first recover — start — the missing terminal via the
session-recovery API, then apply the selection. On the buggy build the composer
calls the model update directly, so the PATCH forwards a ``model_change`` to a
runner with no live pane, the server rejects it, and the picker surfaces a
"Couldn't update configuration" error while the model stays unchanged.

Browsing the switcher must stay usable without launching a terminal: merely
opening the picker must NOT trigger recovery (recovering on open would
needlessly block browsing).

Harness/server-boundary route-patch idiom, matching ``test_claude_model_picker``
and ``test_model_flows_contract``: the real SPA drives a real spawned server;
only the session snapshot (shaped as a terminal-less claude-native session) and
the model-change server contract (a PATCH that succeeds only once the terminal
has been recovered) are stubbed. The seeded session's real runner pane is
deleted server-side first, so the terminals inventory — HTTP snapshot and SSE
replay alike — is genuinely empty, matching a session whose terminal is gone.
The exercised surface — open the picker on an inactive native session, select a
model — is the real user journey.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_CLAUDE_NATIVE_WRAPPER = "claude-code-native-ui"
_BOUND_MODEL = "system.ai.claude-sonnet-5"
_MODEL_OPTIONS = [
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-10",
        "displayName": "Opus 4.10",
        "isDefault": False,
    },
    {"id": "sonnet", "model": _BOUND_MODEL, "displayName": "Sonnet 5", "isDefault": True},
    {
        "id": "haiku",
        "model": "system.ai.claude-haiku-4-5",
        "displayName": "Haiku 4.5",
        "isDefault": False,
    },
]

# Mirrors the server's terminal-less native model-change rejection
# (``_surface_model_change_forward_failure`` -> RUNNER_UNAVAILABLE).
_MODEL_CHANGE_FAILED = (
    "The terminal did not apply the model change. "
    "The previous selection has been restored."
)


def _install_inactive_native_session(page: Page, session_id: str) -> dict[str, list]:
    """Shape the session as a terminal-less claude-native session.

    Two route patches, registered before navigation:

    - ``GET/PATCH /v1/sessions/{id}`` — stamp the claude-native wrapper + bound
      model + catalog rows so the picker is browseable, and gate the model
      PATCH: it 200s (echoing the new override) only once a ``retry_session``
      recovery has run; otherwise it returns the server's terminal-less 503.
    - ``POST /v1/sessions/{id}/events`` — a ``retry_session`` marks the terminal
      recovered and returns the ``native_terminal_ready`` recovery envelope.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch.
    :returns: Recorded ``{"model_patches": [...], "recoveries": [...]}``.
    """
    recovered = [False]
    recorded: dict[str, list] = {"model_patches": [], "recoveries": []}
    latest_payload: list[dict | None] = [None]

    def _events(route: Route) -> None:
        request = route.request
        body: dict[str, object] | None = None
        try:
            body = json.loads(request.post_data or "")
        except (json.JSONDecodeError, TypeError):
            body = None
        is_retry = isinstance(body, dict) and body.get("type") == "retry_session"
        if request.method == "POST" and is_retry:
            recovered[0] = True
            recorded["recoveries"].append(body)
            route.fulfill(
                status=200,
                headers={"content-type": "application/json"},
                body=json.dumps({"recovered": True, "recovery": "native_terminal_ready"}),
            )
            return
        route.continue_()

    def _session(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return

        if request.method == "GET":
            response = fetch_with_retry(route)
            payload = response.json()
            headers = {**response.headers, "content-type": "application/json"}
        elif request.method == "PATCH":
            request_body = json.loads(request.post_data or "{}")
            payload = dict(latest_payload[0] or {})
            headers = {"content-type": "application/json"}
            if "model_override" in request_body:
                recorded["model_patches"].append(request_body)
                if not recovered[0]:
                    route.fulfill(
                        status=503,
                        headers=headers,
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "runner_unavailable",
                                    "message": _MODEL_CHANGE_FAILED,
                                }
                            }
                        ),
                    )
                    return
                payload["model_override"] = request_body["model_override"]
        else:
            route.continue_()
            return

        payload["labels"] = {
            **payload.get("labels", {}),
            _WRAPPER_LABEL_KEY: _CLAUDE_NATIVE_WRAPPER,
        }
        payload["harness"] = "claude"
        payload["llm_model"] = _BOUND_MODEL
        payload["model_options"] = _MODEL_OPTIONS
        latest_payload[0] = dict(payload)
        route.fulfill(status=200, headers=headers, body=json.dumps(payload))

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}/events$"), _events)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _session)
    return recorded


def _remove_runner_terminals(base_url: str, session_id: str) -> None:
    """Delete the session's real runner panes so it has no terminal.

    The seeded fixture's live runner auto-creates its embedded REPL pane, which
    would reach the browser through both the terminals snapshot and the SSE
    resource replay — making the app (correctly) skip recovery. Removing it
    server-side shapes the reported world: a session with no running terminal.

    :param base_url: Spawned server base URL.
    :param session_id: Session whose panes to remove.
    :returns: None.
    """
    listing = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        params={"order": "asc", "limit": 1000},
        timeout=10.0,
    )
    if listing.status_code != 200:
        return
    for row in listing.json().get("data", []):
        httpx.delete(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals/{row['id']}",
            timeout=10.0,
        ).raise_for_status()


def test_inactive_native_model_switch_recovers_terminal_first(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Selecting a model on an inactive native session recovers, then applies.

    On the buggy build the composer PATCHes the model change without first
    recovering the terminal, so it fails on a terminal-less session and the
    picker shows the config error. The fix recovers the terminal via the
    session-recovery API on selection, then applies the change.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session, whose browser view is patched to a terminal-less claude-native
        session with a recovery-gated model PATCH.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _remove_runner_terminals(base_url, session_id)
    recorded = _install_inactive_native_session(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    chip = page.get_by_test_id("composer-agent-config-value")
    expect(chip).to_contain_text("Sonnet 5", timeout=15_000)

    # Open the switcher. Browsing must stay usable without a terminal, so this
    # must NOT recover: no retry_session so far.
    page.get_by_test_id("composer-config-gear").click()
    page.get_by_test_id("composer-agent-edit").click()
    rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
    expect(rows).to_have_count(len(_MODEL_OPTIONS))
    assert recorded["recoveries"] == [], (
        "opening the model switcher must not launch a terminal, "
        f"but recovery was triggered on open: {recorded['recoveries']}"
    )

    # Select a different model.
    page.locator('[role="menuitemcheckbox"][data-model-id="opus"]').click()

    # Settle on a definitive outcome before asserting: the fix recovers the
    # terminal (recovery posted) and applies cleanly; the buggy build skips
    # recovery and the picker shows the config error.
    error_btn = page.get_by_test_id("composer-config-error")
    for _ in range(40):
        if recorded["recoveries"] or error_btn.count() > 0:
            break
        page.wait_for_timeout(200)

    assert recorded["recoveries"], (
        "selecting a model on an inactive native session must recover the "
        "terminal via the session-recovery API before applying the change, "
        "but no retry_session recovery was posted"
    )
    expect(error_btn).to_have_count(0)
