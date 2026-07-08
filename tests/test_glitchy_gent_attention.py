"""Tests for the Glitchy Gent attention ticket updater."""

from __future__ import annotations

import datetime as dt

from omnigent.glitchy_gent_attention import (
    ChildActivity,
    SessionActivity,
    classify_sessions,
    render_board_section,
    sanitize_evidence,
    update_board_text,
)


def _user_message(text: str) -> dict[str, object]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _error(message: str, *, code: str = "RuntimeError") -> dict[str, object]:
    return {
        "type": "error",
        "status": "completed",
        "code": code,
        "message": message,
    }


def test_matrix_revival_after_error_is_p0_incident() -> None:
    session = SessionActivity(
        id="conv_c70c456814af447f870a4ac984ad0ff1",
        title="matrix",
        status="running",
        runner_online=True,
        recent_items_desc=[
            _user_message("testing"),
            _user_message("are you there"),
            _error("account-create command timed out with unknown account state"),
        ],
    )

    tickets = classify_sessions([session])

    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.priority == "P0"
    assert ticket.state == "incident"
    assert ticket.title == "Matrix Account Creation State Unknown"
    assert any("revival/testing" in line for line in ticket.evidence)
    assert "duplicate state" in ticket.risk


def test_postiz_running_child_is_p2_ambient_watch() -> None:
    parent = SessionActivity(
        id="conv_396dc339cbb247cfb35774e95056e3ec",
        title="postiz",
        status="idle",
        runner_online=True,
        children=[
            ChildActivity(
                id="conv_58795a26ecb34a8e88c61269a321c1bb",
                title="codex:postiz-stage-11-shorts",
                status="running",
                busy=True,
            )
        ],
    )

    tickets = classify_sessions([parent])

    assert len(tickets) == 1
    ticket = tickets[0]
    assert ticket.priority == "P2"
    assert ticket.state == "watch"
    assert ticket.title == "Postiz Worker Active"
    assert "do not interrupt" in ticket.blocked_action.lower()


def test_failed_attention_recovery_is_p1() -> None:
    session = SessionActivity(
        id="conv_40bc19385df446039af6ac88f85be315",
        title="attention recovery",
        status="failed",
        runner_online=True,
        archived=True,
        labels={"glitchy.role": "attention-recovery"},
        last_task_error={
            "code": "runner_error",
            "message": "turn exceeded the 240s harness idle watchdog",
        },
    )

    tickets = classify_sessions([session])

    assert len(tickets) == 1
    assert tickets[0].priority == "P1"
    assert tickets[0].title == "Attention Recovery Spare Failed"


def test_idle_healthy_attention_recovery_does_not_keep_stale_p1() -> None:
    session = SessionActivity(
        id="conv_40bc19385df446039af6ac88f85be315",
        title="attention recovery",
        status="idle",
        runner_online=True,
        labels={"glitchy.role": "attention-recovery"},
    )

    assert classify_sessions([session]) == []


def test_runner_disconnected_without_revival_is_p3_backlog() -> None:
    session = SessionActivity(
        id="conv_stale",
        title="old worker",
        status="failed",
        runner_online=False,
        last_task_error={
            "code": "runner_disconnected",
            "message": "Runner disconnected unexpectedly.",
        },
    )

    tickets = classify_sessions([session])

    assert len(tickets) == 1
    assert tickets[0].priority == "P3"
    assert tickets[0].state == "handled"


def test_prompt_text_about_timeouts_does_not_create_timeout_ticket() -> None:
    session = SessionActivity(
        id="conv_prompt",
        title="attention-ticket-updater",
        status="running",
        runner_online=True,
        recent_items_desc=[
            _user_message(
                "Classify tool timeouts and update the generated board without mutation."
            )
        ],
    )

    tickets = classify_sessions([session])

    assert len(tickets) == 1
    assert tickets[0].priority == "P2"
    assert tickets[0].title == "Active Session Watch"


def test_render_board_section_uses_mode_and_does_not_dump_raw_revival_text() -> None:
    session = SessionActivity(
        id="conv_c70c456814af447f870a4ac984ad0ff1",
        title="matrix",
        status="running",
        runner_online=True,
        recent_items_desc=[
            _user_message("hello are you there come back"),
            _error("account-create command timed out with unknown account state"),
        ],
    )
    tickets = classify_sessions([session])
    rendered = render_board_section(
        tickets,
        generated_at=dt.datetime(2026, 7, 7, 16, 45, tzinfo=dt.timezone.utc),
        server_url="http://example.test:6767",
        session_count=1,
    )

    assert "Current mode: `Incident Recovery`" in rendered
    assert "### P0 - Matrix Account Creation State Unknown" in rendered
    assert "hello are you there come back" not in rendered
    assert "<!-- GLITCHY-GENT-ATTENTION:START -->" in rendered
    assert "<!-- GLITCHY-GENT-ATTENTION:END -->" in rendered


def test_update_board_text_replaces_only_marked_generated_section() -> None:
    existing = """---
type: attention-ticket-board
updated: 2026-07-07T16:00:00Z
---

# Glitchy Gent Attention Tickets

Human note before.

<!-- GLITCHY-GENT-ATTENTION:START -->
old generated section
<!-- GLITCHY-GENT-ATTENTION:END -->

Human safety rule after.
"""
    generated = """<!-- GLITCHY-GENT-ATTENTION:START -->
## Mode

- Current mode: `Ambient Watch`
<!-- GLITCHY-GENT-ATTENTION:END -->
"""

    updated = update_board_text(
        existing,
        generated,
        generated_at=dt.datetime(2026, 7, 7, 17, 0, tzinfo=dt.timezone.utc),
    )

    assert "Human note before." in updated
    assert "Human safety rule after." in updated
    assert "old generated section" not in updated
    assert "updated: 2026-07-07T17:00:00Z" in updated


def test_update_board_text_migrates_unmarked_board_after_intro() -> None:
    existing = """# Glitchy Gent Attention Tickets

Intro stays.

## Mode

Old mode.
"""
    generated = """<!-- GLITCHY-GENT-ATTENTION:START -->
## Mode

- Current mode: `Ambient Watch`
<!-- GLITCHY-GENT-ATTENTION:END -->
"""

    updated = update_board_text(
        existing,
        generated,
        generated_at=dt.datetime(2026, 7, 7, 17, 0, tzinfo=dt.timezone.utc),
    )

    assert "Intro stays." in updated
    assert "Old mode." not in updated
    assert updated.count("## Mode") == 1


def test_sanitize_evidence_redacts_sensitive_assignments() -> None:
    evidence = sanitize_evidence(
        "Railway check used API_TOKEN=abcdefghijklmnopqrstuvwxyz1234567890 and printed=false"
    )

    assert "abcdefghijklmnopqrstuvwxyz" not in evidence
    assert "API_TOKEN=[redacted]" in evidence
