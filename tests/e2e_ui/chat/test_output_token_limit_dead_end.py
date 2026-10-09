"""E2E: a claude-native turn that hits Claude's output-token limit must not dead-end.

Claude Code fails a turn whose reply ends with ``stop_reason: "max_tokens"`` using
its CLAUDE_CODE_MAX_OUTPUT_TOKENS error, a remedy the web user cannot apply.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _CLAUDE_MOCK_MODEL,
    configure_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _select_view_mode,
    _send,
)

_ERROR_PILL = '[data-testid="error-pill"]'

# Only requests carrying this token draw from the max_tokens fault queue.
_FAULT_TOKEN = "overlong-report-fault"
_SANITY_LINE = "MOCK TURN OK output-limit-sanity"

# The code and guidance an output-limit failure must carry instead of the raw constant.
_OUTPUT_LIMIT_CODE = "output_limit_exceeded"
_OUTPUT_LIMIT_GUIDANCE = "Output limit reached"

# Claude Code's constant for a response that ended with stop_reason "max_tokens".
_RAW_LIMIT_ERROR_RE = re.compile(
    r"Claude['’]s response exceeded the [\d,]+ output token maximum\. "
    r"To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS "
    r"environment variable\."
)

# claude-native auto-launch + first-run pre-accept + first mock turn.
_FIRST_TURN_TIMEOUT_MS = 180_000
# The fault turn must reach a terminal state (idle/failed) within this window.
_FAULT_SETTLE_S = 150.0
# The transcript mirror and the error pill can trail the failed status edge.
_MIRROR_GRACE_S = 8.0


def _ensure_chat_view(page: Page) -> None:
    """Switch the terminal-first native session to its chat bubble view."""
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=_FIRST_TURN_TIMEOUT_MS)
    _select_view_mode(page, "Chat")


def _session_snapshot(base_url: str, session_id: str) -> dict:
    """Return the session's ``GET /v1/sessions/{id}`` body."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _wait_for_status(base_url: str, session_id: str, status: str, timeout_s: float) -> None:
    """Poll ``GET /v1/sessions/{id}`` until the session reports *status*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            if str(_session_snapshot(base_url, session_id).get("status") or "") == status:
                return
        time.sleep(1.0)
    pytest.fail(f"session never reached status {status!r} within {timeout_s:.0f}s")


def _transcript_blob(base_url: str, session_id: str) -> str:
    """Return the canonical transcript items, which the SPA renders, as one JSON string."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return json.dumps(resp.json().get("data", []), ensure_ascii=False)


def _error_pill_text(page: Page) -> str:
    """Return the error pill's headline title plus its visible text (``""`` when no pill)."""
    pill = page.locator(_ERROR_PILL)
    if pill.count() == 0:
        return ""
    parts: list[str] = []
    # Pill may detach mid-read; its headline and text are best-effort.
    with contextlib.suppress(Exception):
        headline = pill.first.locator('[data-testid="error-headline"]')
        if headline.count() > 0:
            parts.append(headline.first.get_attribute("title") or "")
        parts.append(pill.first.inner_text(timeout=5_000))
    return " ".join(part for part in parts if part)


def _assistant_bubble_text(page: Page) -> str:
    """Return every assistant bubble's visible text, newline-joined (``""`` when none)."""
    # Bubbles can re-render mid-read; their text is best-effort.
    with contextlib.suppress(Exception):
        return "\n".join(page.locator(_ASSISTANT).all_inner_texts())
    return ""


