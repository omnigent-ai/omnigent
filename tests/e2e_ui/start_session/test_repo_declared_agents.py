"""New Chat flows for repo-declared agents (`.omnigent/` discovery).

Covers the three user-facing behaviors this feature adds to the landing
composer:

- discovery rows: agents a workspace's repo declares (ACP commands from
  ``.omnigent/config.yaml``, agent configs from ``.omnigent/agent-configs/``)
  lead their picker groups with a "From this repo" badge;
- first-use consent: launching a repo-declared agent always shows exactly
  what will run (the shell command, or the packaged definition's summary +
  digest) before anything is created, and an approval never outlives the
  pick it was granted for;
- the freeze guarantee: the bundle bytes uploaded on confirm are byte-
  identical to the bytes the consent dialog hashed, so a repo edit between
  consent and create cannot swap the content.

The shared e2e_ui server registers no host daemon (see conftest), so —
following the ``test_start_session`` stub strategy — every host-scoped
endpoint the flows touch is intercepted per-test: ``/v1/hosts``, the
``workspace-harnesses`` discovery read, the ``workspace-agents/package``
packaging call, the multipart ``POST /v1/sessions`` create, and the
``POST /v1/hosts/{id}/runners`` launch. The create is captured as raw
bytes (it is multipart, not JSON) and answered with a real pre-seeded
session id so the post-send navigation lands on a real page.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import re
import tarfile
import threading
from typing import Any

import pytest
from playwright.async_api import Route, async_playwright, expect

_HOST_ID = "host_e2e_repo"
_WORKSPACE = "/work/demo-repo"

_ACP_ROW = {
    "slug": "repo-echo",
    "name": "Repo Echo",
    "command": "echo-agent --acp --flag",
    "model": None,
    "session_id_mode": "server",
    "send_model": False,
    "omnigent_mcp": True,
    "command_found": True,
}
_CONFIG_ROW = {
    "slug": "repo-reviewer",
    "name": "repo-reviewer",
    "path": ".omnigent/agent-configs/reviewer",
    "kind": "bundle",
    "description": "Reviews changes in this repo.",
    "harness": "claude-sdk",
    "sub_agents": ["worker"],
    "has_local_tools": True,
    "has_mcp_servers": False,
}


def _run_in_fresh_loop(coro) -> None:
    """Run *coro* in a dedicated thread with its own event loop.

    Same rationale as ``test_start_session._run_in_fresh_loop``: once a sync
    pytest-playwright test has run, pytest-asyncio can't start a loop on the
    main thread, and the request-interception these tests need is only
    ergonomic on the async API.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 — re-raised on the test thread
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _bundle_bytes() -> bytes:
    """Deterministic tar.gz the packaging stub returns.

    Content is irrelevant to the flow (the create is intercepted before any
    server-side validation); what matters is that the exact bytes can be
    recognized inside the captured multipart upload.
    """
    config = b"spec_version: 1\nname: repo-reviewer\n"
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        info = tarfile.TarInfo("config.yaml")
        info.size = len(config)
        tar.addfile(info, io.BytesIO(config))
    return buf.getvalue()


