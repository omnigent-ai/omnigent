"""Browser e2e: forking an orchestrator while its sub-agent is still running.

Forking while a child runs must warn the user up front and leave a notice in
the fork, while the child's ownership, cancellation and result delivery stay
with the original session.

The worker's single model call is parked on the mock gate so it is "running"
for exactly as long as the journey needs; ``POST /gate/release`` ends it.
Both agents run on the openai-agents SDK harness against the mock model. The
whole journey is driven first and asserted afterwards so a recording of a
failing run still shows every step. Set ``OMNIGENT_E2E_RECORD_DIR`` to film
it and ``OMNIGENT_E2E_EVIDENCE_DIR`` to keep API snapshots.

Excluded from default ``pytest`` runs; invoke with::

    pytest tests/e2e_ui/fork_session/test_fork_during_running_subagent.py -v
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect
from playwright.sync_api import Error as PlaywrightError

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm

pytestmark = [pytest.mark.timeout(900, method="signal")]

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_COMPOSER_LABEL = "Message the agent"
_ORCHESTRATOR_NAME = "fork_orchestrator"
_INBOX_EMPTY = "Inbox is empty — no completed tasks."
_DISPATCH_ACK = "Worker dispatched; its result will arrive in my inbox."
_STATUS_REPLY = "I checked my inbox and sub-agent list; see the tool results above."
_CANCEL_REPLY = "I asked the runtime to stop the worker; see the tool result above."
_WAKE_REPLY = "I collected the worker's result from my inbox; see the tool result above."
# How long the fork is watched for the finished worker's result after the
# original has already shown it; delivery to both would happen at the same time.
_FORK_WATCH_S = 20.0
# Transcript card titles (web/src/lib/toolTitle.ts); sys_session_send has no
# friendly title and renders its raw name.
_TOOL_CARD_TITLE = {
    "sys_session_send": r"^sys_session_send\(",
    "sys_read_inbox": r"^Read inbox",
    "sys_session_list": r"^List child sessions",
    "sys_cancel_task": r"^Cancel task",
}
_FOLD_LABEL = re.compile(r"^(Worked( for .+)?|Called \d+ tools?)$")


def _orchestrator_yaml(orchestrator_model: str, worker_model: str) -> str:
    # No auth block: the runner's OPENAI_BASE_URL already targets the mock.
    return f"""\
name: {_ORCHESTRATOR_NAME}
prompt: |
  You are an orchestrator. When the user asks you to send work to the worker,
  call sys_session_send with agent "worker" and then end your turn. When a
  sub-agent wake notice arrives, call sys_read_inbox and report the result.
  When asked for status, call sys_read_inbox and sys_session_list and report
  what they return. When asked to stop the worker, call sys_cancel_task.

executor:
  model: {orchestrator_model}
  harness: openai-agents

tools:
  worker:
    type: agent
    description: Worker sub-agent that carries out a long task.
    executor:
      model: {worker_model}
      harness: openai-agents
    prompt: |
      You are the worker. Do the task and report the result.