def _raw_constant_surfaces(
    page: Page, base_url: str, session_id: str, snapshot: dict
) -> list[tuple[str, str]]:
    """Return ``(surface, matched_text)`` for each web-visible surface carrying the constant."""
    failure_reason = str((snapshot.get("last_task_error") or {}).get("message") or "")
    surfaces = (
        ("the chat view's assistant bubbles", _assistant_bubble_text(page)),
        ("the failed turn's error pill", _error_pill_text(page)),
        ("the canonical transcript", _transcript_blob(base_url, session_id)),
        ("the failed session's last_task_error", failure_reason),
    )
    hits: list[tuple[str, str]] = []
    for where, text in surfaces:
        found = _RAW_LIMIT_ERROR_RE.search(text)
        if found:
            hits.append((where, found.group(0)))
    return hits


def _turn_settled(page: Page, status: str) -> bool:
    """Whether the turn is over: ``failed``, or ``idle`` with a second reply and no work."""
    if status == "failed":
        return True
    return (
        status == "idle"
        and page.locator(_ASSISTANT).count() >= 2
        and page.locator(_WORKING).count() == 0
    )


def _expand_error_pill(page: Page) -> None:
    """Best-effort: open the error pill so its full message is on screen."""
    with contextlib.suppress(Exception):
        pill = page.locator(_ERROR_PILL).first
        pill.scroll_into_view_if_needed(timeout=5_000)
        pill.locator('button[aria-expanded="false"]').first.click(timeout=5_000)
        expect(pill.get_by_test_id("error-message-content")).to_be_visible(timeout=5_000)


