"""E2E: claude-native model picker follows its live Databricks catalog."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from omnigent.claude_native import ClaudeNativeUcodeConfig, claude_native_model_options
from tests.e2e_ui.conftest import seed_committed_turn

_EXPECTED_ROWS = [
    ("opus", "Opus 4.10"),
    ("sonnet", "Sonnet 5"),
    ("haiku", "Haiku 4.5"),
]
_MODEL_OPTIONS = [
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-10",
        "displayName": "Opus 4.10",
        "isDefault": False,
    },
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": True,
    },
    {
        "id": "haiku",
        "model": "system.ai.claude-haiku-4-5",
        "displayName": "Haiku 4.5",
        "isDefault": False,
    },
]


def _patch_session_as_claude_native(
    page: Page,
    session_id: str,
    model_override: str | None = None,
    catalog_state: dict[str, bool] | None = None,
    model_options: list[dict] | None = None,
    llm_model: str = "system.ai.claude-sonnet-5",
    host_asleep: bool = False,
) -> list[dict]:
    """Patch the browser's session snapshot into a claude-native response.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server. This route patch changes only ``GET``
    and ``PATCH /v1/sessions/{session_id}`` responses as seen by the browser,
    simulating a Claude-native session whose launch-time Databricks query
    returned only Opus 4.10, Sonnet 5, and Haiku 4.5.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param model_override: Optional session-scoped model override to expose.
    :param catalog_state: Optional mutable readiness gate for delayed options.
    :param model_options: Catalog rows to expose; defaults to the live-alias set.
    :param llm_model: Bound model id shown for the session.
    :param host_asleep: Shape the snapshot like a dormant resumable managed
        host (host-bound, resumable, aged past the startup grace); pair with
        :func:`_force_asleep_liveness` to drive the ``host_asleep`` state.
    :returns: Captured PATCH request bodies.
    """
    latest_payload: dict | None = None
    patch_bodies: list[dict] = []

    def _handle(route: Route) -> None:
        nonlocal latest_payload
        request = route.request
        parsed = urlparse(request.url)
        if parsed.path != f"/v1/sessions/{session_id}":
            route.continue_()
            return

        headers = {"content-type": "application/json"}
        if request.method == "GET":
            response = route.fetch()
            payload = response.json()
            headers = {**response.headers, **headers}
        elif request.method == "PATCH":
            request_body = json.loads(request.post_data or "{}")
            patch_bodies.append(request_body)
            payload = dict(latest_payload or {})
            if "model_override" in request_body:
                payload["model_override"] = request_body["model_override"]
        else:
            route.continue_()
            return

        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        payload["llm_model"] = llm_model
        catalog = _MODEL_OPTIONS if model_options is None else model_options
        payload["model_options"] = (
            catalog if catalog_state is None or catalog_state["ready"] else []
        )
        if model_override is not None:
            payload["model_override"] = model_override
        if host_asleep:
            payload["host_id"] = payload.get("host_id") or "host_test_managed"
            payload["host_resumable"] = True
            # Age the session past the startup grace so liveness can't read
            # the runner-down state as a cold boot (see useSessionLiveness).
            payload["created_at"] = 1_700_000_000
        latest_payload = dict(payload)
        route.fulfill(
            status=200,
            headers=headers,
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return patch_bodies


def test_claude_native_picker_lists_only_live_databricks_models(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The picker shows friendly labels for only the live gateway aliases.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    # The Model dropdown lives in the config gear modal now.
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    page.get_by_test_id("composer-config-model").click()

    # The model options carry the same data-model-id rows as before (plus the
    # "Default" sentinel row the modal always offers).
    rows = page.locator('[role="option"][data-model-id]')
    expect(rows).to_have_count(len(_EXPECTED_ROWS))
    for index, (model_id, label) in enumerate(_EXPECTED_ROWS):
        row = rows.nth(index)
        expect(row).to_have_attribute("data-model-id", model_id)
        expect(row).to_contain_text(label)

    # The bound system.ai.claude-sonnet-5 model implicitly selects the "sonnet"
    # (Sonnet 5) row; fable / sonnet_5 aren't in the live catalog at all.
    sonnet_row = page.locator('[role="option"][data-model-id="sonnet"]')
    expect(sonnet_row).to_have_attribute("data-active", "true")
    expect(page.locator('[role="option"][data-model-id="fable"]')).to_have_count(0)
    expect(page.locator('[role="option"][data-model-id="sonnet_5"]')).to_have_count(0)
    _screenshot(page, "pinned-catalog-picker")


def test_claude_native_picker_updates_after_delayed_catalog(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A live catalog event fills the modal and applies a compatible sticky alias."""
    base_url, session_id = seeded_session
    catalog_state = {"ready": False}
    patch_bodies = _patch_session_as_claude_native(
        page,
        session_id,
        catalog_state=catalog_state,
    )
    stream_script = """
        (() => {
          const sessionId = __SESSION_ID__;
          const originalFetch = window.fetch.bind(window);
          window.fetch = (input, init) => {
            const url = typeof input === "string" ? input : input.url;
            const streamPath = `/v1/sessions/${sessionId}/stream`;
            if (new URL(url, window.location.origin).pathname === streamPath) {
              const body = new ReadableStream({
                start(controller) {
                  window.__claudeModelStreamController = controller;
                },
              });
              return Promise.resolve(new Response(body, {
                status: 200,
                headers: { "content-type": "text/event-stream" },
              }));
            }
            return originalFetch(input, init);
          };
        })()
        """.replace("__SESSION_ID__", json.dumps(session_id))
    page.add_init_script(
        stream_script,
    )
    page.add_init_script("window.localStorage.setItem('omnigent.picker.model', 'opus')")

    page.goto(f"{base_url}/c/{session_id}")

    label = page.get_by_test_id("composer-model-effort-label")
    expect(label).to_contain_text("system.ai.claude-sonnet-5", timeout=15_000)
    page.wait_for_function("window.__claudeModelStreamController !== undefined")

    catalog_state["ready"] = True
    page.evaluate(
        """
        ({ sessionId }) => {
          const frame = `event: session.model_options\ndata: ${JSON.stringify({
            conversation_id: sessionId,
          })}\n\n`;
          window.__claudeModelStreamController.enqueue(new TextEncoder().encode(frame));
        }
        """,
        {"sessionId": session_id},
    )

    expect(label).to_contain_text("Opus 4.10", timeout=10_000)
    assert {"model_override": "opus", "silent": True} in patch_bodies
    page.get_by_test_id("composer-config-gear").click()
    page.get_by_test_id("composer-config-model").click()
    expect(page.locator('[role="option"][data-model-id]')).to_have_count(len(_EXPECTED_ROWS))


