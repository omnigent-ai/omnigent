"""E2E: the Claude Code model picker must match the CLI's visible model menu.

Claude Code accepts more ``/model`` aliases than its interactive picker
displays: the ``Usage: /model <name>. Available: ...`` line it prints
enumerates every *settable* alias (``fable`` included), while the visible
menu the picker actually offers under the account's configuration travels in
the initialization event's ``models`` list — including managed/custom
entries that exist nowhere in the usage line. Omnigent's discovery probe
(``omnigent.harnesses.claude_native.main``) parses only the usage line, so
its catalog can offer aliases the user's CLI menu does not show and omit
managed entries the CLI menu does show. Separately, the SPA keeps hardcoded
model catalogs (``web/src/lib/claudeNativeModels.ts``) that inject a static
alias list — Fable included — into the sandbox and settings pickers before
any host catalog exists.

Journeys guarded here:

1. A host whose Claude CLI's visible menu lists only Sonnet/Opus/Haiku plus
   one managed gateway entry — no Fable — while its usage line advertises
   ``fable``: open the session composer's Configure Claude Code model picker
   → it must not offer Fable (an alias the CLI menu never shows).
2. Same host: the picker must offer the managed entry the CLI's visible
   menu exposes.
3. A server with no host catalog at all: pick Claude Code as a project's
   default agent in Project settings → the Model default control must not
   offer a hardcoded static list (Fable) it cannot know is launchable.

The catalog for journeys 1–2 is produced by the REAL probe pipeline
(``claude_model_catalog``) against a stub ``claude`` CLI replaying exactly
the reported mismatch, so every line of Omnigent's parsing/composition code
runs for real and the test stays deterministic without a live Anthropic
account.
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.harnesses.claude_native.main import claude_model_catalog
from tests.e2e_ui.conftest import fetch_with_retry

# The stub CLI replays a Claude Code install whose interactive picker menu
# (the init event's ``models`` list — the account-visible truth) carries only
# Sonnet/Opus/Haiku plus one managed gateway entry, while the printed usage
# line still advertises ``fable`` and friends as accepted aliases.
_FAKE_CLAUDE_CLI = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json
    import sys

    VISIBLE_MODELS = [
        {"id": "claude-sonnet-5", "displayName": "Sonnet 5"},
        {"id": "claude-opus-5", "displayName": "Opus 5"},
        {"id": "claude-haiku-4-5-20251001", "displayName": "Haiku 4.5"},
        {"id": "acme-gateway-sonnet", "displayName": "Acme Managed Sonnet"},
    ]

    RESOLUTIONS = {
        "sonnet": ("claude-sonnet-5", "Sonnet 5"),
        "opus": ("claude-opus-5", "Opus 5"),
        "haiku": ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        "fable": ("claude-fable-5", "Fable 5"),
        "best": ("claude-opus-5", "Opus 5"),
        "sonnet[1m]": ("claude-sonnet-5[1m]", "Sonnet 5"),
        "opusplan": ("claude-sonnet-5", "Opus in plan mode, else Sonnet"),
        "default": ("claude-sonnet-5", "Sonnet 5 (default)"),
    }
    USAGE = (
        "Usage: /model <name>. Available: sonnet, opus, haiku, fable, best, "
        "sonnet[1m], opusplan, default, or a full model ID."
    )

    args = sys.argv[1:]
    if "--version" in args:
        print("2.1.250")
        raise SystemExit(0)
    alias = args[args.index("--model") + 1] if "--model" in args else None
    if alias is None:
        model, label = RESOLUTIONS["default"]
        text = "Current model: " + label + "\\n" + USAGE
    else:
        model, label = RESOLUTIONS.get(alias, RESOLUTIONS["default"])
        text = "Current model: " + label
    print(json.dumps({"type": "system", "subtype": "init", "model": model,
                      "models": VISIBLE_MODELS}))
    print(json.dumps({"type": "result", "result": text}))
    """
)


