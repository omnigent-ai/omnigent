"""Browser journey: a host-launched runner that exits at boot fails the session.

A real host spawns a runner that dies during ``create_app``. The session fails
with ``runner_failed_to_start``; the error card must lead with the runner's own
exit reason instead of a generic headline over a raw log tail.
"""

from __future__ import annotations

import contextlib
import functools
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import compat_runner_python, compat_server_python
from tests._helpers.runner_faults import (
    disk_full_spec_cache_pythonpath,
    tunnel_rejection_spec_cache_pythonpath,
)
from tests._helpers.server_runner import server_runner
from tests.e2e_ui.conftest import _register_extra_agent

# The host's cause line, the SPA headline and the PYTHONPATH fault all require
# the stack built from this checkout, so pinned server or runner builds skip.
pytestmark = pytest.mark.skipif(
    compat_runner_python() is not None or compat_server_python() is not None,
    reason="requires the host report and SPA card built from this checkout",
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REJECT_REASON = (
    "runner tunnel rejected by server (HTTP 403 persisted across 3 attempts); "
    "run omnigent login http://127.0.0.1 to re-authenticate"
)

_T = TypeVar("_T")


def _hold_for_recording(page: Page, ms: int) -> None:
    """Pause only while filming, so the clip shows the state; plain runs skip it."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(ms)


def _wait_until(predicate: Callable[[], _T | None], *, timeout: float = 90.0) -> _T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.3)
    raise AssertionError(f"condition was not met within {timeout}s")


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    ("fault", "inject", "cause_substring"),
    [
        ("enospc", disk_full_spec_cache_pythonpath, "No space left on device"),
        (
            "reject403",
            functools.partial(tunnel_rejection_spec_cache_pythonpath, reason=_REJECT_REASON),
            "run omnigent login",
        ),
    ],
    ids=["enospc", "reject403"],
)
def test_runner_boot_crash_names_the_cause(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
    fault: str,
    inject: Callable[..., str],
    cause_substring: str,
) -> None:
    """A boot-crashing runner fails the session with its reason named up front."""
    pythonpath = inject(tmp_path / f"inject-{fault}", _REPO_ROOT, os.environ.get("PYTHONPATH"))

    with (
        server_runner(tmp_path / f"stack-{fault}") as stack,
        httpx.Client(base_url=stack.base_url, timeout=120.0, trust_env=False) as client,
    ):
        stack.start_host(
            env={
                "OMNIGENT_RUNNER_ZYGOTE": "0",
                "PYTHONPATH": pythonpath,
                "OPENAI_API_KEY": "mock-key",
                "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
                "ANTHROPIC_API_KEY": "mock-key",
                "ANTHROPIC_BASE_URL": mock_llm_server_url,
            }
        )

        def online_host() -> dict | None:
            # A transport error or a partial reply while the stack starts is another poll.
            with contextlib.suppress(httpx.HTTPError, ValueError, KeyError):
                resp = client.get("/v1/hosts")
                resp.raise_for_status()
                return next((h for h in resp.json()["hosts"] if h["status"] == "online"), None)
            return None

        host_id = _wait_until(online_host, timeout=60.0)["host_id"]
        agent_id = _register_extra_agent(stack.base_url, f"boot-crash-{fault}", "terse")
        assert agent_id is not None
        workspace = stack.workspace / "project"
        workspace.mkdir(exist_ok=True)
        create = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        # The first message makes the host launch the runner. The server either
        # queues it (202) or, once the boot crash is reported, refuses it (503).
        send = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hello?"}]},
            },
        )
        assert send.status_code in {202, 503}, (send.status_code, send.text)

        def failed_error() -> dict | None:
            with contextlib.suppress(httpx.HTTPError, ValueError):
                snap = client.get(f"/v1/sessions/{session_id}").json()
                if snap.get("status") == "failed" and snap.get("last_task_error"):
                    return snap["last_task_error"]
            return None

        error = _wait_until(failed_error, timeout=120.0)
        assert error["code"] == "runner_failed_to_start", error
        assert cause_substring in error["message"], error["message"]

        # Create the recorded page only after setup so the clip is the SPA journey.
        page: Page = request.getfixturevalue("page")
        page.goto(f"{stack.base_url}/c/{session_id}")
        pill = page.get_by_test_id("error-pill")
        expect(pill).to_be_visible(timeout=30_000)
        headline = page.get_by_test_id("error-headline")
        # Hold the collapsed pill in view before expanding so a recording shows
        # the headline, the expand, and the stated cause.
        headline.scroll_into_view_if_needed()
        expect(headline).to_be_visible()
        _hold_for_recording(page, 1_500)
        pill.locator("button[aria-expanded]").click()
        message = page.get_by_test_id("error-message-content")
        expect(message).to_contain_text(cause_substring)
        headline.scroll_into_view_if_needed()
        _hold_for_recording(page, 2_000)

        # The card leads with a specific headline rather than the generic fallback.
        expect(page.get_by_test_id("error-headline")).not_to_have_text(
            "Something went wrong", timeout=10_000
        )
        expect(headline).to_have_text("The session's runner failed to start on the host.")
        # The composed message names the cause up front (line two) instead of
        # burying it in the log tail or cutting it off above the shown lines.
        lines = error["message"].splitlines()
        assert len(lines) > 1 and lines[1].startswith("cause:"), error["message"]
        # The raw runner log stays one click away in diagnostics, not in the body.
        expect(message).not_to_contain_text("Traceback")
        page.get_by_role("button", name="View diagnostics").click()
        expect(page.get_by_test_id("error-diagnostics-content")).to_contain_text(
            cause_substring, timeout=15_000
        )
        _hold_for_recording(page, 2_000)
