"""E2E: a configured ``acp:<slug>`` agent must not be badged "needs setup".

A user-configured generic-ACP
agent (an ``acp:`` config block, e.g. TraeX -> harness ``acp:traex``) launches
fine but the New Chat picker wrongly warns it "isn't configured on <host> -- run
omni setup", because the host's readiness map omits the ``acp:<slug>`` key.

The landing composer (``NewChatLandingScreen`` in ``web/src/shell/NewChatDialog.tsx``)
surfaces an under-composer notice (``new-chat-landing-harness-warning``) for the
selected agent when ``harnessUnavailableReasonOnHost`` (``web/src/lib/harnessSetup.ts``)
judges its harness unconfigured on the selected host. That helper returns
``"unconfigured"`` when the harness key is **absent** from a **non-empty**
``configured_harnesses`` map. The daemon builds that map with
``omnigent.onboarding.harness_readiness.configured_harness_map``; before it
enumerated the user-configured ``acp:<slug>`` slugs, ``acp: True`` was present
but ``acp:traex`` was not, and the seeded TraeX row was judged unconfigured
even though it launches.

Why the ``page.route`` stubbing and the async-in-a-fresh-thread shape: both are
inherited from ``chat/test_codex_auth_availability.py``. The e2e harness's runner
tunnels into the server and registers no *host*, so faking ``/v1/hosts`` (with
``configured_harnesses``) and ``/v1/agents`` is the established way to drive the
landing picker. Crucially, the ``configured_harnesses`` map fed to the stub is
**not hand-written**: it is the genuine output of the production
``configured_harness_map()`` computed under an ``acp:``-configured
``OMNIGENT_CONFIG_HOME`` (in an isolated subprocess), so the stub carries the
exact keyspace the daemon actually reports.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from playwright.async_api import Route, async_playwright, expect

# Stubbed host the composer auto-selects (the tunneled runner registers no
# host). Keyed identically in the recent-workspaces localStorage seed.
_HOST_ID = "host_e2e"
_HOST_NAME = "e2e-host"

# The report's configured generic-ACP agent: display name TraeX -> slug traex
# -> harness id acp:traex. One agent seeds one picker row keyed on that harness.
_ACP_SLUG = "traex"
_ACP_HARNESS = f"acp:{_ACP_SLUG}"
_ACP_AGENT_ID = "ag_traex_e2e"
_ACP_DISPLAY_NAME = "TraeX"

_ACP_CONFIG_YAML = """\
acp:
  agents:
    - name: TraeX
      command: traex acp serve
"""


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception (including assertion failures) is captured and
    re-raised on the calling thread so the test fails normally.

    :param coro: The coroutine to run to completion.
    :raises Exception: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _real_configured_harness_map() -> dict[str, Any]:
    """Return the production readiness map for a host with the report's acp: block.

    Runs ``configured_harness_map()`` in an isolated subprocess under an
    ``OMNIGENT_CONFIG_HOME`` holding the report's ``acp:`` config, so the map is
    the genuine wire shape the daemon reports for a TraeX-configured host, not
    a fabricated dict.

    :returns: The parsed ``configured_harnesses`` map (harness spelling -> bool
        or reason string).
    """
    script = textwrap.dedent(
        """
        import json
        from omnigent.onboarding.harness_readiness import configured_harness_map
        print(json.dumps(configured_harness_map()))
        """
    )
    with tempfile.TemporaryDirectory() as home:
        (Path(home) / "config.yaml").write_text(_ACP_CONFIG_YAML)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            # Inherit the full environment (venv/import resolution) and only
            # point config discovery at the isolated acp:-configured home.
            env={**os.environ, "OMNIGENT_CONFIG_HOME": home},
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    return json.loads(proc.stdout)


def _hosts_body(configured_harnesses: dict[str, Any]) -> str:
    """Stub body for ``GET /v1/hosts``: one online host the composer picks.

    :param configured_harnesses: The host's per-harness readiness map, mirroring
        what the ``host.hello`` readiness map produces.
    """
    return json.dumps(
        {
            "hosts": [
                {
                    "host_id": _HOST_ID,
                    "name": _HOST_NAME,
                    "owner": "e2e",
                    "status": "online",
                    "configured_harnesses": configured_harnesses,
                }
            ]
        }
    )


def _acp_agents_body() -> str:
    """Stub body for ``GET /v1/agents``: the sole configured TraeX ACP agent.

    ``harness: "acp:traex"`` is what the frontend treats as a generic-ACP row
    (``isAcpHarnessAgent``), the row keyspace the readiness map is filtered
    against. Sole agent, so it auto-selects and the under-composer notice renders
    without opening the picker.
    """
    return json.dumps(
        {
            "data": [
                {
                    "id": _ACP_AGENT_ID,
                    "name": _ACP_SLUG,
                    "display_name": _ACP_DISPLAY_NAME,
                    "description": "Generic ACP agent",
                    "harness": _ACP_HARNESS,
                    "skills": [],
                }
            ]
        }
    )


async def _register_routes(page: Any, *, configured_harnesses: dict[str, Any]) -> None:
    """Register the host/agent stubs and neutralize agent discovery.

    :param page: The Playwright page to install routes on.
    :param configured_harnesses: Readiness map for the stubbed host.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=_hosts_body(configured_harnesses),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(status=200, content_type="application/json", body=_acp_agents_body())

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize agent discovery so only the stubbed TraeX agent feeds the
        # picker. On the shared e2e_ui server, sessions other tests left behind
        # would otherwise leak in and -- ranking ahead -- auto-select, swapping
        # the selected harness out from under the assertion.
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)


