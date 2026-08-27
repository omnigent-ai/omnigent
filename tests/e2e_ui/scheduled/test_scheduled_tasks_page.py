"""UI journey: the Scheduled Tasks page (``/tasks``).

Covers the Scheduled Tasks page row behavior: a task row renders the
human-readable schedule SUMMARY derived client-side from the stored RRULE
(``describeSchedule``), plus a Run-now action and a relative next-run label
("Next run in Xh"). That label is a DELTA formatted from the server's
authoritative ``next_run_at`` (nextRunAt − now) — the client never recomputes
WHICH instant is next (which couldn't match the server anchor for INTERVAL>1
rules), so the old "no client-recomputed countdown" rule still holds. See
``test_scheduled_task_row_run_controls``.

Tasks are seeded through the same REST API the page consumes
(``POST /v1/scheduled-tasks``), so this asserts the real render path
end-to-end. It's LLM-free and fast: no agent turn is dispatched — the
rows are pure UI state derived from the stored rule, so the mock LLM is
never exercised.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


def _builtin_agent_id(base_url: str, name: str) -> str:
    """Look up a built-in agent's id by name via ``GET /v1/agents``.

    A scheduled task requires a concrete ``agent_id``; the spawned server
    pre-registers ``hello_world`` via ``--agent``, so we resolve its id to
    seed tasks against it. (No agent turn ever fires — the id only has to
    reference a real agent so the create request validates.)

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param name: Built-in agent name, e.g. ``"hello_world"``.
    :returns: The agent id.
    """
    resp = httpx.get(f"{base_url}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    matches = [a["id"] for a in agents if a["name"] == name]
    assert matches, (
        f"built-in agent {name!r} not listed in /v1/agents "
        f"(got {[a['name'] for a in agents]}) — nothing to seed a task against."
    )
    return matches[0]


def _create_task(
    base_url: str,
    agent_id: str,
    name: str,
    rrule: str,
    *,
    host_id: str | None = None,
    workspace: str | None = None,
    model_override: str | None = None,
    reasoning_effort: str | None = None,
    permission_mode: str | None = None,
) -> str:
    """Seed one scheduled task via ``POST /v1/scheduled-tasks``.

    ``model_override`` / ``reasoning_effort`` / ``permission_mode`` are optional
    so a test can seed a task carrying the overrides the create/edit dialog
    persists — used by the edit-prefill tests, where the dialog must read them
    back onto the ``task-model-trigger`` / ``task-effort-trigger`` /
    ``task-permission-trigger`` controls. Left ``None`` they are omitted from the
    body (the row persists null, the agent's own defaults).

    :returns: The created task id.
    """
    body = {
        "name": name,
        "prompt": "Do the thing.",
        "rrule": rrule,
        "agent_id": agent_id,
        "timezone": "UTC",
    }
    if host_id is not None:
        body["host_id"] = host_id
    if workspace is not None:
        body["workspace"] = workspace
    if model_override is not None:
        body["model_override"] = model_override
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    if permission_mode is not None:
        body["permission_mode"] = permission_mode
    resp = httpx.post(
        f"{base_url}/v1/scheduled-tasks",
        json=body,
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _row_by_name(page: Page, name: str):
    """The scheduled-task row whose title matches ``name``."""
    return page.locator('[data-testid="scheduled-task-row"]').filter(has_text=name)


def _pick_minute(page: Page, minute: int) -> None:
    """Open the time picker and click a minute cell, tolerating a flickered open.

    The picker is a Radix popover nested inside the create-task dialog; under
    load the dialog's focus management can fire a focus/interaction-outside that
    closes it the moment it mounts, so the minute cells unmount between a
    visibility check and the click. Re-open from a known-closed state and retry
    until the cell is present, then click it.

    Selecting a minute does not close the popover, and an open floating-ui
    popover keeps recomputing its position — leaving the dialog's submit button
    perpetually "not stable" for Playwright. So dismiss the picker (by clicking a
    field inside the dialog, an interaction-outside — Escape would bubble to the
    Radix Dialog and close it) and wait for it to unmount, letting the layout
    settle before the caller submits.

    Every dismiss click on the name input uses force=True: while the picker
    popover is open the Radix Dialog owns pointer hit-testing over the modal, so
    a normal actionability-gated click resolves to the ``dialog-overlay`` at the
    input's coordinates ("overlay intercepts pointer events") and blocks the full
    30s under load. A forced click still dispatches a real pointerdown on the
    input, which Radix registers as the interaction-outside that closes the
    popover — the same reason the open click below is forced.

    :param page: Playwright page with the create/edit task dialog open.
    :param minute: Minute of the hour to select (0-59).
    """
    trigger = page.get_by_test_id("schedule-time-picker-trigger")
    cell = page.get_by_test_id(f"schedule-minute-{minute:02d}")
    picker = page.get_by_test_id("schedule-time-picker")
    name_input = page.get_by_test_id("task-name-input")
    for _ in range(5):
        # force=True skips the actionability "stable" wait: the trigger is a
        # tiny translate-positioned button, so a busy render loop can leave it
        # never-quite-stable even though the toggle is a no-arg click.
        trigger.click(force=True)
        try:
            expect(cell).to_be_visible(timeout=3_000)
            cell.click(force=True)
            break
        except (AssertionError, PlaywrightTimeoutError):
            # The popover flickered shut before the click landed; dismiss any
            # partial-open state (click-outside, not Escape) and try again.
            name_input.click(force=True)
    else:
        expect(cell).to_be_visible(timeout=3_000)
        cell.click(force=True)
    # Dismiss the picker so its floating position stops churning the layout.
    name_input.click(force=True)
    expect(picker).to_be_hidden(timeout=5_000)


def test_scheduled_task_rows_show_schedule_summary_and_relative_next_run(
    page: Page,
    live_server: str,
) -> None:
    """Rows render the RRULE-derived schedule summary + the relative next-run label.

    Seeds three tasks whose summaries exercise the daily, weekdays, and
    hourly-with-minute (the ``describeSchedule`` hourly fix) cases, then asserts
    each row's schedule line shows the expected summary and that the armed rows
    carry the server-derived relative "Next run in …" label (a delta from the
    server's next_run_at, NOT a client-recomputed instant).
    """
    agent_id = _builtin_agent_id(live_server, "hello_world")

    # Daily at 9:00 AM → "Every day at 9:00 AM".
    _create_task(live_server, agent_id, "Daily digest", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0")
    # Mon–Fri at 8:00 AM → "Weekdays at 8:00 AM".
    _create_task(
        live_server,
        agent_id,
        "Weekday triage",
        "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0",
    )
    # Hourly at :30 → "Hourly at :30" (the interval-1 non-zero-BYMINUTE fix).
    _create_task(live_server, agent_id, "Half-past sweep", "FREQ=HOURLY;BYMINUTE=30")

    page.goto(f"{live_server}/tasks")

    # All three rows render (the list query resolves against the seeded tasks).
    rows = page.locator('[data-testid="scheduled-task-row"]')
    expect(rows).to_have_count(3, timeout=30_000)

    # Each row's schedule line shows the client-derived summary text. The line
    # also carries the relative next-run suffix ("· Next run in …") for an armed
    # active task, so assert the summary is CONTAINED rather than the exact line.
    daily = _row_by_name(page, "Daily digest")
    expect(daily.get_by_test_id("task-schedule-line")).to_contain_text(
        "Every day at 9:00 AM", timeout=30_000
    )

    weekday = _row_by_name(page, "Weekday triage")
    expect(weekday.get_by_test_id("task-schedule-line")).to_contain_text("Weekdays at 8:00 AM")

    # The hourly-minute fix: interval-1 hourly with a non-zero BYMINUTE shows
    # the minute rather than a bare "Hourly".
    hourly = _row_by_name(page, "Half-past sweep")
    expect(hourly.get_by_test_id("task-schedule-line")).to_contain_text("Hourly at :30")

    # The next-run label IS shown now — but as a DELTA from the server's
    # next_run_at ("Next run in Xh/Xd"), not a client-recomputed instant. An
    # armed daily task is always <24h out, so its label is "Next run in …".
    # (The forbidden thing was a client recompute of WHICH instant is next; a
    # delta off the server value is fine.)
    expect(daily.get_by_test_id("task-next-run")).to_contain_text("Next run in", timeout=30_000)


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``) as aware UTC."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_scheduled_task_next_run_label_live_ticks_without_navigation(
    page: Page,
    live_server: str,
) -> None:
    """The relative next-run label re-renders on its own as time passes.

    The label is a delta (``next_run_at − now``); ``now`` comes from the shared
    ``useNow()`` ticker (a 30s ``setInterval`` behind ``useSyncExternalStore``),
    so the displayed value must advance WITHOUT any navigation or reload as real
    time passes. A regression to the frozen-at-first-render ``now`` (the bug this
    guards) would leave the label stuck until remount.

    Determinism comes from Playwright's clock mocking. We pin the browser clock a
    known distance BEFORE the server's authoritative ``next_run_at`` (so the
    initial label is a fixed "in 40 mins"), then fast-forward the clock past many
    30s ticks and assert the SAME row's label changed to "in 5 mins" — no reload
    between the two assertions. LLM-free: the row is pure UI state, no agent turn.
    """
    agent_id = _builtin_agent_id(live_server, "hello_world")
    task_id = _create_task(live_server, agent_id, "Ticking task", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0")

    # The server armed the task; read its authoritative next fire so the delta is
    # anchored to real server state rather than a hand-picked absolute time.
    created = httpx.get(f"{live_server}/v1/scheduled-tasks/{task_id}", timeout=10.0).json()
    next_run_at = created["next_run_at"]
    assert next_run_at, "seeded active task should have an armed next_run_at"
    fire = _parse_iso(next_run_at).replace(microsecond=0)

    # Pin the browser clock to 40 minutes BEFORE the fire, so the delta the label
    # formats is exactly 40 minutes → "Next run in 40 mins". Installed before
    # navigation so the SPA's first `Date.now()` (and every `useNow` read) is faked.
    page.clock.install(time=fire - timedelta(minutes=40))

    page.goto(f"{live_server}/tasks")

    row = _row_by_name(page, "Ticking task")
    expect(row).to_be_visible(timeout=30_000)
    next_run = row.get_by_test_id("task-next-run")
    expect(next_run).to_have_text("Next run in 40 mins", timeout=30_000)

    # Advance the clock 35 minutes — past 70 of the 30s ticks — WITHOUT touching
    # navigation. The useNow() interval fires and the same element re-renders to
    # the new delta (40 − 35 = 5 minutes). If the ticker were dead, this stays
    # "in 40 mins" and the assertion fails.
    page.clock.fast_forward("35:00")
    expect(next_run).to_have_text("Next run in 5 mins", timeout=30_000)


def test_scheduled_task_create_edit_modal_and_time_picker(
    page: Page,
    live_server: str,
) -> None:
    """Create/edit modal supports typed time input and the compact minute picker.

    This stays LLM-free: creating and editing scheduled tasks only exercise
    REST + client state, and no scheduled run fires.
    """
    agent_id = _builtin_agent_id(live_server, "hello_world")

    page.goto(f"{live_server}/tasks")

    page.get_by_test_id("new-task-button").click()
    expect(page.get_by_test_id("create-scheduled-task-dialog")).to_be_visible(timeout=30_000)
    page.get_by_test_id("task-name-input").fill("Typed time daily")
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    agent_trigger = page.get_by_test_id("task-agent-picker").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    expect(agent_trigger).to_contain_text("Claude Code", timeout=30_000)
    agent_trigger.click()
    page.get_by_role("menuitem").filter(has_text="Claude Code").click()
    expect(page.get_by_test_id("schedule-preset-trigger")).to_contain_text("Daily")

    time_input = page.get_by_test_id("schedule-time")
    time_input.fill("")
    time_input.click()
    page.keyboard.type("9:37")
    assert time_input.input_value() == "9:37"
    # force=True: focusing the time input opened the picker popover, so a normal
    # click here can resolve to the dialog overlay (see _pick_minute's note). A
    # forced click still blurs the input and closes the picker.
    page.get_by_test_id("task-name-input").click(force=True)
    expect(time_input).to_have_value("09:37 AM")
    _pick_minute(page, 37)
    expect(time_input).to_have_value("09:37 AM")
    page.get_by_test_id("create-scheduled-task-submit").click()

    created_row = _row_by_name(page, "Typed time daily")
    expect(created_row).to_be_visible(timeout=30_000)
    # `to_contain_text`: the line may also carry the server next-run suffix.
    expect(created_row.get_by_test_id("task-schedule-line")).to_contain_text(
        "Every day at 9:37 AM",
        timeout=30_000,
    )

    _create_task(
        live_server,
        agent_id,
        "Edit footer task",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )
    page.set_viewport_size({"width": 900, "height": 520})
    page.reload()

    edit_row = _row_by_name(page, "Edit footer task")
    expect(edit_row).to_be_visible(timeout=30_000)
    edit_row.hover()
    edit_row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-edit").click()

    dialog = page.get_by_test_id("create-scheduled-task-dialog")
    submit = page.get_by_test_id("create-scheduled-task-submit")
    expect(dialog).to_be_visible(timeout=30_000)
    expect(page.get_by_role("button", name="Cancel")).to_be_visible()
    expect(submit).to_be_visible()
    dialog_box = dialog.bounding_box()
    submit_box = submit.bounding_box()
    assert dialog_box is not None
    assert submit_box is not None
    assert submit_box["y"] + submit_box["height"] <= dialog_box["y"] + dialog_box["height"] + 1

    time_input.fill("")
    time_input.click()
    page.keyboard.type("10:37")
    # force=True: the time input's focus reopened the picker popover, so blur it
    # with a forced click that can't be swallowed by the dialog overlay.
    page.get_by_test_id("task-name-input").click(force=True)
    expect(time_input).to_have_value("10:37 AM")
    submit.click()

    expect(edit_row.get_by_test_id("task-schedule-line")).to_contain_text(
        "Every day at 10:37 AM",
        timeout=30_000,
    )


def test_scheduled_task_row_run_controls(
    page: Page,
    live_server: str,
) -> None:
    """Run controls: relative next-run label, and Run now → a run is recorded.

    LLM-free. The seeded task pins no host, so a manual Run now resolves no online
    host and records a ``failed`` run. This exercises the real
    POST /v1/scheduled-tasks/{id}/run path (the ⋯-menu action) end-to-end and
    confirms the run row is written, without ever dispatching an agent turn. The
    completion pill was removed from the row UI, so completion is asserted via the
    server-side run record rather than a visible badge.
    """
    agent_id = _builtin_agent_id(live_server, "hello_world")
    task_id = _create_task(
        live_server, agent_id, "Run-now target", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
    )

    page.goto(f"{live_server}/tasks")
    row = _row_by_name(page, "Run-now target")
    expect(row).to_be_visible(timeout=30_000)

    # Relative next-run renders for an armed active task — a delta off the
    # server's next_run_at ("Next run in …"), not a client-recomputed instant.
    expect(row.get_by_test_id("task-next-run")).to_contain_text("Next run in", timeout=30_000)

    # No run has fired yet → the run history is empty.
    empty = httpx.get(f"{live_server}/v1/scheduled-tasks/{task_id}/runs", timeout=10.0)
    assert empty.json()["runs"] == []

    # Trigger Run now from the ⋯ menu.
    row.hover()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-run-now").click()

    # The manual fire runs through the shared fire path and records a run row.
    # With no online host it resolves to a ``failed`` run. Poll the history until
    # the background fire lands (the POST returns 202 before the run is written).
    def _has_failed_run() -> bool:
        runs = httpx.get(f"{live_server}/v1/scheduled-tasks/{task_id}/runs", timeout=10.0).json()[
            "runs"
        ]
        return len(runs) == 1 and runs[0]["status"] == "failed"

    deadline = 0
    while not _has_failed_run() and deadline < 100:
        page.wait_for_timeout(200)
        deadline += 1
    assert _has_failed_run(), "Run now did not record a failed run within the timeout"


# ── Model + reasoning-effort selectors (PR #3331) ──────────────────────────
#
# The create/edit dialog renders a Model|Effort row (ModelEffortFields) that is
# CAPABILITY-GATED on the selected agent: shown for a native coding agent that
# carries the model/effort surface (Claude Code — the `permissionMode`
# capability), hidden for agents without it (Codex, plain-SDK agents). Both
# controls default to a "Default" sentinel; on CREATE a Default control is
# omitted from the body (agent defaults apply), and a concrete pick sends
# `model_override` / `reasoning_effort`. On EDIT the controls prefill from the
# loaded task. These tests are LLM-free like the sibling ones: they exercise
# only the create/edit dialog, REST, and the rendered row — no agent turn fires.


def _open_create_dialog(page: Page) -> None:
    """Open the New-automation dialog and confirm Claude Code is the pick.

    The picker auto-selects the first agent (Claude Code, sortRank 10) on open,
    so the model/effort row's capability gate is satisfied without any explicit
    selection — this just asserts that default holds before a test reads the
    row. Mirrors the setup in ``test_scheduled_task_create_edit_modal_and_time_picker``.

    :param page: Playwright page already navigated to ``/tasks``.
    """
    page.get_by_test_id("new-task-button").click()
    expect(page.get_by_test_id("create-scheduled-task-dialog")).to_be_visible(timeout=30_000)
    agent_trigger = page.get_by_test_id("task-agent-picker").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    expect(agent_trigger).to_contain_text("Claude Code", timeout=30_000)


def _pick_select_option(page: Page, trigger_test_id: str, option_label: str) -> None:
    """Open a Radix ``Select`` by its trigger test-id and click an option.

    The model/effort Selects portal their listbox OUTSIDE the dialog, but the
    dialog's dismiss guard (``handleSelectOpenChange``) keeps it open across the
    open/pick, so this never closes the modal. Radix items expose
    ``role="option"``; ``exact`` avoids "Sonnet 4.6" matching "Sonnet 5".

    :param page: Playwright page with the create/edit dialog open.
    :param trigger_test_id: ``"task-model-trigger"`` or ``"task-effort-trigger"``.
    :param option_label: The exact rendered option label, e.g. ``"Opus"``.
    """
    page.get_by_test_id(trigger_test_id).click()
    page.get_by_role("option", name=option_label, exact=True).click()


def test_scheduled_task_model_effort_controls_visible_for_capable_agent(
    page: Page,
    live_server: str,
) -> None:
    """The Model/Effort row shows for Claude Code, both reading 'Default'.

    Claude Code carries the ``permissionMode`` capability the dialog gates the
    model/effort surface on, so opening the create dialog (which defaults to
    Claude Code) renders ``task-model-effort-row`` with both the model and
    effort triggers defaulting to the "Default" sentinel — nothing overridden.
    """
    page.goto(f"{live_server}/tasks")
    _open_create_dialog(page)

    expect(page.get_by_test_id("task-model-effort-row")).to_be_visible(timeout=30_000)
    model_trigger = page.get_by_test_id("task-model-trigger")
    effort_trigger = page.get_by_test_id("task-effort-trigger")
    permission_trigger = page.get_by_test_id("task-permission-trigger")
    expect(model_trigger).to_be_visible()
    expect(effort_trigger).to_be_visible()
    expect(permission_trigger).to_be_visible()
    expect(model_trigger).to_have_text("Default")
    expect(effort_trigger).to_have_text("Default")
    expect(permission_trigger).to_have_text("Default")


def test_scheduled_task_model_effort_controls_hidden_for_incapable_agent(
    page: Page,
    live_server: str,
) -> None:
    """The Model/Effort row is hidden for a non-capable agent, with the hint.

    The e2e server seeds the built-in ``codex-native-ui`` ("Codex") agent, which
    carries ``approvalMode`` — NOT the ``permissionMode`` capability the
    model/effort surface is gated on. Rather than drive the create-dialog agent
    picker into Codex (it can fold into a hover-only "More" submenu when the host
    reports it unconfigured, which is fragile to click), we SEED a task against
    the Codex agent and open its EDIT dialog: the row gates on the selected
    agent, which edit mode seeds from the loaded task, so the row must be absent
    and the "uses defaults" helper hint shown instead. (Chosen because the e2e
    server does register Codex; the seeded-edit path is the deterministic way to
    exercise the hidden branch.)
    """
    codex_agent_id = _builtin_agent_id(live_server, "codex-native-ui")
    _create_task(live_server, codex_agent_id, "Codex daily", "FREQ=DAILY;BYHOUR=9;BYMINUTE=0")

    page.goto(f"{live_server}/tasks")
    row = _row_by_name(page, "Codex daily")
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-edit").click()

    dialog = page.get_by_test_id("create-scheduled-task-dialog")
    expect(dialog).to_be_visible(timeout=30_000)
    # Capability gate off → the row is never rendered (nor the permission
    # control), and the "uses defaults" helper hint stands in for it.
    expect(page.get_by_test_id("task-model-effort-row")).to_be_hidden()
    expect(page.get_by_test_id("task-permission-control")).to_be_hidden()
    expect(dialog.get_by_text("Uses this agent")).to_be_visible()


def test_scheduled_task_create_persists_model_and_effort(
    page: Page,
    live_server: str,
) -> None:
    """Picking a concrete Model + Effort on create persists both via the API.

    Opens the create dialog on Claude Code, picks Opus + High through the
    ``task-model-trigger`` / ``task-effort-trigger`` selects, submits, then reads
    the created row back from ``GET /v1/scheduled-tasks`` and asserts its
    ``model_override`` / ``reasoning_effort`` are the persisted picks (asserting
    the server-side values is more robust than re-reading the row text). Opus =
    the ``"opus"`` model alias, High = the ``"high"`` effort — the shared option
    vocabulary the dialog renders; a pick sends those verbatim.
    """
    page.goto(f"{live_server}/tasks")
    _open_create_dialog(page)

    page.get_by_test_id("task-name-input").fill("Model effort create")
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    _pick_select_option(page, "task-model-trigger", "Opus")
    _pick_select_option(page, "task-effort-trigger", "High")
    expect(page.get_by_test_id("task-model-trigger")).to_have_text("Opus")
    expect(page.get_by_test_id("task-effort-trigger")).to_have_text("High")
    page.get_by_test_id("create-scheduled-task-submit").click()

    # The row renders once the create resolves; assert the persisted API values.
    expect(_row_by_name(page, "Model effort create")).to_be_visible(timeout=30_000)
    tasks = httpx.get(f"{live_server}/v1/scheduled-tasks", timeout=10.0).json()["scheduled_tasks"]
    created = next(t for t in tasks if t["name"] == "Model effort create")
    assert created["model_override"] == "opus", created
    assert created["reasoning_effort"] == "high", created


def test_scheduled_task_create_default_omits_model_and_effort(
    page: Page,
    live_server: str,
) -> None:
    """Leaving both controls on 'Default' persists null model/effort.

    A create that never touches the model/effort controls must omit both fields
    so the row inherits the agent's configured model + effort. Confirms the
    "Default" sentinel maps to a null override (not the literal sentinel string).
    """
    page.goto(f"{live_server}/tasks")
    _open_create_dialog(page)

    page.get_by_test_id("task-name-input").fill("Default model effort")
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    # Controls left on their default "Default" sentinel — no pick.
    page.get_by_test_id("create-scheduled-task-submit").click()

    expect(_row_by_name(page, "Default model effort")).to_be_visible(timeout=30_000)
    tasks = httpx.get(f"{live_server}/v1/scheduled-tasks", timeout=10.0).json()["scheduled_tasks"]
    created = next(t for t in tasks if t["name"] == "Default model effort")
    assert created["model_override"] is None, created
    assert created["reasoning_effort"] is None, created


def test_scheduled_task_edit_prefills_model_and_effort(
    page: Page,
    live_server: str,
) -> None:
    """The edit dialog prefills the Model/Effort controls from the loaded task.

    Seeds a Claude Code task carrying ``model_override="opus"`` +
    ``reasoning_effort="high"``, opens its Edit dialog, and asserts the
    ``task-model-trigger`` / ``task-effort-trigger`` render the stored picks
    ("Opus" / "High"), not the "Default" sentinel — the round-trip the create
    test's persistence feeds into.
    """
    claude_agent_id = _builtin_agent_id(live_server, "claude-native-ui")
    _create_task(
        live_server,
        claude_agent_id,
        "Prefill target",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        model_override="opus",
        reasoning_effort="high",
    )

    page.goto(f"{live_server}/tasks")
    row = _row_by_name(page, "Prefill target")
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-edit").click()

    dialog = page.get_by_test_id("create-scheduled-task-dialog")
    expect(dialog).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("task-model-effort-row")).to_be_visible()
    expect(page.get_by_test_id("task-model-trigger")).to_have_text("Opus")
    expect(page.get_by_test_id("task-effort-trigger")).to_have_text("High")


def test_scheduled_task_edit_switches_the_harness(
    page: Page,
    live_server: str,
) -> None:
    """The edit dialog can rebind an existing automation to another harness.

    Seeds a Codex task, switches it to Claude Code through the edit dialog's
    picker, and asserts the PERSISTED ``agent_id`` changed on the SAME task id —
    the whole point, since recreating the task would mint a new id and drop its
    run history. (The route tests cover what a switch does to the per-agent
    settings stored beside the binding.)
    """
    codex_agent_id = _builtin_agent_id(live_server, "codex-native-ui")
    claude_agent_id = _builtin_agent_id(live_server, "claude-native-ui")
    task_id = _create_task(
        live_server,
        codex_agent_id,
        "Switch me",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    )

    page.goto(f"{live_server}/tasks")
    row = _row_by_name(page, "Switch me")
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-edit").click()

    dialog = page.get_by_test_id("create-scheduled-task-dialog")
    expect(dialog).to_be_visible(timeout=30_000)
    agent_trigger = page.get_by_test_id("task-agent-picker").get_by_test_id(
        "new-chat-landing-agent-select"
    )
    # Seeded from the task's own agent, not the first listed one.
    expect(agent_trigger).to_contain_text("Codex", timeout=30_000)
    agent_trigger.click()
    page.get_by_role("menuitem").filter(has_text="Claude Code").click()
    expect(agent_trigger).to_contain_text("Claude Code")
    page.get_by_test_id("create-scheduled-task-submit").click()

    expect(dialog).to_be_hidden(timeout=30_000)
    task = httpx.get(f"{live_server}/v1/scheduled-tasks/{task_id}", timeout=10.0).json()
    assert task["agent_id"] == claude_agent_id, task
    assert task["id"] == task_id, task


# ── Permission-mode selector ───────────────────────────────────────────────
#
# The dialog renders a Permission-mode select alongside Model/Effort, gated on
# the same ``permissionMode`` capability (Claude Code). Because an automation
# fires a fresh session each run, the whole launch vocabulary is offered —
# including the launch-only ``dontAsk`` / ``bypassPermissions``. Left on
# "Default" the field is omitted (agent default); a concrete pick persists
# ``permission_mode``, which the fire path turns into the runner's
# ``--permission-mode`` launch arg. Like the sibling tests these are LLM-free:
# they exercise only the dialog, REST, and the rendered row.


def test_scheduled_task_create_persists_permission_mode(
    page: Page,
    live_server: str,
) -> None:
    """Picking a Permission mode on create persists it via the API.

    Opens the create dialog on Claude Code, picks "Accept edits" through the
    ``task-permission-trigger`` select, submits, then reads the created row back
    from ``GET /v1/scheduled-tasks`` and asserts its ``permission_mode`` is the
    persisted pick (``"acceptEdits"`` — the wire value behind the "Accept edits"
    label).
    """
    page.goto(f"{live_server}/tasks")
    _open_create_dialog(page)

    page.get_by_test_id("task-name-input").fill("Permission create")
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    _pick_select_option(page, "task-permission-trigger", "Accept edits")
    expect(page.get_by_test_id("task-permission-trigger")).to_have_text("Accept edits")
    page.get_by_test_id("create-scheduled-task-submit").click()

    expect(_row_by_name(page, "Permission create")).to_be_visible(timeout=30_000)
    tasks = httpx.get(f"{live_server}/v1/scheduled-tasks", timeout=10.0).json()["scheduled_tasks"]
    created = next(t for t in tasks if t["name"] == "Permission create")
    assert created["permission_mode"] == "acceptEdits", created


def test_scheduled_task_create_default_omits_permission_mode(
    page: Page,
    live_server: str,
) -> None:
    """Leaving the Permission control on 'Default' persists a null mode.

    A create that never touches the permission control must omit the field so
    the row inherits the agent's configured mode. Confirms the "Default"
    sentinel maps to a null override, not the literal sentinel string.
    """
    page.goto(f"{live_server}/tasks")
    _open_create_dialog(page)

    page.get_by_test_id("task-name-input").fill("Default permission")
    page.get_by_test_id("task-prompt-input").fill("Summarize the day.")
    # Permission control left on its default "Default" sentinel — no pick.
    page.get_by_test_id("create-scheduled-task-submit").click()

    expect(_row_by_name(page, "Default permission")).to_be_visible(timeout=30_000)
    tasks = httpx.get(f"{live_server}/v1/scheduled-tasks", timeout=10.0).json()["scheduled_tasks"]
    created = next(t for t in tasks if t["name"] == "Default permission")
    assert created["permission_mode"] is None, created


def test_scheduled_task_edit_prefills_permission_mode(
    page: Page,
    live_server: str,
) -> None:
    """The edit dialog prefills the Permission control from the loaded task.

    Seeds a Claude Code task carrying ``permission_mode="bypassPermissions"`` (a
    launch-only mode, valid for automations), opens its Edit dialog, and asserts
    ``task-permission-trigger`` renders the stored pick ("Bypass permissions"),
    not the "Default" sentinel.
    """
    claude_agent_id = _builtin_agent_id(live_server, "claude-native-ui")
    _create_task(
        live_server,
        claude_agent_id,
        "Permission prefill",
        "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        permission_mode="bypassPermissions",
    )

    page.goto(f"{live_server}/tasks")
    row = _row_by_name(page, "Permission prefill")
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    row.get_by_test_id("task-row-menu").click()
    page.get_by_test_id("task-edit").click()

    dialog = page.get_by_test_id("create-scheduled-task-dialog")
    expect(dialog).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("task-permission-control")).to_be_visible()
    expect(page.get_by_test_id("task-permission-trigger")).to_have_text("Bypass permissions")
