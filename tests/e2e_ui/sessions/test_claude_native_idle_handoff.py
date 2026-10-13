"""A completed Claude Task child must not fail when its new replica loses the runner.

Claude's real Agent tool creates and completes the child through the native
forwarder. Two server processes share storage and a TCP proxy moves the real
runner between them. Only model replies and the stale saved status are staged;
the handoff, cold cache, disconnect grace, and browser rendering are real.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.entities import ResourceEventData
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e.test_runner_tunnel_mid_turn_reconnect_grace_e2e import (
    _poll_until,
    _TunnelIngressProxy,
)
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_REPO = Path(__file__).resolve().parents[3]
_MODEL = "claude-sonnet-4-20250514"
_PARENT_PROMPT = "NATIVE_IDLE_HANDOFF_PARENT: delegate the check to a general-purpose agent."
_CHILD_PROMPT = "NATIVE_IDLE_HANDOFF_CHILD: finish the delegated check and report the result."
_CHILD_RESULT = "The delegated check completed successfully."
_PARENT_RESULT = "The child finished its check."


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _agents_row(page: Page, child_id: str) -> Locator:
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    row = rail.locator(f'[data-testid="subagent-row"][data-child-session-id="{child_id}"]')
    expect(row).to_be_visible(timeout=30_000)
    return row


@pytest.mark.timeout(360)
def test_completed_claude_child_survives_stale_status_handoff(
    page: Page,
    built_spa: None,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Saved running state alone cannot turn a finished native child into an error."""
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip(
            "machine-managed Claude settings override mock auth; use an isolated container"
        )

    mock_url = isolated_mock_llm_server_url
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "toolu_idle_handoff_child",
                        "name": "Agent",
                        "arguments": json.dumps(
                            {
                                "subagent_type": "general-purpose",
                                "description": "Check idle handoff",
                                "prompt": _CHILD_PROMPT,
                            }
                        ),
                    }
                ]
            },
            {"text": _PARENT_RESULT},
        ],
        match=_PARENT_PROMPT,
        required_tools=["Agent"],
    )
    configure_mock_llm(
        mock_url,
        [{"text": _CHILD_RESULT}],
        match=_CHILD_PROMPT,
        required_tools=["Bash"],
    )
    set_fallback_mock_llm(mock_url, key="default", text="Acknowledged.")
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')

    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "repro-claude": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {"default": _MODEL},
                        },
                    }
                },
            }
        )
    )
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    token = secrets.token_urlsafe(32)
    with ExitStack() as resources:
        original = resources.enter_context(
            server_runner(
                tmp_path / "replica-a",
                server_cwd=_REPO,
                base_env=base_env,
                server_env=env,
                binding_token=token,
                poll_interval=0.2,
            )
        )
        proxy = _TunnelIngressProxy("127.0.0.1", original.port)
        resources.callback(proxy.close)
        original.start_runner(
            cwd=_REPO, env={**env, "RUNNER_SERVER_URL": f"http://127.0.0.1:{proxy.port}"}
        )
        client = resources.enter_context(
            httpx.Client(
                base_url=original.base_url,
                trust_env=False,
                timeout=30,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    "x-omnigent-background-session-titles": "off",
                },
            )
        )
        parent_id = create_native_session(
            client,
            original.base_url,
            harness="claude",
            metadata={
                "workspace": str(original.workspace),
                "terminal_launch_args": ["--dangerously-skip-permissions"],
            },
        )["session_id"]
        bind_session_runner(client.patch, original.base_url, parent_id, original.runner_id)
        page.goto(f"{original.base_url}/c/{parent_id}")
        page.get_by_test_id("view-mode-chat").click(timeout=90_000)
        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_enabled(timeout=90_000)
        composer.fill(_PARENT_PROMPT)
        page.get_by_role("button", name="Send", exact=True).click()
        expect(
            page.locator('[data-role="assistant"]', has_text=_PARENT_RESULT).first
        ).to_be_visible(timeout=90_000)

        children = _get(client, f"/v1/sessions/{parent_id}/child_sessions")["data"]
        assert len(children) == 1, children
        child_id = children[0]["id"]
        assert children[0]["labels"]["omnigent.wrapper"] == "claude-code-native-ui-subagent"
        _poll_until(
            lambda: all(
                _get(client, f"/v1/sessions/{session_id}")["status"] == "idle"
                for session_id in (parent_id, child_id)
            ),
            timeout=30,
            what="the real Claude parent and child to finish",
        )
        row = _agents_row(page, child_id)
        expect(row.get_by_test_id("subagent-status-avatar")).to_have_attribute(
            "data-activity", "done", timeout=30_000
        )
        row.click()
        expect(
            page.locator('[data-role="assistant"]', has_text=_CHILD_RESULT).first
        ).to_be_visible()
        expect(page.get_by_test_id("error-pill")).to_have_count(0)

        # Snapshot requests refresh status; leave both chats closed until the decision.
        page.goto("about:blank")
        store = SqlAlchemyConversationStore(original.database_uri)
        store.set_session_live_status(child_id, "running")
        child = store.get_conversation(child_id)
        assert child is not None and child.live_status == "running"
        assert child.runner_id == original.runner_id
        replica_log = tmp_path / "replica-b-process.log"
        replica = resources.enter_context(
            server_runner(
                tmp_path / "replica-b",
                server_cwd=_REPO,
                base_env=base_env,
                server_env={**env, "OMNIGENT_PROCESS_LOG_FILE": str(replica_log)},
                database_uri=original.database_uri,
                artifact_location=original.artifact_location,
                binding_token=token,
                poll_interval=0.2,
            )
        )
        proxy.begin_blackout()
        proxy.retarget("127.0.0.1", replica.port)
        proxy.end_blackout()
        _poll_until(
            lambda: f"runner stream ready for session={child_id}" in replica_log.read_text(),
            timeout=30,
            what="the cold replica to adopt the native child",
        )
        child = store.get_conversation(child_id)
        assert child is not None and child.live_status == "running"
        proxy.begin_blackout()
        # Both handlers must settle before a browser snapshot can warm the cache.
        _poll_until(
            lambda: (
                replica_log.read_text().count(f"Runner disconnect for session={child_id}:") >= 2
            ),
            timeout=RUNNER_DISCONNECT_GRACE_S + 30,
            what="the relay and offline sweep to decide after the production disconnect grace",
        )

        page.goto(f"{replica.base_url}/c/{parent_id}")
        row = _agents_row(page, child_id)
        expect(row.get_by_test_id("subagent-status-avatar")).not_to_have_attribute(
            "data-activity", re.compile("failed|disconnected")
        )
        row.click()
        for reload_page in (False, True):
            if reload_page:
                page.reload()
            expect(
                page.locator('[data-role="assistant"]', has_text=_CHILD_RESULT).first
            ).to_be_visible()
            expect(page.get_by_test_id("error-pill")).to_have_count(0)

        with httpx.Client(base_url=replica.base_url, trust_env=False, timeout=30) as recovered:
            snapshot = _get(recovered, f"/v1/sessions/{child_id}")
        assert snapshot["status"] != "failed", snapshot
        assert snapshot.get("last_task_error") is None, snapshot
        child = store.get_conversation(child_id)
        assert child is not None
        assert not any(
            value
            for key, value in child.labels.items()
            if key.startswith("omnigent.last_task_error")
        )
        activities = store.list_items(parent_id, type="resource_event").data
        assert not any(
            isinstance(item.data, ResourceEventData)
            and item.data.resource_id == child_id
            and isinstance(item.data.resource, dict)
            and item.data.resource.get("status") == "failed"
            for item in activities
        )
        log = replica_log.read_text()
        assert f"session turn failed for {child_id} " not in log
        assert (
            f"Runner disconnect for session={child_id}: subagent_unobserved "
            "(status=running source=persisted)"
        ) in log
