"""UI journey: an OIDC user's automation survives its first Run now.

External auth saves tasks without an account generation while the owner's
``users`` row may carry one (kept across an accounts-to-OIDC switch, or set by a
password reset). That owner is live, so the task must fire rather than be
disabled as ``owner_deleted``. Runs a dedicated OIDC-mode server behind the
suite's fake IdP and drives the real sign-in redirect and Automations page.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.passwords import hash_password
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.e2e_ui.auth._fake_idp import FAKE_IDP_EMAIL
from tests.e2e_ui.auth._oidc_server import OIDCServer, spawn_oidc_server

_TASK_NAME = "Morning digest"
_RUN_RECORDED_TIMEOUT_S = 20.0
_RECORDING = bool(os.environ.get("OMNIGENT_E2E_RECORD_DIR"))


@pytest.fixture(scope="module")
def oidc_server_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("e2e_ui_oidc_scheduled")


@pytest.fixture(scope="module")
def oidc_server(
    built_spa: None, mock_llm_server_url: str, oidc_server_tmp: Path
) -> Iterator[OIDCServer]:
    """A dedicated OIDC-mode server wired to a fake IdP."""
    yield from spawn_oidc_server(mock_llm_server_url, oidc_server_tmp)


@pytest.fixture
def owner_with_generation(oidc_server: OIDCServer, oidc_server_tmp: Path) -> str:
    """Give the IdP identity a users row with a generation, as a password reset would."""
    db_uri = f"sqlite:///{oidc_server_tmp / 'test.db'}"
    permission_store = SqlAlchemyPermissionStore(db_uri)
    permission_store.ensure_user(FAKE_IDP_EMAIL)
    generation = SqlAlchemyAccountStore(db_uri).update_password(
        FAKE_IDP_EMAIL, hash_password("e2e-owner-pw-123456")
    )
    owner = permission_store.get_user(FAKE_IDP_EMAIL)
    assert generation and owner is not None and owner.account_generation == generation
    return generation


def _pause_for_recording(page: Page, ms: int) -> None:
    """Hold the frame only while filming; CI runs the journey at full speed."""
    if _RECORDING:
        page.wait_for_timeout(ms)


def _session_cookie_header(page: Page) -> dict[str, str]:
    cookies = page.context.cookies()
    return {"Cookie": "; ".join(f"{c['name']}={c['value']}" for c in cookies)}


def _api_get(page: Page, base_url: str, path: str) -> Any:
    """Read the API as the signed-in browser user (its session cookie)."""
    resp = httpx.get(f"{base_url}{path}", headers=_session_cookie_header(page), timeout=10.0)
    resp.raise_for_status()
    return resp.json()


def _wait_for_run(page: Page, base_url: str, task_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _RUN_RECORDED_TIMEOUT_S
    while time.monotonic() < deadline:
        runs = _api_get(page, base_url, f"/v1/scheduled-tasks/{task_id}/runs")["runs"]
        if runs:
            return runs[0]
        page.wait_for_timeout(250)
    raise AssertionError(f"Run now did not record a run within {_RUN_RECORDED_TIMEOUT_S:.0f}s")


def test_oidc_owner_with_account_generation_run_now_fires(
    request: pytest.FixtureRequest,
    oidc_server: OIDCServer,
    oidc_server_tmp: Path,
    owner_with_generation: str,
) -> None:
    """Run now keeps the task active; with no host connected the run fails for a host reason."""
    page: Page = request.getfixturevalue("page")
    public_url, api_url = oidc_server.public_url, oidc_server.base_url
    started = time.monotonic()
    timeline: list[tuple[float, str]] = []

    def mark(label: str) -> None:
        timeline.append((round(time.monotonic() - started, 1), label))

    page.goto(f"{public_url}/tasks")
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    expect(continue_link).to_contain_text(FAKE_IDP_EMAIL)
    mark("fake IdP sign-in page shown")
    continue_link.click()
    expect(page).not_to_have_url(re.compile(r"/authorize|/auth/login"), timeout=15_000)
    expect(page.locator('[data-testid="sidebar-brand"]')).to_be_visible(timeout=15_000)
    mark("signed in")

    page.goto(f"{public_url}/tasks")
    _pause_for_recording(page, 1_500)
    page.get_by_test_id("new-task-button").click()
    expect(page.get_by_test_id("create-scheduled-task-dialog")).to_be_visible(timeout=30_000)
    mark("New task dialog open")
    _pause_for_recording(page, 1_500)
    page.get_by_test_id("task-name-input").fill(_TASK_NAME)
    page.get_by_test_id("task-prompt-input").fill("Summarize yesterday's activity.")
    agent_trigger = page.get_by_test_id("task-agent-picker").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    expect(agent_trigger).to_contain_text("Claude Code", timeout=30_000)
    page.get_by_test_id("create-scheduled-task-submit").click()

    row = page.locator('[data-testid="scheduled-task-row"]').filter(has_text=_TASK_NAME)
    expect(row).to_be_visible(timeout=30_000)
    expect(row.get_by_test_id("task-next-run")).to_contain_text("Next run in", timeout=30_000)
    mark("task row listed with a next run")
    _pause_for_recording(page, 2_000)
    tasks = _api_get(page, api_url, "/v1/scheduled-tasks")["scheduled_tasks"]
    created = next(t for t in tasks if t["name"] == _TASK_NAME)
    task_id = created["id"]
    assert created["owner_user_id"] == FAKE_IDP_EMAIL
    # The reported precondition: external auth saved the task without a generation.
    stored = SqlAlchemyScheduledTaskStore(f"sqlite:///{oidc_server_tmp / 'test.db'}").get(task_id)
    assert stored is not None and stored.account_generation is None, stored

    row.hover()
    row.get_by_test_id("task-row-menu").click()
    expect(page.get_by_test_id("task-run-now")).to_be_visible()
    mark("row menu open")
    _pause_for_recording(page, 1_500)
    page.get_by_test_id("task-run-now").click()
    mark("Run now clicked")
    run = _wait_for_run(page, api_url, task_id)
    mark("run recorded server-side")
    _pause_for_recording(page, 2_500)

    page.reload()
    expect(row).to_be_visible(timeout=30_000)
    expect(row.get_by_test_id("task-schedule-line")).to_contain_text("Every day", timeout=30_000)
    row_after = {
        "schedule_line": row.get_by_test_id("task-schedule-line").inner_text(),
        "paused_pill": row.get_by_test_id("task-paused-pill").count(),
        "data_state": row.get_attribute("data-state"),
    }
    mark("Automations page reloaded")
    _pause_for_recording(page, 4_000)

    task = _api_get(page, api_url, f"/v1/scheduled-tasks/{task_id}")
    sessions = _api_get(page, api_url, "/v1/sessions?visibility=all")["data"]
    observed = {
        "owner_generation_set": bool(owner_with_generation),
        "task": {k: task[k] for k in ("state", "last_run_status", "next_run_at")},
        "run": {k: run.get(k) for k in ("status", "error_code", "conversation_id", "error")},
        "sessions_created": len(sessions),
        "row_after_reload": row_after,
        "timeline": timeline,
    }
    print("observed:", json.dumps(observed))
    assert run["error_code"] != "owner_deleted", f"live OIDC owner treated as deleted: {observed}"
    assert task["state"] == "active", observed
    assert row_after["data_state"] == "active", observed
    assert "Next run in" in row_after["schedule_line"], observed
