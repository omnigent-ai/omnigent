"""E2E: the New Chat picker offers Devin with its own model + effort lists.

Opening Devin's config submenu in the New Chat picker must surface:

* Devin's model **families** (from the host's ``devin-native`` catalog probe) —
  not Claude's or Pi's list; and
* an Effort ladder, because Devin has no ``--effort`` flag and Omnigent composes
  the (model, effort) pair into one variant id at launch
  (``resolve_devin_launch_model``). Without the ladder rendered there is no way
  to express effort when starting a chat.

Regression target: Devin declares only the ``devinMode`` capability, so the
model + effort sections hang off that flag alone. Both the config-content gate
(``selectedAgentHasKnobs``) and the models-section gate must honour it, or the
config submenu (and with it every model/effort control) never renders — the
``agent-config-*`` Edit entry ``_open_entry_models`` clicks would not even exist.

Drives the picker through the shared ``_open_entry_models`` helper so it opens
the config submenu the same way the passing Pi/Codex picker tests do, rather
than re-deriving the menu navigation here.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.start_session.test_start_session import (
    _HOST_ID,
    _agents_body,
    _close_entry_models,
    _hosts_body,
    _open_entry_models,
    _register_common_routes,
    _run_in_fresh_loop,
    _wait_until,
)

_DEVIN_AGENT_ID = "ag_devin_e2e"

# Devin's Fusion option, as `list_devin_cli_model_options` emits it: a `fusion`
# descriptor pairing a lead (family + effort) with a sidekick. Only real combos
# are listed, so the picker's Lead/Effort/Sidekick selectors offer just these.
_FUSION_OPTION = {
    "id": "fusion",
    "displayName": "Fusion",
    "isDefault": False,
    "fusion": {
        "default": "fusion-claude-fable-5-1-medium-sidekick-swe-2-medium",
        "combos": [
            {
                "modelUid": "fusion-claude-fable-5-1-medium-sidekick-swe-2-medium",
                "lead": "claude-fable-5.1",
                "leadLabel": "Claude Fable 5.1",
                "effort": "medium",
                "fast": False,
                "sidekick": "swe-2-medium",
                "sidekickLabel": "SWE-2 Medium",
                "priority": False,
            },
            {
                "modelUid": "fusion-claude-fable-5-1-medium-sidekick-swe-2-high",
                "lead": "claude-fable-5.1",
                "leadLabel": "Claude Fable 5.1",
                "effort": "medium",
                "fast": False,
                "sidekick": "swe-2-high",
                "sidekickLabel": "SWE-2 High",
                "priority": False,
            },
        ],
    },
}

# Devin model *families* (claude-opus-5, swe-2, …), the shape
# ``list_devin_cli_model_options`` returns. Effort is a separate axis, so no
# variant suffixes appear here.
# Effort rungs are PER MODEL, mirroring what `list_devin_cli_model_options`
# reports: swe-2 exposes only medium/high/max (`swe-2-low` is a different Fusion
# model), while claude-opus-5 carries the full ladder.
_DEVIN_MODELS = [
    {
        "id": "claude-opus-5",
        "displayName": "Claude Opus 5",
        "isDefault": False,
        "supportedReasoningEfforts": [
            {"reasoningEffort": rung} for rung in ("low", "medium", "high", "xhigh", "max")
        ],
    },
    {
        "id": "swe-2",
        "displayName": "SWE-2",
        "isDefault": True,
        "supportedReasoningEfforts": [
            {"reasoningEffort": rung} for rung in ("medium", "high", "max")
        ],
    },
]


def _devin_native_agents_body() -> str:
    """Stub ``GET /v1/agents``: the native Devin agent as the sole built-in.

    ``name: "devin-native-ui"`` + ``harness: "devin-native"`` is what the
    frontend maps (via ``nativeCodingAgents``) to the ``devinMode`` capability
    that gates Devin's model + effort rows. Sole agent, so it auto-selects and
    no explicit pick is needed before opening its config.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": _DEVIN_AGENT_ID,
                    "name": "devin-native-ui",
                    "display_name": "Devin",
                    "description": "Cognition's coding agent",
                    "harness": "devin-native",
                    "skills": [],
                }
            ]
        }
    )