async def _register_routes(
    page,
    *,
    created_session_id: str,
    create_buffers: list[bytes],
    launch_bodies: list[dict[str, Any]],
    package_bytes: bytes,
    package_calls: list[dict[str, Any]],
) -> None:
    """Install the host/discovery/create/launch stubs these flows touch.

    :param created_session_id: Real pre-seeded session id the faked create
        returns, so post-send navigation lands on a real page.
    :param create_buffers: Sink for the raw multipart ``POST /v1/sessions``
        bodies (bytes — the byte-identity assertions read these).
    :param launch_bodies: Sink for ``POST /v1/hosts/{id}/runners`` bodies.
    :param package_bytes: The tar.gz the packaging endpoint stub returns.
    :param package_calls: Sink for the packaging request bodies.
    """

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": "e2e-repo-host",
                            "owner": "e2e",
                            "status": "online",
                        }
                    ]
                }
            ),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
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
            ),
        )

    async def handle_agent_scan(route: Route) -> None:
        # Neutralize session-derived agent discovery so only the stubs feed
        # the picker (see test_start_session for why leakage matters).
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"data": []})
        )

    async def handle_discovery(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"agents": [_ACP_ROW], "agent_configs": [_CONFIG_ROW]}),
        )

    async def handle_package(route: Route) -> None:
        package_calls.append(route.request.post_data_json)
        await route.fulfill(status=200, content_type="application/gzip", body=package_bytes)

    async def handle_sessions(route: Route) -> None:
        # Capture ONLY the composer's create POST — as raw bytes, because the
        # repo-agent paths upload multipart bundles, not JSON.
        if route.request.method == "POST":
            create_buffers.append(route.request.post_data_buffer or b"")
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id, "session_id": created_session_id}),
            )
        else:
            await route.continue_()

    async def handle_launch(route: Route) -> None:
        launch_bodies.append(route.request.post_data_json)
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runner_id": "runner_token_e2e", "status": "launching"}),
        )

    async def handle_events(route: Route) -> None:
        # Swallow the auto-sent initial prompt so no real LLM turn runs.
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
        )

    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(re.compile(r"/v1/sessions\?.*kind=any"), handle_agent_scan)
    await page.route(re.compile(r"/v1/hosts/[^/]+/workspace-harnesses"), handle_discovery)
    await page.route(re.compile(r"/v1/hosts/[^/]+/workspace-agents/package"), handle_package)
    await page.route(re.compile(r"/v1/sessions$"), handle_sessions)
    await page.route(re.compile(r"/v1/hosts/[^/]+/runners"), handle_launch)
    await page.route("**/v1/sessions/*/events", handle_events)


async def _open_landing(page, base_url: str) -> None:
    """Seed host + workspace prefs and open the landing composer."""
    await page.add_init_script(
        f"""
        window.localStorage.setItem("omnigent:last-host-choice", "{_HOST_ID}");
        window.localStorage.setItem(
          "omnigent:recent-workspaces",
          JSON.stringify({{ "{_HOST_ID}": ["{_WORKSPACE}"] }})
        );
        """
    )
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)


def _gunzip_first_member(buffer: bytes) -> bytes:
    """Decompress the first gzip stream inside a multipart body."""
    start = buffer.find(b"\x1f\x8b")
    assert start >= 0, "no gzip stream found in captured create body"
    import zlib

    return zlib.decompressobj(wbits=31).decompress(buffer[start:])


@pytest.mark.timeout(300)
def test_repo_declared_agents_rank_in_picker(seeded_session: tuple[str, str]) -> None:
    """Discovered repo agents lead their picker groups, badged with provenance.

    A workspace whose repo declares an ACP command and an agent config must
    surface both in the agent picker — the command in the Harnesses group,
    the config in the Agents group — each carrying a "From this repo" badge,
    with nothing auto-selected (the built-in stays the default pick).
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_picker_rows(base_url, session_id))


async def _drive_picker_rows(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            await _register_routes(
                page,
                created_session_id=session_id,
                create_buffers=[],
                launch_bodies=[],
                package_bytes=_bundle_bytes(),
                package_calls=[],
            )
            await _open_landing(page, base_url)

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-repo-acp:repo-echo")
            ).to_be_visible()
            await expect(
                page.get_by_test_id("new-chat-landing-agent-repo-cfg:repo-reviewer")
            ).to_be_visible()
            await expect(
                page.get_by_test_id("new-chat-landing-repo-badge-repo-echo")
            ).to_have_text("From this repo")
            await expect(
                page.get_by_test_id("new-chat-landing-repo-cfg-badge-repo-reviewer")
            ).to_have_text("From this repo")
        finally:
            await browser.close()


@pytest.mark.timeout(300)
def test_repo_acp_consent_gates_create(seeded_session: tuple[str, str]) -> None:
    """The ACP consent dialog gates the create and binds to the exact pick.

    Sending with a repo-declared command selected must open the consent
    dialog showing the exact shell string, with nothing created yet.
    Cancelling one agent's dialog and switching to another repo agent must
    re-prompt (an approval never outlives its pick — the regression guard
    for the one-shot-flag leak). Confirming must produce exactly one
    multipart create whose bundle embeds the consented command, followed by
    a runner launch against the selected host and workspace.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_acp_consent(base_url, session_id))


