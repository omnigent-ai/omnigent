"""Settings recovery in the built SPA with explicit synthetic API contracts.

No identity provider or vendor CLI is contacted by these browser tests.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import chat_session_handle
from tests.browser_ui.conftest import BrowserContract


@pytest.mark.timeout(90)
@pytest.mark.parametrize("supports_login", [True, False], ids=["recovery", "older-host"])
def test_expired_codex_account_recovers_on_selected_machine(
    page: Page, browser_contract: BrowserContract, supports_login: bool
) -> None:
    """Expired account → retry/cancel → synthetic completion, or CLI fallback."""
    # Reuse the SPA shell contract; only Settings-specific dependencies differ.
    with contextmanager(chat_session_handle)(page, browser_contract):
        contract = browser_contract
        host_id = "selected-machine"
        host: dict[str, Any] = {
            "host_id": host_id,
            "name": "Synthetic laptop",
            "status": "online",
            "owner": "e2e",
            "configured_harnesses": {"codex-native": "needs-auth"},
            "harness_accounts_supported": True,
            "harness_login_supported": supports_login,
            "harness_accounts": {
                "codex": {"kind": "chatgpt", "email": "expired@example.test", "expired": True}
            },
        }
        contract.json("/v1/hosts", lambda _: {"hosts": [host]})
        contract.json(
            "/v1/info",
            {
                "single_user": True,
                "features": {"harness_install": True},
                "installable_harnesses": ["codex-native"],
                "databricks_features": False,
            },
        )
        contract.json("/v1/harnesses", {"data": [], "setup_steps": {}})
        prefix = f"/v1/hosts/{host_id}"
        contract.json(prefix + "/mcp-servers", {"mcp_servers": []})
        contract.json(prefix + "/plugins", {"plugins": []})
        contract.json(re.compile(r"/v1/skills(?:\?.*)?$"), {"skills": []})
        contract.json(prefix + "/credentials/detected", {"credentials": []})
        contract.json(
            prefix + "/harnesses/codex-native/startup",
            {
                "command": "codex",
                "resolved_path": "/usr/bin/codex",
                "command_source": "default",
                "arg_count": 0,
                "args": [],
                "environment": {"inherit": True, "variables": {}, "unset": []},
            },
        )
        contract.json(
            prefix + "/harnesses/accounts/refresh",
            lambda _: {"harness_accounts": host["harness_accounts"]},
            method="POST",
        )
        login_path = prefix + "/harnesses/codex/login"
        starts: list[str] = []
        cancelled: list[str] = []
        status = "pending"

        def start(request: Request) -> dict[str, Any]:
            starts.append(request.url)
            return {
                "login_id": f"attempt-{len(starts)}",
                "verification_url": "https://identity.example.test/device",
                "user_code": "SYNTHETIC-CODE",
                "expires_at": int(time.time()) + 900,
            }

        def poll(request: Request) -> dict[str, Any]:
            if request.url.endswith("attempt-1"):
                return {"status": "failed", "error": "Synthetic sign-in expired", "expired": True}
            if status == "succeeded":
                host["configured_harnesses"] = {"codex-native": True}
                host["harness_accounts"] = {
                    "codex": {"kind": "chatgpt", "email": "recovered@example.test", "plan": "Plus"}
                }
            return {
                "status": status,
                "expired": False,
                "configured_harnesses": host["configured_harnesses"],
                "harness_accounts": host["harness_accounts"],
            }

        def cancel(request: Request) -> dict[str, Any]:
            cancelled.append(request.url)
            return {"status": "cancelled"}

        contract.json(login_path, start, method="POST")
        contract.json(re.compile(re.escape(login_path) + r"/attempt-\d+$"), poll)
        contract.json(
            re.compile(re.escape(login_path) + r"/attempt-\d+$"), cancel, method="DELETE"
        )
        page.goto(f"{contract.base_url}/settings/harnesses/codex-native?host={host_id}")
        expect(page.get_by_text("Sign-in expired", exact=True)).to_be_visible()
        page.get_by_role("button", name="Sign in again", exact=True).click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("radio", name="Personal subscription").click()
        dialog.get_by_role("button", name="Authenticate", exact=True).click()
        if not supports_login:
            expect(dialog.get_by_text("codex login --device-auth", exact=True)).to_be_visible()
            assert starts == []
            return

        expect(dialog.get_by_text("Synthetic sign-in expired", exact=True)).to_be_visible()
        dialog.get_by_role("button", name="Try again", exact=True).click()
        expect(dialog.get_by_text("SYNTHETIC-CODE", exact=True)).to_be_visible()
        expect(dialog.get_by_role("link", name="Open sign-in page")).to_have_attribute(
            "href", "https://identity.example.test/device"
        )
        expect(dialog).to_contain_text("Signing in on Synthetic laptop")
        with page.expect_response(lambda response: response.request.method == "DELETE"):
            dialog.get_by_role("button", name="Cancel", exact=True).click()
        expect(dialog).not_to_be_visible()
        assert cancelled == [contract.base_url + login_path + "/attempt-2"]

        page.get_by_role("button", name="Sign in again", exact=True).click()
        dialog.get_by_role("radio", name="Personal subscription").click()
        dialog.get_by_role("button", name="Authenticate", exact=True).click()
        expect(dialog.get_by_text("SYNTHETIC-CODE", exact=True)).to_be_visible()
        status = "succeeded"
        expect(dialog).not_to_be_visible(timeout=10_000)
        expect(page.get_by_text("Sign-in expired", exact=True)).not_to_be_visible()
        expect(page.get_by_role("button", name="Switch account", exact=True)).to_be_visible()
        expect(page.get_by_text(re.compile("recovered@example.test")).first).to_be_visible()
        assert starts == [contract.base_url + login_path] * 3
        assert cancelled == [contract.base_url + login_path + "/attempt-2"]