@pytest.mark.parametrize("initially_ready", [True, False])
def test_devin_picker_offers_its_own_models_and_effort(
    seeded_session: tuple[str, str],
    initially_ready: bool,
) -> None:
    """Only the selected, configured harness fetches and refreshes its catalog.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive(base_url, session_id, initially_ready=initially_ready))


async def _drive(base_url: str, session_id: str, *, initially_ready: bool) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            agents = json.loads(_agents_body())
            agents["data"].extend(json.loads(_devin_native_agents_body())["data"])
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=[],
                agents_body=json.dumps(agents),
            )

            devin_requests: list[str] = []
            hosts = json.loads(_hosts_body())
            readiness = {
                "claude-native": True,
                "codex-native": True,
                "devin-native": initially_ready,
            }
            hosts["hosts"][0]["configured_harnesses"] = readiness

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(hosts)
                )

            await page.route("**/v1/hosts", handle_hosts)

            async def assert_devin_idle(expected_count: int | None = None) -> None:
                # Pause ambient polling and drain earlier requests before the baseline.
                await page.clock.pause_at(await page.evaluate("Date.now() / 1_000 + 1"))
                try:
                    await page.wait_for_load_state("networkidle")
                    requests_before = len(devin_requests)
                    if expected_count is not None:
                        assert requests_before == expected_count
                    # Claude's completed poll proves timer-triggered requests reached the host.
                    async with page.expect_response(
                        lambda response: response.url.endswith(
                            f"/hosts/{_HOST_ID}/harnesses/claude-native/model-options"
                        )
                    ) as polled:
                        await page.clock.fast_forward(30_000)
                    await (await polled.value).finished()
                    await page.wait_for_load_state("networkidle")
                    assert len(devin_requests) == requests_before
                finally:
                    await page.clock.resume()

            async def handle_devin_models(route: Route) -> None:
                devin_requests.append(route.request.url)
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": _DEVIN_MODELS}),
                )

            async def handle_agent_scan(route: Route) -> None:
                # Keep sessions from other tests out of the stubbed agent list.
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/devin-native/model-options",
                handle_devin_models,
            )
            await page.route(
                re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine"), handle_agent_scan
            )

            # A host workspace exercises readiness-aware catalog discovery.
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );
                window.localStorage.setItem(
                    "omnigent:last-mode-by-harness",
                    JSON.stringify({{ "devin-native": {{ model: "swe-2" }} }})
                );"""
            )

            await page.clock.install()
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
                "aria-label", re.compile(r"^Claude Code,")
            )
            await assert_devin_idle(0)

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-harness-more").click()
            await expect(
                page.get_by_test_id(f"new-chat-landing-agent-summary-{_DEVIN_AGENT_ID}")
            ).to_have_text("swe-2")
            await assert_devin_idle(0)
            if not initially_ready:
                row = page.get_by_test_id(f"new-chat-landing-agent-{_DEVIN_AGENT_ID}")
                await expect(row).to_have_attribute("aria-disabled", "true")
                await row.locator("span.truncate").first.hover()
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-tooltip-{_DEVIN_AGENT_ID}")
                ).to_contain_text(
                    "Devin isn't configured on e2e-host — run omni setup on that machine."
                )
                assert not devin_requests
                return
            await (
                page.get_by_test_id(f"new-chat-landing-agent-config-{_DEVIN_AGENT_ID}")
                .get_by_text("Edit", exact=True)
                .click()
            )

            # Devin's own families render, from the devin-native catalog probe.
            models = page.get_by_test_id("new-chat-landing-agent-models")
            await expect(models).to_be_visible(timeout=30_000)
            for model in _DEVIN_MODELS:
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-model-{model['id']}")
                ).to_be_visible()
            assert devin_requests
            requests_before = len(devin_requests)
            await page.clock.fast_forward(30_000)
            await _wait_until(lambda: len(devin_requests) > requests_before)

            # The Effort ladder renders, carrying only the DEFAULT model's rungs.
            # Devin has no --effort flag, so this is the only way to express effort
            # when starting a chat; the runner composes it onto the model id at
            # launch — and offering a rung the model lacks would compose an id
            # Devin resolves back to the bare family, so the pick would look inert.
            await expect(page.get_by_test_id("new-chat-landing-agent-efforts")).to_be_visible()
            for rung in ("medium", "high", "max"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-effort-{rung}")
                ).to_be_visible()
            for rung in ("low", "xhigh"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-effort-{rung}")
                ).to_have_count(0)

            # A model + effort pick sticks, which is what the create call sends as
            # model_override + reasoning_effort. Switching to a model with the full
            # ladder widens the rungs, which is the per-model derivation working.
            await page.get_by_test_id("new-chat-landing-agent-model-claude-opus-5").click()
            for rung in ("low", "xhigh"):
                await expect(
                    page.get_by_test_id(f"new-chat-landing-agent-effort-{rung}")
                ).to_be_visible()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-model-claude-opus-5")
            ).to_have_attribute("data-state", "checked")
            await page.get_by_test_id("new-chat-landing-agent-effort-xhigh").click()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-effort-xhigh")
            ).to_have_attribute("data-state", "checked")

            await _close_entry_models(page)
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
                "aria-expanded", "false"
            )
            await page.get_by_test_id("new-chat-landing-agent-select").press("ArrowDown")
            await page.get_by_test_id("new-chat-landing-agent-ag_claude_e2e").click()
            await expect(page.get_by_test_id("new-chat-landing-agent-select")).to_have_attribute(
                "aria-label", re.compile(r"^Claude Code,")
            )
            await assert_devin_idle()
        finally:
            await browser.close()