"""


def _configure_orchestrator_turns(
    mock_url: str, *, dispatch_token: str, status_token: str, worker_model: str, marker: str
) -> None:
    """Script every turn except the fork's cancel, which needs the worker id.

    Content routing concatenates all user messages, and the longest matching
    token wins, so the fork's tokens are longer than the dispatch token the
    fork inherits in its copied history. The wake turn carries only the
    dispatch token, so its two responses trail the dispatch queue.
    """
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": "long-task",
                                "args": "Carry out the long task and report when done.",
                            }
                        ),
                    }
                ]
            },
            {"text": _DISPATCH_ACK},
            {
                "tool_calls": [
                    {"call_id": "call_wake_drain", "name": "sys_read_inbox", "arguments": "{}"}
                ]
            },
            {"text": _WAKE_REPLY},
        ],
        match=dispatch_token,
        required_tools=["sys_session_send"],
    )
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {"call_id": "call_fork_inbox", "name": "sys_read_inbox", "arguments": "{}"}
                ]
            },
            {
                "tool_calls": [
                    {"call_id": "call_fork_list", "name": "sys_session_list", "arguments": "{}"}
                ]
            },
            {"text": _STATUS_REPLY},
        ],
        match=status_token,
        required_tools=["sys_session_send"],
    )
    configure_mock_llm(
        mock_url,
        [{"text": f"Worker finished the long task. {marker}", "block": True}],
        key=worker_model,
    )


def _configure_fork_cancel_turn(mock_url: str, *, cancel_token: str, worker_id: str) -> None:
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_fork_cancel",
                        "name": "sys_cancel_task",
                        "arguments": json.dumps({"task_id": worker_id}),
                    }
                ]
            },
            {"text": _CANCEL_REPLY},
        ],
        match=cancel_token,
        required_tools=["sys_session_send"],
    )


def _create_orchestrator_session(base_url: str, runner_id: str, yaml_text: str) -> str:
    bundle = bundle_files({f"{_ORCHESTRATOR_NAME}.yaml": yaml_text.encode()})
    create = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0, trust_env=False
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    bind_session_runner(
        httpx.patch, base_url, session_id, runner_id, timeout=10.0, trust_env=False
    )
    return session_id


def _gate_pending(mock_url: str) -> bool:
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _wait_gate_pending(mock_url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gate_pending(mock_url):
            return
        time.sleep(0.25)
    raise AssertionError("worker never reached its gated model call")


def _child_sessions(base_url: str, session_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/child_sessions", timeout=10.0, trust_env=False
    )
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def _items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0, trust_env=False
    )
    resp.raise_for_status()
    payload = resp.json()
    assert not payload.get("has_more"), f"items for {session_id} truncated at limit=200"
    return list(payload["data"])


def _tool_output(items: list[dict[str, Any]], call_id_prefix: str) -> str:
    for item in items:
        if item.get("type") != "function_call_output":
            continue
        data = item.get("data") or {}
        call_id = str(item.get("call_id") or data.get("call_id") or "")
        if call_id.startswith(call_id_prefix):
            return str(item.get("output") or data.get("output") or "")
    return ""


def _transcript_text(items: list[dict[str, Any]], *, include_tool_outputs: bool = True) -> str:
    """Text a reader of the transcript sees: messages, notice cards and, optionally, tool outputs.

    Bookkeeping items (resource events keyed by the source session's id) and
    ``is_meta`` messages (agent-only context the UI hides) are left out, and
    a notice addressed to the user must be a message or a notice card, so a
    session id inside a tool result cannot pass for one.
    """
    parts: list[str] = []
    for item in items:
        data = item.get("data") or {}
        if item.get("type") == "message":
            if data.get("is_meta") or item.get("is_meta"):
                continue
            content = data.get("content") or item.get("content") or []
            if isinstance(content, str):
                parts.append(content)
            else:
                parts.extend(
                    str(block.get("text", "")) for block in content if isinstance(block, dict)
                )
        elif item.get("type") == "error":
            parts.extend(
                str(data.get(field) or item.get(field) or "")
                for field in ("title", "message", "cause", "remediation")
            )
        elif include_tool_outputs and item.get("type") == "function_call_output":
            parts.append(str(item.get("output") or data.get("output") or ""))
    return "\n".join(parts)


def _listed_sub_agent_ids(sys_session_list_output: str) -> list[str]:
    try:
        parsed = json.loads(sys_session_list_output)
    except ValueError:
        return []
    rows = parsed.get("sub_agents") if isinstance(parsed, dict) else None
    return [str(row.get("conversation_id")) for row in rows or [] if isinstance(row, dict)]


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    expect(composer).to_be_enabled(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_reply(page: Page, text: str, timeout_ms: int = 120_000) -> None:
    expect(page.locator(_ASSISTANT, has_text=text).first).to_be_visible(timeout=timeout_ms)


def _expand_tool_card(page: Page, tool_name: str) -> None:
    """Open the transcript card for ``tool_name`` so its output is on screen.

    A settled multi-step turn folds its calls behind a "Worked for Ns" (or
    "Called N tools") disclosure that mounts open and closes a frame later,
    so opening it is retried until the card's trigger is on screen; the card
    itself is collapsed by default.
    """
    page.wait_for_timeout(1_000)
    trigger = page.get_by_role("button", name=re.compile(_TOOL_CARD_TITLE[tool_name])).last
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not (trigger.count() and trigger.is_visible()):
        for fold in page.get_by_role("button", name=_FOLD_LABEL).all():
            try:
                if fold.get_attribute("aria-expanded") != "true":
                    fold.click()
            except PlaywrightError:
                continue
        page.wait_for_timeout(500)
    expect(trigger).to_be_visible(timeout=5_000)
    if trigger.get_attribute("aria-expanded") != "true":
        trigger.click()
    trigger.scroll_into_view_if_needed()


def _record_on_screen(page: Page, obs: dict[str, Any], key: str, pattern: str) -> None:
    """Note whether text matching ``pattern`` is actually visible in the viewport."""
    locator = page.get_by_text(re.compile(pattern)).first
    try:
        expect(locator).to_be_visible(timeout=10_000)
        locator.scroll_into_view_if_needed()
        visible = True
    except AssertionError:
        visible = False
    obs.setdefault("on_screen", {})[key] = visible


def _pace(seconds: float) -> None:
    """Pause only while filming, so each step stays readable; unrecorded runs skip it."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        time.sleep(seconds)


def _save(evidence_dir: Path | None, name: str, payload: Any) -> None:
    if evidence_dir is None:
        return
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def _screenshot(page: Page, evidence_dir: Path | None, name: str) -> None:
    if evidence_dir is not None:
        page.screenshot(path=str(evidence_dir / name))


def _run_journey(
    browser: Browser,
    *,
    base_url: str,
    mock_url: str,
    runner_id: str,
    original_id: str,
    dispatch_token: str,
    status_token: str,
    cancel_token: str,
    marker: str,
    evidence_dir: Path | None,
    obs: dict[str, Any],
) -> dict[str, Any]:
    """Drive the user journey, filling ``obs`` as it goes and asserting nothing."""
    obs["original_id"] = original_id
    viewport = {"width": 1280, "height": 720}
    context_kwargs: dict[str, Any] = {"viewport": viewport}
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        # The e2e_ui conftest injects record_video_dir; keep the clip at full size.
        context_kwargs["record_video_size"] = viewport
    context = browser.new_context(**context_kwargs)
    try:
        page = context.new_page()
        page.goto(f"{base_url}/c/{original_id}")

        # 1. Dispatch the worker; the dispatch turn ends with the ack while the
        #    worker parks on the mock gate.
        _send(page, f"Please send the worker its long task now. {dispatch_token}")
        _wait_reply(page, _DISPATCH_ACK)
        _wait_gate_pending(mock_url)
        children = _child_sessions(base_url, original_id)
        obs["children_before_fork"] = children
        if not children:
            raise AssertionError("the orchestrator never created a worker child session")
        worker_id = str(children[0]["id"])
        obs["worker_id"] = worker_id
        _configure_fork_cancel_turn(mock_url, cancel_token=cancel_token, worker_id=worker_id)
        _expand_tool_card(page, "sys_session_send")
        _record_on_screen(page, obs, "dispatch_ack", re.escape(_DISPATCH_ACK))
        _record_on_screen(page, obs, "dispatch_receipt", r"Result will appear in your inbox")
        _screenshot(page, evidence_dir, "01-original-dispatched.png")
        _pace(2)

        # 2. Fork from the header menu while the worker is still running.
        page.get_by_test_id("header-conversation-actions").click()
        page.get_by_role("menuitem", name="Fork", exact=True).click()
        dialog = page.get_by_test_id("fork-session-dialog")
        expect(dialog).to_be_visible(timeout=15_000)
        _pace(1.5)
        _record_on_screen(
            page, obs, "dialog_subagent_warning", r"sub-agents? (is|are) still running"
        )
        obs["dialog_text"] = dialog.inner_text()
        _screenshot(page, evidence_dir, "02-fork-dialog.png")
        _pace(2)
        page.get_by_test_id("fork-session-submit").click()
        expect(page).to_have_url(
            re.compile(rf"/c/(?!{re.escape(original_id)})[0-9a-f]{{32}}"), timeout=30_000
        )
        fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
        obs["fork_id"] = fork_id
        fork_snapshot = httpx.get(
            f"{base_url}/v1/sessions/{fork_id}", timeout=10.0, trust_env=False
        )
        obs["fork_snapshot"] = fork_snapshot.json() if fork_snapshot.status_code == 200 else None
        # A non-coding fork is created unbound; bind it to a runner so it can take a turn.
        bind_session_runner(
            httpx.patch, base_url, fork_id, runner_id, timeout=10.0, trust_env=False
        )
        page.reload()
        expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
        _pace(1.5)

        # 3. In the fork, ask for status.
        _send(page, f"Status check: what is the worker doing right now? {status_token}")
        _wait_reply(page, _STATUS_REPLY)
        _expand_tool_card(page, "sys_read_inbox")
        _record_on_screen(page, obs, "fork_inbox_empty", re.escape(_INBOX_EMPTY))
        _expand_tool_card(page, "sys_session_list")
        _record_on_screen(page, obs, "fork_sub_agents_empty", r'"sub_agents":\s*\[\]')
        _record_on_screen(page, obs, "fork_subagent_notice", r"stayed with the original session")
        fork_items = _items(base_url, fork_id)
        obs["fork_inbox_output"] = _tool_output(fork_items, "call_fork_inbox")
        obs["fork_session_list_output"] = _tool_output(fork_items, "call_fork_list")
        obs["fork_listed_sub_agent_ids"] = _listed_sub_agent_ids(obs["fork_session_list_output"])
        obs["fork_children_after_status"] = _child_sessions(base_url, fork_id)
        _save(evidence_dir, "fork-items-after-status.json", fork_items)
        _screenshot(page, evidence_dir, "03-fork-status.png")
        _pace(3)

        # 4. In the fork, try to stop the worker.
        _send(page, f"Please stop the worker now. {cancel_token}")
        _wait_reply(page, _CANCEL_REPLY)
        _expand_tool_card(page, "sys_cancel_task")
        _record_on_screen(
            page, obs, "fork_cancel_refused", r"Error: no in-flight task with task_id"
        )
        fork_items = _items(base_url, fork_id)
        obs["fork_cancel_output"] = _tool_output(fork_items, "call_fork_cancel")
        obs["gate_pending_after_cancel"] = _gate_pending(mock_url)
        obs["fork_text_before_finish"] = _transcript_text(fork_items)
        obs["fork_item_count_before_finish"] = len(fork_items)
        _save(evidence_dir, "fork-items-after-cancel.json", fork_items)
        _screenshot(page, evidence_dir, "04-fork-cancel.png")
        _pace(3)

        # 5. Watch the original while the worker finishes.
        page.goto(f"{base_url}/c/{original_id}")
        expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
        _pace(1.5)
        httpx.post(f"{mock_url}/gate/release", timeout=5.0, trust_env=False).raise_for_status()
        obs["released_at"] = time.time()
        original_woke = False
        try:
            _wait_reply(page, _WAKE_REPLY, timeout_ms=90_000)
            original_woke = True
        except AssertionError:
            pass
        if original_woke:
            _expand_tool_card(page, "sys_read_inbox")
            _record_on_screen(page, obs, "original_wake_notice", r"finished \(completed\)")
            _record_on_screen(page, obs, "original_result_marker", re.escape(marker))
        obs["original_woke"] = original_woke
        original_items = _items(base_url, original_id)
        obs["original_wake_inbox_output"] = _tool_output(original_items, "call_wake_drain")
        obs["original_has_marker"] = marker in _transcript_text(original_items)
        _save(evidence_dir, "original-items-after-finish.json", original_items)
        _screenshot(page, evidence_dir, "05-original-woke.png")
        _pace(3)

        # 6. Back in the fork: does anything arrive?
        page.goto(f"{base_url}/c/{fork_id}")
        expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
        deadline = time.monotonic() + _FORK_WATCH_S
        while time.monotonic() < deadline:
            fork_items = _items(base_url, fork_id)
            if len(fork_items) != obs["fork_item_count_before_finish"]:
                break
            time.sleep(2.0)
        page.reload()
        expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
        _pace(3)
        fork_items = _items(base_url, fork_id)
        obs["fork_after_marker_on_screen"] = page.get_by_text(marker).count() > 0
        obs["fork_after_text"] = _transcript_text(fork_items)
        obs["fork_after_messages"] = _transcript_text(fork_items, include_tool_outputs=False)
        obs["fork_item_count_after_finish"] = len(fork_items)
        obs["fork_children_after_finish"] = _child_sessions(base_url, fork_id)
        worker_snapshot = httpx.get(
            f"{base_url}/v1/sessions/{worker_id}", timeout=10.0, trust_env=False
        )
        obs["worker_snapshot"] = (
            worker_snapshot.json() if worker_snapshot.status_code == 200 else None
        )
        _save(evidence_dir, "fork-items-after-finish.json", fork_items)
        _screenshot(page, evidence_dir, "06-fork-after-finish.png")
        _pace(3)
    finally:
        context.close()
    return obs


def test_fork_during_running_subagent_keeps_fork_informed(
    browser: Browser,
    live_server: str,
    mock_llm_server_url: str,
    runner_id: str,
    tmp_path: Path,
) -> None:
    """A fork made while a sub-agent runs is told the worker stayed with the original.

    The dialog warns before cloning and the fork's transcript carries a visible
    notice naming the original session; the worker itself stays owned by the
    original, so the fork cannot list or cancel it and never receives its
    result.
    """
    reset_mock_llm(mock_llm_server_url)
    uid = uuid.uuid4().hex[:6]
    orchestrator_model = f"mock-fork-orchestrator-{uid}"
    worker_model = f"mock-fork-worker-{uid}"
    marker = f"RESULT-MARKER-{uid}"
    dispatch_token = f"dsp-{uid}"
    status_token = f"fork-status-{uid}-probe"
    cancel_token = f"fork-cancel-{uid}-stopnow"
    evidence_env = os.environ.get("OMNIGENT_E2E_EVIDENCE_DIR")
    evidence_dir = Path(evidence_env) if evidence_env else tmp_path / "evidence"

    _configure_orchestrator_turns(
        mock_llm_server_url,
        dispatch_token=dispatch_token,
        status_token=status_token,
        worker_model=worker_model,
        marker=marker,
    )
    original_id = _create_orchestrator_session(
        live_server, runner_id, _orchestrator_yaml(orchestrator_model, worker_model)
    )
    obs: dict[str, Any] = {}
    try:
        _run_journey(
            browser,
            base_url=live_server,
            mock_url=mock_llm_server_url,
            runner_id=runner_id,
            original_id=original_id,
            dispatch_token=dispatch_token,
            status_token=status_token,
            cancel_token=cancel_token,
            marker=marker,
            evidence_dir=evidence_dir,
            obs=obs,
        )
    finally:
        # Always keep the observations, even when the mock server is gone.
        try:
            if _gate_pending(mock_llm_server_url):
                httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0, trust_env=False)
        except httpx.HTTPError:
            pass
        _save(evidence_dir, "observations.json", obs)

    worker_id = obs["worker_id"]
    fork_id = obs["fork_id"]
    assert obs["original_has_marker"], (
        f"the worker's result never reached the original session {original_id}; "
        "the journey did not get far enough to judge the fork"
    )

    on_screen = obs["on_screen"]
    failures: list[str] = []
    if not on_screen.get("dialog_subagent_warning") or not re.search(
        r"sub-?agent", obs["dialog_text"], re.IGNORECASE
    ):
        failures.append(
            "the fork dialog did not warn that a sub-agent was still running; its text was: "
            + " ".join(obs["dialog_text"].split())
        )
    if not on_screen.get("fork_subagent_notice") or original_id not in obs["fork_after_messages"]:
        failures.append(
            f"fork {fork_id} showed no notice naming the original session {original_id} for "
            f"the worker {worker_id} that stayed behind"
        )
    # Ownership stays with the original: the fork neither lists, cancels nor
    # hears from the worker, and its own inbox stays empty.
    if (
        obs["fork_children_after_status"]
        or worker_id in obs["fork_listed_sub_agent_ids"]
        or "Inbox is empty" not in obs["fork_inbox_output"]
    ):
        failures.append(
            f"fork {fork_id} adopted the worker: children {obs['fork_children_after_status']}, "
            f"sub_agents {obs['fork_listed_sub_agent_ids']}, inbox {obs['fork_inbox_output']!r}"
        )
    if "Error: no in-flight task" not in obs["fork_cancel_output"]:
        failures.append(
            f"the fork could act on the original's worker: sys_cancel_task returned "
            f"{obs['fork_cancel_output']!r}"
        )
    if not obs["original_woke"] or marker in obs["fork_after_text"]:
        failures.append(
            f"the worker's result must wake the original {original_id} only "
            f"(woke={obs['original_woke']}, marker in fork={marker in obs['fork_after_text']}, "
            f"fork items {obs['fork_item_count_before_finish']} -> "
            f"{obs['fork_item_count_after_finish']} over {_FORK_WATCH_S:.0f}s)"
        )
    assert not failures, "\n".join(failures)
