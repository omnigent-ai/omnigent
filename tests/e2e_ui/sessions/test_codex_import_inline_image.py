"""E2E: an imported Codex inline ``input_image`` must not blank the conversation.

Codex rollout files can carry a user image block as
``{"type": "input_image", "image_url": "data:...", "detail": "auto"}`` — no
``file_id``. The importer (`omnigent/session_import/local.py::_codex_message_data`)
preserves that block verbatim, but ``UserBubble`` in ``web/src/pages/ChatPage.tsx``
unconditionally evaluates ``img.file_id.startsWith("pending:")``, so one such
block throws ``TypeError: Cannot read properties of undefined (reading
'startsWith')`` during render and the whole conversation page goes blank.

This drives the REAL user journey end to end: it writes a genuine Codex rollout
JSONL under a private ``CODEX_HOME``, imports it with the real
``omnigent import --harness codex`` CLI against the live server, then opens the
imported conversation in the web UI and asserts the transcript renders — the
user's text, the assistant's reply, and no uncaught ``file_id`` TypeError.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Codex thread ids are hyphenated hex (see _CODEX_THREAD_ID_RE).
_SOURCE_SESSION_ID = "019e96aa-0be2-7343-8d3b-6f914d65753a"

# 1x1 PNG — the inline image payload shape reported in the bug.
_PIXEL_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

_USER_TEXT = "please look at this screenshot"
_ASSISTANT_TEXT = "I see a single dark pixel."


def _write_codex_rollout(codex_home: Path) -> None:
    """Write one Codex rollout whose user turn carries an inline input_image."""
    rollout = (
        codex_home
        / "sessions"
        / "2026"
        / "08"
        / "28"
        / f"rollout-2026-08-28T00-00-01-{_SOURCE_SESSION_ID}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "session_meta", "payload": {"id": _SOURCE_SESSION_ID, "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _USER_TEXT},
                    # Inline/imported representation: image_url, no file_id.
                    {"type": "input_image", "image_url": _PIXEL_DATA_URL, "detail": "auto"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": _ASSISTANT_TEXT}],
            },
        },
    ]
    rollout.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def _import_codex_session(live_server: str, codex_home: Path, config_home: Path) -> str:
    """Run the real ``omnigent import`` CLI and return the new session id."""
    env = {
        **os.environ,
        "CODEX_HOME": str(codex_home),
        # Isolate the CLI's config so it can't pick up an ambient server or
        # auth material from the invoking environment.
        "OMNIGENT_CONFIG_HOME": str(config_home),
        # Import omnigent from this worktree, not a stale site-packages copy.
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "import",
            "--harness",
            "codex",
            "--session",
            _SOURCE_SESSION_ID,
            "--server",
            live_server,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    # Output is the session's browser URL (…/c/<id>); pull the id back out.
    match = re.search(r"Imported \d+ item\(s\) into \S+/c/(\S+)", result.stdout)
    assert match is not None, f"unexpected import output: {result.stdout!r}"
    return match.group(1)


def test_imported_codex_inline_image_keeps_conversation_readable(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """An inline (no file_id) Codex image block must not blank the transcript.

    Failure mode this guards: ``UserBubble`` reads ``img.file_id.startsWith``
    on a block that only has ``image_url``, throwing during render; React
    unmounts the tree and the entire imported conversation shows as a blank
    page. The transcript text — present in the same imported items — must stay
    visible, and no uncaught ``startsWith`` TypeError may fire.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Base URL of the spawned server.
    :param tmp_path: Per-test scratch dir for the fake ``CODEX_HOME``.
    """
    codex_home = tmp_path / "codex-home"
    _write_codex_rollout(codex_home)
    session_id = _import_codex_session(live_server, codex_home, tmp_path / "config")

    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))

    page.goto(f"{live_server}/c/{session_id}")

    # The imported transcript renders: the user's text and the assistant's
    # reply are both visible inside message bubbles. Scoped to the transcript
    # (the sidebar link and header title also carry the user text) — with the
    # bug, the render throw blanks the conversation area and no bubble ever
    # appears.
    bubbles = page.get_by_test_id("message-bubble")
    try:
        expect(bubbles.filter(has_text=_USER_TEXT).first).to_be_visible(timeout=30_000)
        expect(bubbles.filter(has_text=_ASSISTANT_TEXT).first).to_be_visible(timeout=10_000)
    except AssertionError as exc:
        raise AssertionError(
            "imported conversation did not render its transcript; "
            f"uncaught page errors: {page_errors!r}"
        ) from exc

    # And the crash signature itself must not have fired.
    file_id_crashes = [err for err in page_errors if "startsWith" in err]
    assert not file_id_crashes, (
        f"imported inline input_image block crashed the conversation render: {file_id_crashes}"
    )