def test_devin_picker_fusion_lead_and_sidekick(
    seeded_session: tuple[str, str],
) -> None:
    """Selecting Fusion reveals Lead/Sidekick selectors and sends the composed id.

    :param seeded_session: ``(base_url, session_id)`` from the spawned server.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_fusion(base_url, session_id))


async def _drive_fusion(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        create_bodies: list[dict] = []
        try:
            await _register_common_routes(
                page,
                created_session_id=session_id,
                create_bodies=create_bodies,
                agents_body=_devin_native_agents_body(),
            )

            async def handle_devin_models(route: Route) -> None:
                # swe-2 stays the default family; Fusion is a second option.
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"models": [*_DEVIN_MODELS, _FUSION_OPTION]}),
                )

            async def handle_agent_scan(route: Route) -> None:
                await route.fulfill(
                    status=200, content_type="application/json", body=json.dumps({"data": []})
                )

            await page.route(
                f"**/v1/hosts/{_HOST_ID}/harnesses/devin-native/model-options",
                handle_devin_models,
            )
            await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
                );"""
            )

            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            await _open_entry_models(page, _DEVIN_AGENT_ID)

            # Picking Fusion reveals the Lead / Effort / Sidekick sections.
            await page.get_by_test_id("new-chat-landing-agent-model-fusion").click()
            await expect(page.get_by_test_id("new-chat-landing-agent-fusion-leads")).to_be_visible(
                timeout=30_000
            )
            await expect(
                page.get_by_test_id("new-chat-landing-agent-fusion-lead-claude-fable-5.1")
            ).to_be_visible()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-fusion-sidekicks")
            ).to_be_visible()

            # Switching the sidekick composes the exact fusion variant id.
            await page.get_by_test_id("new-chat-landing-agent-fusion-sidekick-swe-2-high").click()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-fusion-sidekick-swe-2-high")
            ).to_have_attribute("data-state", "checked")

            await page.get_by_test_id("new-chat-landing-input").fill("build it with fusion")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await _wait_until(lambda: len(create_bodies) == 1)
            body = create_bodies[0]
            expected_uid = "fusion-claude-fable-5-1-medium-sidekick-swe-2-high"
            assert body["model_override"] == expected_uid, body
            # The lead effort is baked into the id, so no separate effort is sent.
            assert "reasoning_effort" not in body, body
        finally:
            await browser.close()