def _turn_failure_log_lines(
    tmp_path_factory: pytest.TempPathFactory, session_id: str
) -> list[str]:
    """Return the fixture-spawned server's 'session turn failed' lines for *session_id*.

    Reads the server's stdout capture and the log file it announces at startup.
    Empty when the server log is not local (a workflow-owned server).
    """
    needle = f"session turn failed for {session_id}"
    logs = list(tmp_path_factory.getbasetemp().glob("e2e_ui_server*/server.log"))
    for stdout_log in list(logs):
        with contextlib.suppress(OSError):
            logs.extend(
                Path(match.group(1))
                for match in re.finditer(r"^\s*log:\s+(\S+)", stdout_log.read_text(), re.M)
            )
    lines: list[str] = []
    for log in dict.fromkeys(logs):
        with contextlib.suppress(OSError):
            lines.extend(
                line.rstrip()
                for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                if needle in line
            )
    return lines


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_output_token_limit_turn_is_not_a_raw_dead_end(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A turn that hits Claude's output-token max must not strand the user on the raw CLI error.

    After a sanity turn, the fault turn must settle with no raw constant on any web
    surface, and a failed turn must not be attributed to the generic turn-error code.
    """
    base_url, session_id = native_claude_mock_session

    # Fallbacks survive /mock/reset, so Claude's background requests answer normally.
    set_fallback_mock_llm(mock_llm_server_url, "default", _SANITY_LINE)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _SANITY_LINE)

    # Deep enough that background requests (title generation etc.) carrying the
    # token cannot drain the queue before the main-loop request draws it.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": "Here is the start of the very long report you asked for —",
                "stop_reason": "max_tokens",
            }
        ]
        * 12,
        key="output-token-limit-fault",
        match=_FAULT_TOKEN,
    )
    print(f"product session: {base_url}/c/{session_id}")

    # Non-browser setup is complete; the recorded page starts at the first navigation.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # Turn 1 — sanity: a broken pipeline can never masquerade as a fixed build.
    _send(page, "hello, quick check before the real request")
    expect(page.locator(_ASSISTANT, has_text=_SANITY_LINE).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # The Stop hook flips the session back to idle a few seconds after the indicator clears.
    _wait_for_status(base_url, session_id, "idle", timeout_s=60.0)

    # Turn 2 — the scripted reply to this request ends with stop_reason "max_tokens".
    _send(page, f"please write the full 50-page report now ({_FAULT_TOKEN})")
    expect(page.locator(_USER, has_text=_FAULT_TOKEN).first).to_be_visible(timeout=60_000)

    settled_at: float | None = None
    deadline = time.monotonic() + _FAULT_SETTLE_S
    while time.monotonic() < deadline:
        try:
            snapshot = _session_snapshot(base_url, session_id)
            hits = _raw_constant_surfaces(page, base_url, session_id, snapshot)
        except httpx.HTTPError:
            # A transient API blip must not abort the diagnostic poll.
            time.sleep(2.0)
            continue
        if hits:
            # Let the mirror and the pill catch up so every affected surface is named.
            time.sleep(_MIRROR_GRACE_S)
            break
        if _turn_settled(page, str(snapshot.get("status") or "")):
            if settled_at is None:
                settled_at = time.monotonic()
        else:
            settled_at = None
        if settled_at is not None and time.monotonic() - settled_at >= _MIRROR_GRACE_S:
            break
        time.sleep(2.0)

    snapshot = _session_snapshot(base_url, session_id)
    hits = _raw_constant_surfaces(page, base_url, session_id, snapshot)
    status = str(snapshot.get("status") or "")
    last_task_error = snapshot.get("last_task_error") or {}
    log_lines = _turn_failure_log_lines(tmp_path_factory, session_id)
    _expand_error_pill(page)
    print(
        "fault turn outcome:",
        json.dumps(
            {
                "status": status,
                "last_task_error": last_task_error,
                "raw_constant_surfaces": hits,
                "assistant_bubbles": _assistant_bubble_text(page),
                "error_pill": _error_pill_text(page),
                "server_turn_failure_log": log_lines,
            },
            ensure_ascii=False,
        ),
    )
    output_dir = Path(str(request.config.getoption("--output")))
    output_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output_dir / "output-token-limit-chat.png"))

    if settled_at is None and not hits:
        pytest.fail(
            f"the output-token-limit turn never reached a terminal state within "
            f"{_FAULT_SETTLE_S:.0f}s (no failed status, no second assistant "
            "reply) — the claude-native pipeline did not finish the turn, so "
            "its output-token handling could not be judged."
        )

    problems: list[str] = []
    if hits:
        where = "; ".join(f"{surface} carried {text!r}" for surface, text in hits)
        problems.append(
            "the turn dead-ended on the raw CLI constant — "
            f"{where}. Setting CLAUDE_CODE_MAX_OUTPUT_TOKENS on the running CLI is "
            "not available from the Omnigent web chat."
        )
    elif status != "failed":
        # Claude Code fails a max_tokens turn; an idle end means the scripted stop
        # was not drawn or the CLI now recovers from it — verify the limit was hit.
        problems.append(
            f"the fault turn ended with status {status!r} instead of failing on the "
            "output limit, so the injected max_tokens stop was not demonstrated."
        )
    elif last_task_error.get("code") != _OUTPUT_LIMIT_CODE:
        problems.append(
            f"the failed turn was not attributed to {_OUTPUT_LIMIT_CODE!r} "
            f"(last_task_error={last_task_error!r}; server log={log_lines!r}), so the "
            "model's output cap is counted as an Omnigent turn failure instead of an "
            "upstream limit."
        )
    if _OUTPUT_LIMIT_GUIDANCE not in _assistant_bubble_text(page):
        problems.append(
            f"the chat does not show the {_OUTPUT_LIMIT_GUIDANCE!r} guidance for the "
            "failed turn, so the user has nothing to act on."
        )
    # The log is only readable for the fixture-spawned server; an external
    # --ui-base-url server yields no lines and skips this check.
    if log_lines and not any(f"code={_OUTPUT_LIMIT_CODE}" in line for line in log_lines):
        problems.append(
            f"the server's turn-failure log does not attribute the turn to "
            f"{_OUTPUT_LIMIT_CODE!r}: {log_lines!r}"
        )
    if problems:
        # Hold the failure state on screen so the recording ends on it.
        page.wait_for_timeout(3_000)
        pytest.fail(
            "a claude-native turn that hit Claude's output-token maximum: "
            + " Also, ".join(problems)
        )