def _probe_catalog_from_limited_menu_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[dict[str, object]]:
    """Run the real catalog probe against the stubbed limited-menu ``claude``.

    :param monkeypatch: Used to front-load the stub's bin dir onto ``PATH``.
    :param tmp_path: Per-test dir the stub executable is written into.
    :returns: The wire-ready picker rows the probe composed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(_FAKE_CLAUDE_CLI)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # A gateway base URL inherited from the machine would reclassify the
    # endpoint and filter rows; the probed CLI is the only input under test.
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    # The e2e_ui suite already runs under an event loop, so drive the async
    # probe on its own loop in a worker thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        catalog = executor.submit(asyncio.run, claude_model_catalog(None)).result(timeout=120)
    assert catalog is not None, "the stubbed claude CLI probe must not fail"
    return catalog


def _patch_session_as_claude_native(
    page: Page,
    session_id: str,
    model_options: list[dict[str, object]],
    llm_model: str,
) -> None:
    """Reshape the browser's session snapshot into a claude-native session.

    The server fixture seeds a normal ``hello_world`` session so the page can
    boot against the real app/server; this route patch changes only the
    ``GET /v1/sessions/{session_id}`` response the browser sees, exposing the
    probed catalog rows as the session's model options.

    :param page: Playwright page, before navigation.
    :param session_id: The seeded session's id.
    :param model_options: Catalog rows the picker should render.
    :param llm_model: Bound model id reported for the session.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        payload["llm_model"] = llm_model
        payload["model_options"] = model_options
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def _open_model_picker(page: Page, base_url: str, session_id: str) -> None:
    """Navigate to the session and open the Configure Claude Code model list.

    :param page: Playwright page with the session route already patched.
    :param base_url: The live server's base URL.
    :param session_id: The seeded session's id.
    """
    page.goto(f"{base_url}/c/{session_id}")
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()
    page.get_by_test_id("composer-agent-edit").click()