def test_claude_native_alias_selection_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Picking Opus PATCHes its alias and the label shows the live name.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    patch_bodies = _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    page.get_by_test_id("composer-config-model").click()

    # Selecting only drafts the pick; the PATCH fires on Save.
    page.locator('[role="option"][data-model-id="opus"]').click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        page.get_by_test_id("composer-config-save").click()

    assert patch_bodies[-1] == {"model_override": "opus"}
    # The read-only composer label reflects the new pick.
    expect(page.get_by_test_id("composer-model-effort-label")).to_contain_text("Opus 4.10")


def _force_asleep_liveness(page: Page, session_id: str) -> None:
    """Patch the browser's liveness view of ``session_id`` to runner+host down.

    Paired with ``_patch_session_as_claude_native(..., host_asleep=True)``
    this yields the ``host_asleep`` liveness variant (mirrors
    ``tests/e2e_ui/sessions/test_host_asleep_composer.py``): the ``/health``
    poll reports both tunnels down, the sidebar list drops the session so the
    open view derives liveness from the patched host-bound snapshot, and the
    updates WS is blocked so a live push can't revert to the real online
    state.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    """

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        offline = {"runner_online": False, "host_online": False}
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = offline
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **offline}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _drop_from_list(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            payload["data"] = [
                r for r in rows if not (isinstance(r, dict) and r.get("id") == session_id)
            ]
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(re.compile(r"/v1/sessions(\?|$)"), _drop_from_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_claude_native_picker_saves_model_while_host_asleep(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An asleep session keeps the config gear live and Save PATCHes the model.

    A model/effort change persists server-side and applies when the next
    message wakes the session, so wherever the composer stays open (here:
    ``host_asleep``) the gear must stay enabled with the catalog-backed
    dropdown — not inert behind a live-runner requirement.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to an asleep claude-native shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _force_asleep_liveness(page, session_id)
    patch_bodies = _patch_session_as_claude_native(page, session_id, host_asleep=True)

    page.goto(f"{base_url}/c/{session_id}")

    # Wait for liveness to resolve to host_asleep (the composer's resume
    # placeholder) so the gear assertions exercise the asleep state, not the
    # initial not-yet-polled window.
    composer = page.get_by_label("Message the agent")
    expect(composer).to_have_attribute(
        "placeholder", re.compile("resume the sandbox host"), timeout=15_000
    )

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_have_attribute("aria-disabled", "false")
    gear.click()
    expect(page.get_by_test_id("composer-config-modal")).to_be_visible()
    page.get_by_test_id("composer-config-model").click()
    # The catalog still populates the dropdown while the session sleeps.
    expect(page.locator('[role="option"][data-model-id]')).to_have_count(len(_EXPECTED_ROWS))
    _screenshot(page, "asleep-config-gear")

    page.locator('[role="option"][data-model-id="opus"]').click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == f"/v1/sessions/{session_id}"
            and response.status == 200
        )
    ):
        page.get_by_test_id("composer-config-save").click()

    assert patch_bodies[-1] == {"model_override": "opus"}


def _screenshot(page: Page, name: str) -> None:
    """Save a demo screenshot when E2E_SCREENSHOT_DIR is set (local runs)."""
    shot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if shot_dir:
        page.screenshot(path=str(Path(shot_dir) / f"{name}.png"))


def test_claude_native_unpinned_gateway_catalog_offers_only_the_routable_default(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A pin-less gateway config renders one concrete row and no alias rows.

    The catalog is produced by the real ``claude_native_model_options`` for a
    provider config with no ``ANTHROPIC_DEFAULT_*_MODEL`` pins — the setup
    where the subscription aliases used to appear and canonicalize to
    Anthropic ids the gateway rejects at launch.
    """
    base_url, session_id = seeded_session
    default_model = "databricks-claude-sonnet-4-5"
    catalog = claude_native_model_options(
        ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": "https://example.databricks.com/ai-gateway/anthropic"},
            model=default_model,
        )
    )
    _patch_session_as_claude_native(
        page,
        session_id,
        model_options=catalog,
        llm_model=default_model,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # The composer label already shows the concrete routable id.
    expect(page.get_by_test_id("composer-model-effort-label")).to_contain_text(
        default_model, timeout=15_000
    )
    _screenshot(page, "unpinned-gateway-composer")

    page.get_by_test_id("composer-config-gear").click()
    page.get_by_test_id("composer-config-model").click()

    # Exactly one row — the provider's routable default, pre-selected — so no
    # alias row exists to canonicalize into an id the gateway rejects. Picking
    # it can only ever PATCH the concrete gateway id, which the launch
    # resolver passes through verbatim.
    rows = page.locator('[role="option"][data-model-id]')
    expect(rows).to_have_count(1)
    expect(rows.first).to_have_attribute("data-model-id", default_model)
    expect(rows.first).to_have_attribute("data-active", "true")
    _screenshot(page, "unpinned-gateway-picker")


_CODEX_MODEL_ID = "gpt-5.5"
_CODEX_MODEL_LABEL = "Codex Pretty 5.5"
_CODEX_MODEL_OPTIONS = [
    {
        "id": _CODEX_MODEL_ID,
        "model": "databricks-gpt-5-5",
        "displayName": _CODEX_MODEL_LABEL,
        "isDefault": True,
    }
]
_CLAUDE_LLM_MODEL = "system.ai.claude-sonnet-5"
# Long enough that the stale-label window is unmissable, short enough to keep
# the test quick. Only the switch back to the Claude session is delayed.
_SNAPSHOT_DELAY_MS = 2_000

# Records every distinct composer model label the page ever paints, tagged with
# the session route it was painted under. A transient wrong label is invisible
# to `expect()` (which retries until it passes), so the assertion runs against
# this log rather than a point-in-time read.
_LABEL_RECORDER = """
(() => {
  window.__modelLabelLog = [];
  const record = () => {
    const el = document.querySelector('[data-testid="composer-model-effort-label"]');
    const entry = { path: location.pathname, text: el ? el.textContent.trim() : "" };
    const log = window.__modelLabelLog;
    const last = log[log.length - 1];
    if (!last || last.path !== entry.path || last.text !== entry.text) log.push(entry);
  };
  new MutationObserver(record).observe(document, {
    subtree: true,
    childList: true,
    characterData: true,
  });
})()
"""

# Holds the incoming session's snapshot GET so the pre-bind window — where the
# store has dropped the outgoing session's model fields but not yet hydrated the
# incoming one's — lasts long enough to observe. Armed via `__delaySnapshot`
# right before the switch so the first visit stays fast.
_SNAPSHOT_DELAY = """
(() => {
  const sessionId = __SESSION_ID__;
  const delayMs = __DELAY_MS__;
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    if (
      window.__delaySnapshot &&
      new URL(url, window.location.origin).pathname === `/v1/sessions/${sessionId}`
    ) {
      await new Promise((resolve) => setTimeout(resolve, delayMs));
    }
    return originalFetch(input, init);
  };
})()
"""


def _patch_native_session_pair(page: Page, codex_session_id: str, claude_session_id: str) -> None:
    """Patch two session snapshots at once: one codex-native, one claude-native.

    The sibling single-session helpers each own a ``**/v1/sessions/**`` route
    and ``continue_()`` past the ids they don't serve, so two of them can't be
    composed (the first matching handler wins and goes straight to the
    network). This serves both ids from one handler instead.

    :param page: Playwright page before navigation.
    :param codex_session_id: Session id to shape as codex-native, pinned to
        ``gpt-5.5`` via ``model_override`` so binding it leaves that model as
        the cross-session sticky pick.
    :param claude_session_id: Session id to shape as claude-native, bound to
        Sonnet 5 with no override of its own.
    """

    def _handle(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if request.method != "GET" or path not in (
            f"/v1/sessions/{codex_session_id}",
            f"/v1/sessions/{claude_session_id}",
        ):
            route.continue_()
            return

        response = route.fetch()
        payload = response.json()
        if path == f"/v1/sessions/{codex_session_id}":
            wrapper, harness = "codex-native-ui", "codex"
            payload["llm_model"] = _CODEX_MODEL_ID
            payload["model_override"] = _CODEX_MODEL_ID
            payload["model_options"] = _CODEX_MODEL_OPTIONS
        else:
            wrapper, harness = "claude-code-native-ui", "claude"
            payload["llm_model"] = _CLAUDE_LLM_MODEL
            payload["model_options"] = _MODEL_OPTIONS
        payload["labels"] = {**payload.get("labels", {}), "omnigent.wrapper": wrapper}
        payload["harness"] = harness
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def test_composer_model_label_never_shows_the_previous_sessions_model(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Opening a Claude session must not paint the previous session's model.

    Failure mode this catches: a COLD open clears (never had) the
    session-scoped model fields (``sessionModelOverride`` / ``llmModel`` /
    ``codexModelOptions``) but deliberately keeps ``selectedModel``, the
    cross-session sticky pick. If the composer mounts before the snapshot
    lands, it resolves that sticky and reads ``gpt-5.5`` on a Claude session
    before correcting itself to Sonnet 5. The store must hold the hydrating
    placeholder for the whole snapshot round trip so the composer never
    mounts against empty session-scoped state.

    Cold open, not switch-back, on purpose: a conversation already visited is
    now held live (its stream stays open in the background), so returning to
    it repaints its own settled state with no snapshot fetch and therefore no
    pre-bind window at all. The first open is the only place this window
    still exists.

    :param page: Playwright page fixture.
    :param seeded_session_pair: ``(base_url, codex_session, claude_session)``
        for two real server-backed sessions; both snapshots are patched into
        native shapes as seen by the browser.
    """
    base_url, codex_session, claude_session = seeded_session_pair
    # Both sessions need a committed transcript so the composer renders rather
    # than falling back to the empty-session state.
    for session_id in (codex_session, claude_session):
        seed_committed_turn(session_id, prompt="ping", reply="pong")
    _patch_native_session_pair(page, codex_session, claude_session)
    page.add_init_script(_LABEL_RECORDER)
    page.add_init_script(
        _SNAPSHOT_DELAY.replace("__SESSION_ID__", json.dumps(claude_session)).replace(
            "__DELAY_MS__", str(_SNAPSHOT_DELAY_MS)
        )
    )

    label = page.get_by_test_id("composer-model-effort-label")

    # Open the Codex session FIRST and only — binding it makes gpt-5.5 the
    # sticky pick, and leaves Claude never-visited so its open is cold.
    page.goto(f"{base_url}/c/{codex_session}")
    expect(label).to_contain_text(_CODEX_MODEL_LABEL, timeout=15_000)

    # Cold-open Claude with its snapshot held, and watch every label the
    # composer paints under the Claude route.
    page.evaluate("window.__delaySnapshot = true")
    page.evaluate("window.__modelLabelLog = []")
    page.locator(f'a[href="/c/{claude_session}"]').click()
    page.wait_for_url(re.compile(rf"/c/{re.escape(claude_session)}"))
    expect(label).to_contain_text("Sonnet 5", timeout=15_000)

    log = page.evaluate("window.__modelLabelLog")
    claude_labels = [e["text"] for e in log if e["path"] == f"/c/{claude_session}"]
    # Guard against a no-op run: the held snapshot must have produced at least
    # one pre-bind paint before the settled Sonnet 5 label.
    assert len(claude_labels) > 1, (
        f"the delayed-bind window was never observed (labels: {claude_labels}); "
        "the snapshot delay did not take effect, so this run proves nothing"
    )
    leaked = [
        text for text in claude_labels if _CODEX_MODEL_ID in text or _CODEX_MODEL_LABEL in text
    ]
    assert not leaked, (
        f"the Codex session's model leaked into the Claude session's composer: {leaked} "
        f"(full label sequence under the Claude route: {claude_labels}). The composer must "
        "never surface another session's sticky model while the snapshot is in flight."
    )


def test_claude_native_picker_prefers_session_override_over_sticky_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The active row follows the session override, not another session's pick."""
    page.add_init_script("window.localStorage.setItem('omnigent.picker.model', 'haiku')")
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id, model_override="opus")

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    page.get_by_test_id("composer-config-model").click()

    expect(page.locator('[role="option"][data-model-id="opus"]')).to_have_attribute(
        "data-active", "true"
    )
    expect(page.locator('[role="option"][data-model-id="haiku"]')).not_to_have_attribute(
        "data-active", "true"
    )