async def _drive_acp_consent(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_buffers: list[bytes] = []
            launch_bodies: list[dict[str, Any]] = []
            await _register_routes(
                page,
                created_session_id=session_id,
                create_buffers=create_buffers,
                launch_bodies=launch_bodies,
                package_bytes=_bundle_bytes(),
                package_calls=[],
            )
            await _open_landing(page, base_url)

            # Open the config agent's dialog first, then cancel — the later
            # ACP pick must still be gated by its own consent.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-agent-repo-cfg:repo-reviewer").click()
            await page.get_by_test_id("new-chat-landing-input").fill("review the changes")
            await page.get_by_test_id("new-chat-landing-submit").click()
            await expect(page.get_by_test_id("repo-config-consent-summary")).to_be_visible()
            await page.get_by_role("button", name="Cancel", exact=True).click()

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-agent-repo-acp:repo-echo").click()
            await page.get_by_test_id("new-chat-landing-submit").click()
            dialog = page.get_by_test_id("repo-harness-consent-dialog")
            await expect(dialog).to_be_visible()
            await expect(dialog.locator("pre")).to_have_text(_ACP_ROW["command"])
            assert create_buffers == [], "consent must gate the create"

            await page.get_by_test_id("repo-harness-consent-confirm").click()
            await page.wait_for_url(re.compile(r"/c/"), timeout=30_000)

            assert len(create_buffers) == 1, "confirm must create exactly one session"
            config_yaml = _gunzip_first_member(create_buffers[0])
            assert b"acp_agent:" in config_yaml
            assert _ACP_ROW["command"].encode() in config_yaml
            assert launch_bodies and launch_bodies[0]["session_id"] == session_id
            assert launch_bodies[0]["workspace"] == _WORKSPACE
        finally:
            await browser.close()


@pytest.mark.timeout(300)
def test_repo_agent_config_upload_matches_consented_bytes(
    seeded_session: tuple[str, str],
) -> None:
    """The bundle uploaded on confirm is byte-identical to what was consented.

    Selecting a repo-declared agent config and sending must call the
    host-side packaging endpoint, show the consent summary (kind, harness,
    sub-agents, digest), and — on confirm — upload the exact bytes the
    packaging stub returned inside the multipart create. This pins the
    feature's core trust property: what the user approved is what runs.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_config_consent(base_url, session_id))


async def _drive_config_consent(base_url: str, session_id: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            create_buffers: list[bytes] = []
            launch_bodies: list[dict[str, Any]] = []
            package_calls: list[dict[str, Any]] = []
            package_bytes = _bundle_bytes()
            await _register_routes(
                page,
                created_session_id=session_id,
                create_buffers=create_buffers,
                launch_bodies=launch_bodies,
                package_bytes=package_bytes,
                package_calls=package_calls,
            )
            await _open_landing(page, base_url)

            await page.get_by_test_id("new-chat-landing-agent-select").click()
            await page.get_by_test_id("new-chat-landing-agent-repo-cfg:repo-reviewer").click()
            await page.get_by_test_id("new-chat-landing-input").fill("review the changes")
            await page.get_by_test_id("new-chat-landing-submit").click()

            summary = page.get_by_test_id("repo-config-consent-summary")
            await expect(summary).to_be_visible()
            await expect(summary).to_contain_text("agent bundle")
            await expect(summary).to_contain_text("claude-sdk")
            await expect(summary).to_contain_text("worker")
            await expect(summary).to_contain_text("digest")
            assert package_calls and package_calls[0] == {
                "path": _WORKSPACE,
                "config_path": _CONFIG_ROW["path"],
            }
            assert create_buffers == [], "consent must gate the create"

            await page.get_by_test_id("repo-harness-consent-confirm").click()
            await page.wait_for_url(re.compile(r"/c/"), timeout=30_000)

            assert len(create_buffers) == 1
            assert package_bytes in create_buffers[0], (
                "uploaded bundle must be byte-identical to the consented bytes"
            )
            assert launch_bodies and launch_bodies[0]["workspace"] == _WORKSPACE
        finally:
            await browser.close()