def test_claude_picker_omits_help_only_aliases(
    page: Page,
    seeded_session: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The picker must not offer an alias the CLI's visible menu never shows.

    The stub CLI's visible ``models`` menu has no Fable, yet its usage line
    advertises ``fable`` as an accepted alias. The user-visible failure: the
    Configure Claude Code model picker offers a "Fable 5" row the same
    account's interactive CLI picker does not offer.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched claude-native.
    :param monkeypatch: For the stub CLI's ``PATH`` front-load.
    :param tmp_path: Where the stub CLI is written.
    """
    base_url, session_id = seeded_session
    catalog = _probe_catalog_from_limited_menu_cli(monkeypatch, tmp_path)
    _patch_session_as_claude_native(page, session_id, catalog, llm_model="claude-sonnet-5")

    _open_model_picker(page, base_url, session_id)

    rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
    # The picker rendered: the visible-menu tiers are offered.
    expect(rows.filter(has_text="Sonnet 5").first).to_be_visible(timeout=10_000)

    # ── The bug: a help-line-only alias is offered as a picker row. ──
    expect(page.locator('[role="menuitemcheckbox"][data-model-id="fable"]')).to_have_count(0)
    expect(rows.filter(has_text="Fable")).to_have_count(0)


def test_claude_picker_offers_visible_menu_managed_entry(
    page: Page,
    seeded_session: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The picker must offer managed/custom entries the CLI's menu exposes.

    The stub CLI's visible ``models`` menu carries "Acme Managed Sonnet"
    (``acme-gateway-sonnet``), an entry the usage line's alias list never
    mentions. The user-visible failure: the Configure Claude Code model
    picker omits an entry the same account's interactive CLI picker offers.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched claude-native.
    :param monkeypatch: For the stub CLI's ``PATH`` front-load.
    :param tmp_path: Where the stub CLI is written.
    """
    base_url, session_id = seeded_session
    catalog = _probe_catalog_from_limited_menu_cli(monkeypatch, tmp_path)
    _patch_session_as_claude_native(page, session_id, catalog, llm_model="claude-sonnet-5")

    _open_model_picker(page, base_url, session_id)

    rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
    expect(rows.filter(has_text="Sonnet 5").first).to_be_visible(timeout=10_000)

    # ── The bug: the visible menu's managed entry has no picker row. ──
    expect(rows.filter(has_text="Acme Managed Sonnet")).to_have_count(1)


def _create_project(base_url: str, name: str) -> str:
    """Create an empty first-class project via the API; return its id."""
    resp = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    resp.raise_for_status()
    return resp.json()["id"]


def _resolve_builtin_agent(base_url: str, name: str) -> dict:
    """Resolve a packaged built-in agent (e.g. ``claude-native-ui``) by name."""
    resp = httpx.get(f"{base_url}/v1/agents", params={"limit": 100}, timeout=30.0)
    resp.raise_for_status()
    agent = next((a for a in resp.json()["data"] if a["name"] == name), None)
    assert agent is not None, (
        f"{name} built-in not registered on the test server — it is seeded "
        "unconditionally at startup, so its absence is a server bug"
    )
    return agent


def _open_project_settings(page: Page, project: str) -> None:
    """Open the folder kebab → "Project settings" for *project*."""
    actions = page.get_by_role("button", name=f"Project actions for {project}", exact=True)
    expect(actions).to_be_visible()
    actions.click()
    page.get_by_test_id("project-settings").click()
    # The dialog's Save button confirms the editor mounted + the config fetch
    # settled (Save is disabled while loading).
    expect(page.get_by_test_id("project-settings-save")).to_be_enabled()


def _pick_default_agent(page: Page, agent_id: str) -> None:
    """In the open settings dialog, pick *agent_id* as the default agent."""
    field = page.get_by_test_id("project-settings-agent")
    trigger = field.get_by_test_id("new-chat-landing-agent-select")
    expect(trigger).to_be_enabled()
    trigger.click()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    expect(row).to_be_visible()
    row.click()


def test_project_settings_claude_model_default_is_not_hardcoded(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """With no host catalog, the settings picker must not invent model rows.

    The e2e_ui server registers no host, so no Claude catalog exists anywhere
    — exactly the pre-catalog window in which the settings dialog falls back
    to the SPA's hardcoded static alias list. The user-visible failure: the
    Model default control offers "Fable" (and the rest of the static list)
    although nothing has established that the user's CLI offers it. Codex in
    the same state honestly renders a disabled control; Claude must not
    instead serve a hardcoded guess.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session (no host registered).
    """
    base_url, session_id = seeded_session
    agent = _resolve_builtin_agent(base_url, "claude-native-ui")
    project = f"Project {uuid.uuid4().hex[:6]}"
    _create_project(base_url, project)

    page.goto(f"{base_url}/c/{session_id}")

    _open_project_settings(page, project)
    _pick_default_agent(page, agent["id"])

    trigger = page.get_by_test_id("project-settings-agent").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    expect(trigger).to_contain_text("Claude Code")

    model = page.get_by_test_id("project-settings-model")
    expect(model).to_be_visible(timeout=5_000)
    if model.is_enabled():
        model.click()
        # The dropdown opened: its honest sentinel row is present. The option
        # row may render as a Radix Select option or a dropdown menu item.
        sentinel = page.get_by_role("option", name="No default", exact=True)
        if sentinel.count() == 0:
            sentinel = page.get_by_role("menuitem", name="No default", exact=True)
        expect(sentinel.first).to_be_visible()

        # ── The bug: the static fallback list (Fable first) is offered. ──
        for role in ("option", "menuitem"):
            expect(page.get_by_role(role, name="Fable", exact=True)).to_have_count(0)
    # A disabled control (Codex's honest empty-catalog rendering) offers
    # nothing, which is the expected fixed behavior — nothing more to assert.
