"""Settings → Harnesses signs in to OAuth MCP servers through the real harness CLIs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, Route, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.host import Host, isolated_host
from tests._helpers.oauth_mcp_server import OAuthMcpServer, oauth_mcp_server

# Older servers show Needs auth without a Sign in button.
pytestmark = pytest.mark.min_server_version("0.22.0")

SIGNED_IN_NOTE = re.compile(r"Signed in through (Claude Code|Codex), so its sessions can use")


def _cli(name: str) -> str:
    path = os.environ.get(f"OMNIGENT_{name.upper()}_PATH") or shutil.which(name)
    if not path:
        pytest.skip(f"{name} CLI required for a real MCP sign-in")
    return path


def _harness_env(home: Path, harness: str, binary: str, llm_url: str) -> dict[str, str | None]:
    return {
        "OMNIGENT_RUNNER_ZYGOTE": "0",
        f"OMNIGENT_{harness.upper()}_PATH": binary,
        "CLAUDE_CONFIG_DIR": None,
        "CODEX_HOME": str(home / ".codex"),
        # Placeholder model keys mark the harness set up. The CLIs also call their
        # model API from `mcp` commands, so keep that traffic on the local mock.
        "ANTHROPIC_API_KEY": "mock-key",
        "ANTHROPIC_BASE_URL": llm_url,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{llm_url}/v1",
    }


def _run(argv: list[str], home: Path) -> None:
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex")}
    env.pop("CLAUDE_CONFIG_DIR", None)
    subprocess.run(argv, env=env, cwd=home, check=True, capture_output=True, timeout=60)


@contextmanager
def _host(
    tmp_path: Path, live_server: str, llm_url: str, harness: str, binary: str
) -> Iterator[Host]:
    home = tmp_path / "host" / "home"
    with (
        httpx.Client(base_url=live_server, timeout=30) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            name="dev-box",
            env=_harness_env(home, harness, binary, llm_url),
        ) as host,
    ):
        host.wait_online(client, timeout=60, poll_interval=0.2)
        yield host


def _open_settings(
    request: pytest.FixtureRequest, live_server: str, host: Host, path: str
) -> Page:
    # Requested after host setup so a recording starts at the user's first page.
    page = request.getfixturevalue("page")
    page.add_init_script(
        f"localStorage.setItem('omnigent:imports-reviewed:{host.host_id}', 'true')"
    )
    page.goto(f"{live_server}/settings/harnesses/{path}?host={host.host_id}")
    return page


def _expect_needs_sign_in(page: Page, server: str) -> Locator:
    row = page.get_by_test_id(f"catalog-row-{server}")
    expect(row).to_be_visible(timeout=60_000)
    row.click()
    expect(row.get_by_role("status")).to_have_text("Needs auth", timeout=30_000)
    sign_in = page.get_by_role("button", name="Sign in", exact=True)
    expect(sign_in).to_be_visible()
    return row


def _approve_in_new_tab(page: Page) -> Page:
    dialog = page.get_by_role("dialog")
    open_link = dialog.get_by_role("link", name="Open sign-in page")
    expect(open_link).to_be_visible(timeout=45_000)
    with page.expect_popup() as popup_info:
        open_link.click()
    popup = popup_info.value
    expect(popup.get_by_role("heading", name="Authorize Demo Issues")).to_be_visible(
        timeout=15_000
    )
    popup.get_by_role("button", name="Approve").click()
    return popup


def _close_when_done(popup: Page) -> None:
    try:
        popup.wait_for_event("close", timeout=4_000)
    except PlaywrightTimeoutError:
        popup.close()


def _expect_signed_in(page: Page, row: Locator, server: OAuthMcpServer) -> None:
    expect(page.get_by_role("dialog")).to_be_hidden(timeout=30_000)
    expect(row.get_by_role("status")).to_have_text("Signed in", timeout=30_000)
    expect(page.get_by_text(SIGNED_IN_NOTE)).to_be_visible()
    expect(page.get_by_role("button", name="Sign in", exact=True)).to_have_count(0)
    assert server.tokens_issued == 1


def _loopback_sign_in(page: Page, row: Locator, server: OAuthMcpServer) -> None:
    page.get_by_role("button", name="Sign in", exact=True).click()
    popup = _approve_in_new_tab(page)
    # The harness's loopback listener answers the redirect; its page may close the tab.
    _close_when_done(popup)
    _expect_signed_in(page, row, server)


def test_claude_user_mcp_sign_in(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    claude = _cli("claude")
    home = tmp_path / "host" / "home"
    home.mkdir(parents=True)
    with oauth_mcp_server() as server:
        _run(
            [claude, "mcp", "add", "-s", "user", "--transport", "http", "issues", server.url], home
        )
        with _host(tmp_path, live_server, mock_llm_server_url, "claude", claude) as host:
            page = _open_settings(request, live_server, host, "claude-native")
            row = _expect_needs_sign_in(page, "issues")
            _loopback_sign_in(page, row, server)
            page.context.close()


def test_claude_plugin_mcp_sign_in(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    claude = _cli("claude")
    home = tmp_path / "host" / "home"
    home.mkdir(parents=True)
    market = tmp_path / "market"
    plugin = market / "plugins" / "tracker"
    (market / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin").mkdir(parents=True)
    (market / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "demo-market",
                "owner": {"name": "Demo"},
                "plugins": [{"name": "tracker", "source": "./plugins/tracker"}],
            }
        )
    )
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "tracker", "version": "1.0.0", "description": "Issue tracker"})
    )
    with oauth_mcp_server() as server:
        (plugin / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"issues": {"type": "http", "url": server.url}}})
        )
        _run([claude, "plugin", "marketplace", "add", str(market)], home)
        _run([claude, "plugin", "install", "tracker@demo-market"], home)
        with _host(tmp_path, live_server, mock_llm_server_url, "claude", claude) as host:
            page = _open_settings(request, live_server, host, "claude-native")
            plugins_tab = page.get_by_test_id("harness-tab-plugins")
            expect(plugins_tab).to_have_text(re.compile(r"Plugins · 1"), timeout=60_000)
            plugins_tab.click()
            page.get_by_test_id("catalog-row-tracker").click()
            mcps_tab = page.get_by_role("tab", name=re.compile(r"MCPs"))
            expect(mcps_tab).to_be_visible()
            mcps_tab.click()
            row = _expect_needs_sign_in(page, "issues")
            _loopback_sign_in(page, row, server)
            page.context.close()


def test_codex_mcp_sign_in_by_pasting_the_redirect(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    codex = _cli("codex")
    home = tmp_path / "host" / "home"
    (home / ".codex").mkdir(parents=True)
    with oauth_mcp_server() as server:
        (home / ".codex" / "config.toml").write_text(
            f'[mcp_servers.issues]\nurl = "{server.url}"\n'
        )
        with _host(tmp_path, live_server, mock_llm_server_url, "codex", codex) as host:
            page = _open_settings(request, live_server, host, "codex-native")
            # Stand in for a remote host: the browser can't reach the host's loopback
            # listener. Redirects aren't routed, so read the consent page's redirect.
            redirects: list[str] = []

            def refuse_redirect(route: Route) -> None:
                if route.request.method != "POST":
                    route.continue_()
                    return
                response = route.fetch(max_redirects=0)
                redirects.append(response.headers["location"])
                route.abort("connectionrefused")

            consent = server.url.removesuffix("/mcp") + "/approve*"
            page.context.route(consent, refuse_redirect)
            row = _expect_needs_sign_in(page, "issues")
            page.get_by_role("button", name="Sign in", exact=True).click()
            popup = _approve_in_new_tab(page)
            for _ in range(150):
                if redirects:
                    break
                popup.wait_for_timeout(100)
            assert len(redirects) == 1
            assert re.match(r"^http://(127\.0\.0\.1|localhost):\d+/callback", redirects[0])
            _close_when_done(popup)
            dialog = page.get_by_role("dialog")
            expect(dialog.get_by_role("status")).to_have_text(
                "Waiting for you to finish in the browser…"
            )
            field = dialog.get_by_label(re.compile("paste it here"))
            field.click()
            field.fill(redirects[0])
            dialog.get_by_role("button", name="Finish").click()
            _expect_signed_in(page, row, server)
            page.context.close()