async def _open_landing(page: Any, base_url: str) -> None:
    """Seed a recent workspace, load the landing screen, wait for the composer.

    :param page: The Playwright page.
    :param base_url: The live server base URL.
    """
    await page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)


def test_configured_acp_slug_agent_is_not_badged_needs_setup(
    seeded_session: tuple[str, str],
) -> None:
    """A configured ``acp:<slug>`` agent must not warn "isn't configured" on its host.

    Two phases drive the reason-driven notice end to end against the rendered
    landing screen:

    1. **configured phase** -- the host reports the genuine
       ``configured_harness_map()`` for a TraeX-configured machine. The selected
       TraeX agent must NOT show the under-composer "isn't configured" notice,
       because the harness is configured and launchable. Before the map
       enumerated ``acp:<slug>`` keys, the seeded row's key was absent from a
       non-empty map and the notice rendered (the reported false negative).
    2. **liveness control** -- the same host explicitly reports
       ``acp:traex: False``; the notice MUST appear. This proves the notice path
       fires for this agent and host, so the bug-phase "no notice" assertion is a
       real check and not a false pass from a non-rendering picker.
    """
    base_url, session_id = seeded_session
    del session_id  # this flow never creates a session -- only reads the picker

    real_map = _real_configured_harness_map()
    # Sanity: the genuine map for a TraeX-configured host is non-empty, generic
    # acp is ready, and the configured slug carries its own available key -- a
    # map that omits the slug key is what badges the launchable agent
    # "needs setup".
    assert real_map, "production configured_harness_map() returned an empty map"
    assert real_map.get("acp"), (
        f"expected generic 'acp' readiness to be available with a configured agent, "
        f"got {real_map.get('acp')!r}"
    )
    assert real_map.get(_ACP_HARNESS) is True, (
        f"expected the configured {_ACP_HARNESS!r} slug key to be available in the "
        f"readiness map, got {real_map.get(_ACP_HARNESS)!r} -- the map no longer "
        "enumerates user-configured acp:<slug> agents"
    )

    _run_in_fresh_loop(_drive_acp_slug_badge(base_url, real_map))


async def _drive_acp_slug_badge(base_url: str, real_map: dict[str, Any]) -> None:
    # configured phase: the genuine readiness map for a TraeX-configured host.
    # The configured, launchable agent must not warn.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so context.close() flushes any recorded video before
        # the browser tears down (a page-only close can drop the .webm).
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _register_routes(page, configured_harnesses=real_map)
            await _open_landing(page, base_url)

            # TraeX auto-selects (sole agent); its harness must read as
            # configured, so no under-composer notice. count()==0 (not "hidden")
            # because the notice is conditionally rendered, never just hidden.
            await expect(page.get_by_test_id("new-chat-landing-harness-warning")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()

    # liveness control: the same host explicitly reports the slug unconfigured
    # (False). The notice MUST render -- proving the picker rendered the agent
    # and the notice path fires, so the assertion above is meaningful.
    control_map = {**real_map, _ACP_HARNESS: False}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await _register_routes(page, configured_harnesses=control_map)
            await _open_landing(page, base_url)

            notice = page.get_by_test_id("new-chat-landing-harness-warning")
            await expect(notice).to_be_visible(timeout=30_000)
            await expect(notice).to_contain_text(_HOST_NAME)
        finally:
            await context.close()
            await browser.close()
