"""E2E tests for the AP-side ``sys_terminal_*`` tool family.

Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §8.3 — these tests
exercise the full Omnigent integration path: omnigent YAML
declares ``terminals:``, the compat translator threads it onto
``AgentSpec.terminals``, the AP-side ``ToolManager`` registers
the ``sys_terminal_*`` family, the LLM invokes them, the
:class:`omnigent.terminals.TerminalRegistry` spawns real
tmux sessions, and (per §4.4 corrected) cleanup fires only at
conversation deletion / Omnigent shutdown — NOT at workflow exit.

Skipped if tmux isn't installed on the host running the test.

Usage::

    pytest tests/e2e/test_sys_terminal_e2e.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import json
import shutil

import httpx
import pytest

from tests.e2e.conftest import poll_until_terminal

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; sys_terminal_* e2e tests need tmux on PATH",
)


def _get_function_call_outputs(
    client: httpx.Client,
    conversation_id: str,
    tool_name: str,
) -> list[str]:
    """
    Return raw outputs of every ``tool_name`` call in conversation order.

    Walks ``function_call`` and ``function_call_output`` items in the
    conversation. Used so assertions land on deterministic tool
    output strings, not on flaky LLM prose summaries.

    :param client: HTTP client.
    :param conversation_id: Conversation to inspect.
    :param tool_name: Only outputs of calls to this tool are returned.
    :returns: Ordered list of raw output strings.
    """
    resp = client.get(f"/v1/sessions/{conversation_id}/items?limit=200")
    resp.raise_for_status()
    items = resp.json()["data"]
    calls_by_id: dict[str, dict] = {}
    for item in items:
        if item.get("type") == "function_call" and item.get("name") == tool_name:
            calls_by_id[item["call_id"]] = item
    outputs: list[str] = []
    for item in items:
        if item.get("type") == "function_call_output":
            cid = item.get("call_id")
            if cid in calls_by_id:
                outputs.append(str(item.get("output", "")))
    return outputs


def test_sys_terminal_basic_round_trip_e2e(
    live_server: str,
    sys_terminal_test_agent: str,
    http_client: httpx.Client,
) -> None:
    """
    Real LLM drives the full ``sys_terminal_*`` round trip
    against a real tmux. Asserts on the raw tool outputs (not
    prose) so flaky LLM wording can't fail the test.

    What this verifies:
      1. The compat translator threaded ``terminals:`` from
         omnigent YAML through to ``AgentSpec.terminals``.
      2. The AP-side ToolManager registered the
         ``sys_terminal_*`` family from
         ``ToolManager._register_terminal_tools``.
      3. The LLM successfully invoked launch + send + read.
      4. The :class:`TerminalRegistry` spawned a real tmux
         session; ``send`` reached it; ``read`` saw the marker.

    What breaks if this fails (top suspects):
      - ``AgentSpec.terminals=None`` after translation → tools
        not registered → "tool not available" mid-conversation.
      - Workflow path differs from test ToolManager registration
        path → tools register in tests but not at runtime.
      - tmux subprocess spawn fails silently → empty pane reads.
    """
    marker = "TERMINAL_E2E_MARKER_AAAA"
    prompt = (
        f"Use sys_terminal_launch to start the 'bash' terminal with "
        f"session 's1'. Then use sys_terminal_send to type "
        f"'echo {marker}' followed by Enter. Wait briefly for the "
        f"output, then call sys_terminal_read on session 's1'. "
        f"Report what you saw. Do this in one go, then reply 'done'."
    )
    resp = http_client.post(
        "/v1/responses",
        json={
            "model": sys_terminal_test_agent,
            "input": prompt,
            "stream": False,
        },
        timeout=180.0,
    )
    resp.raise_for_status()
    body = resp.json()
    response_id = body["id"]
    body = poll_until_terminal(http_client, response_id, timeout=180)
    assert body["status"] == "completed", (
        f"Workflow failed before completion. Status={body['status']!r}; "
        f"error={body.get('error')!r}. If 'failed' with an exception "
        f"about ``sys_terminal_launch``, the tools didn't register on "
        f"the AP-side ToolManager."
    )

    conv_id = body["conversation"]["id"]

    # The LLM must have called launch — otherwise the rest of the
    # test would be testing nothing.
    launches = _get_function_call_outputs(http_client, conv_id, "sys_terminal_launch")
    assert len(launches) >= 1, (
        f"sys_terminal_launch was never called; conv_id={conv_id}. "
        f"If 0 calls, either the LLM ignored the prompt or the tool "
        f"wasn't on the schema (registration regression)."
    )
    launch_result = json.loads(launches[0])
    assert launch_result.get("status") == "launched", (
        f"First launch should report status='launched'; got "
        f"{launch_result!r}. If the value is 'already_running', the "
        f"registry reused stale state from a prior test run."
    )

    # The marker must appear in at least one read output. We
    # don't constrain the LLM's call ordering (it might read
    # twice, retry, etc.), only that the data flowed.
    reads = _get_function_call_outputs(http_client, conv_id, "sys_terminal_read")
    assert len(reads) >= 1, f"sys_terminal_read was never called; conv_id={conv_id}."
    combined_screens = " ".join(reads)
    assert marker in combined_screens, (
        f"Echo marker {marker!r} not seen in any sys_terminal_read "
        f"output. Reads: {reads!r}. If empty, the send didn't reach "
        f"tmux. If reads have a prompt but not the echo, the bash "
        f"command failed in tmux (e.g. shell-init error)."
    )


def test_sys_terminal_full_workflow_e2e(
    live_server: str,
    sys_terminal_test_agent: str,
    http_client: httpx.Client,
) -> None:
    """
    A coherent task that exercises ALL FIVE ``sys_terminal_*`` tools
    in one conversation. The LLM is asked to perform a small shell
    investigation, then verify the cleanup succeeded.

    Flow:
      1. ``sys_terminal_launch`` — start ``bash:investigate``.
      2. ``sys_terminal_send`` — write a marker to a tmp file.
      3. ``sys_terminal_read`` — capture the pane confirming the
         echo + the file-write completed.
      4. ``sys_terminal_list`` — confirm the registry shows
         ``bash:investigate`` as running.
      5. ``sys_terminal_close`` — kill the session.
      6. ``sys_terminal_list`` again — confirm the registry no
         longer reports ``bash:investigate``.

    What this catches that the focused tests don't:
      - ``sys_terminal_list`` schema/dispatch never gets exercised
        through the LLM in the focused tests; a malformed list
        schema or wrong return shape would only fail here.
      - ``sys_terminal_close`` likewise — the focused tests
        verify the registry-level close, but not the LLM-driven
        path through the AP-side ToolManager.
      - The post-close list is the only e2e check that close
        actually removed the registry entry (not just killed
        the process); without it, a leak that only surfaces
        across multiple turns / closes would be invisible.

    The prompt tells the LLM the sequence explicitly; the
    assertions check tool names appear in conversation items
    rather than trusting the LLM's prose summary. LLM ordering
    flexibility within reason: as long as all 5 tools fire and
    the markers / list states show up in the right order, the
    test passes.
    """
    marker = "FULL_WORKFLOW_MARKER_BBBB"
    prompt = (
        "Perform this exact sequence using sys_terminal_* tools. "
        "Do NOT skip steps. Reply only after step 6 completes.\n\n"
        f"  1. sys_terminal_launch terminal='bash' session='investigate'.\n"
        f"  2. sys_terminal_send terminal='bash' session='investigate' "
        f"text='echo {marker}' keys='Enter'.\n"
        "  3. sys_terminal_read terminal='bash' session='investigate'.\n"
        "  4. sys_terminal_list (no args) — capture the result.\n"
        "  5. sys_terminal_close terminal='bash' session='investigate'.\n"
        "  6. sys_terminal_list again (no args) — capture the result.\n\n"
        "Reply with 'done' once step 6 completes. No prose, no extra "
        "wording."
    )
    resp = http_client.post(
        "/v1/responses",
        json={
            "model": sys_terminal_test_agent,
            "input": prompt,
            "stream": False,
        },
        timeout=240.0,
    )
    resp.raise_for_status()
    body = poll_until_terminal(http_client, resp.json()["id"], timeout=240)
    assert body["status"] == "completed", (
        f"Workflow failed: status={body.get('status')!r}, error={body.get('error')!r}."
    )
    conv_id = body["conversation"]["id"]

    # All 5 tool names must appear in the conversation. We pull
    # the raw call lists so a missing tool can be named in the
    # failure message.
    launches = _get_function_call_outputs(http_client, conv_id, "sys_terminal_launch")
    sends = _get_function_call_outputs(http_client, conv_id, "sys_terminal_send")
    reads = _get_function_call_outputs(http_client, conv_id, "sys_terminal_read")
    lists = _get_function_call_outputs(http_client, conv_id, "sys_terminal_list")
    closes = _get_function_call_outputs(http_client, conv_id, "sys_terminal_close")

    missing = [
        name
        for name, calls in [
            ("sys_terminal_launch", launches),
            ("sys_terminal_send", sends),
            ("sys_terminal_read", reads),
            ("sys_terminal_list", lists),
            ("sys_terminal_close", closes),
        ]
        if not calls
    ]
    assert not missing, (
        f"LLM didn't invoke these tools: {missing!r}. The full-workflow "
        f"test requires all 5. If sys_terminal_list or sys_terminal_close "
        f"is missing, those paths have no e2e coverage anywhere else."
    )

    # The marker must appear in at least one read — proves
    # send actually reached tmux and read captured the output.
    combined_reads = " ".join(reads)
    assert marker in combined_reads, (
        f"Marker {marker!r} not seen in sys_terminal_read output: "
        f"{reads!r}. send/read flow broken."
    )

    # At least one list call must have returned a non-empty list
    # (the pre-close one in step 4) and at least one must have
    # returned an empty list (the post-close one in step 6).
    # We don't pin which is which — LLM may make extra exploratory
    # list calls — but both states must exist among the calls.
    saw_running = False
    saw_empty = False
    for raw in lists:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(entries, list):
            # ``sys_terminal_list`` entries expose ``session`` (the
            # LLM-facing key), not ``session_key`` (the registry's
            # internal field). Confirmed via real JSON output.
            if any(isinstance(e, dict) and e.get("session") == "investigate" for e in entries):
                saw_running = True
            if entries == []:
                saw_empty = True
    assert saw_running, (
        f"No sys_terminal_list call ever showed bash:investigate as "
        f"a registered terminal. Lists: {lists!r}. Either the launch "
        f"never registered (impossible — launches above non-empty), "
        f"or list returns the wrong shape."
    )
    assert saw_empty, (
        f"No sys_terminal_list call ever returned an empty list. "
        f"Lists: {lists!r}. Either close didn't remove the entry "
        f"(registry leak), or the LLM didn't call list after close."
    )

    # Close response must have status='closed' (not 'not_found').
    # This catches the case where the LLM closed a different
    # session than it launched.
    close_results = [json.loads(r) for r in closes if r]
    assert any(c.get("status") == "closed" for c in close_results), (
        f"No sys_terminal_close returned status='closed'. Got: "
        f"{close_results!r}. Either the LLM passed the wrong "
        f"session_key, or close didn't find the registered entry "
        f"(would be a registry-tooling bug)."
    )


def test_sys_terminal_send_keys_drives_interactive_e2e(
    live_server: str,
    sys_terminal_test_agent: str,
    http_client: httpx.Client,
) -> None:
    """
    Interactive driving — the load-bearing capability that
    ``sys_terminal_*`` adds over ``sys_os_shell``. Start a Python
    REPL inside the bash terminal, send ``print(2+2)``, assert
    ``4`` appears in the pane.

    A naive ``sys_os_shell`` ("python3 -c 'print(2+2)'") would
    work too, but proves nothing about *interactive* state. The
    test below requires the REPL to stay running across two
    separate ``send`` calls, with the second ``send`` interpreted
    by the live python process from the first.

    What breaks if this fails:
      - ``send_keys`` parsing regresses (Enter etc. mis-routed).
      - The 50ms ``asyncio.sleep`` between text and keys collapses
        and Enter fires before the text lands.
      - The per-instance lock over-serializes such
        that the python REPL never gets to read its own input.
    """
    prompt = (
        "Use sys_terminal_launch to start the 'bash' terminal with "
        "session 'pyrepl'. Then sys_terminal_send 'python3' followed "
        "by Enter. Wait briefly. Then sys_terminal_send "
        "'print(2+2)' followed by Enter. Wait briefly. Then "
        "sys_terminal_read on session 'pyrepl'. Reply 'done' once "
        "the read completes."
    )
    resp = http_client.post(
        "/v1/responses",
        json={
            "model": sys_terminal_test_agent,
            "input": prompt,
            "stream": False,
        },
        timeout=180.0,
    )
    resp.raise_for_status()
    body = poll_until_terminal(http_client, resp.json()["id"], timeout=180)
    assert body["status"] == "completed", (
        f"Workflow failed: {body.get('status')!r}, error={body.get('error')!r}"
    )
    conv_id = body["conversation"]["id"]

    # Two distinct sends required — one for the REPL start, one
    # for the print expression. If only one fired, the LLM
    # short-circuited to a single sys_os_shell or merged the
    # commands; the test no longer proves interactive driving.
    sends = _get_function_call_outputs(http_client, conv_id, "sys_terminal_send")
    assert len(sends) >= 2, (
        f"Expected >=2 sys_terminal_send calls (python3 start + "
        f"print(2+2)), got {len(sends)}. Sends: {sends!r}. The LLM "
        f"may have collapsed both into a single send — test no "
        f"longer exercises interactive driving."
    )

    reads = _get_function_call_outputs(http_client, conv_id, "sys_terminal_read")
    assert len(reads) >= 1, f"sys_terminal_read never called; conv_id={conv_id}"
    combined = " ".join(reads)

    # The result of print(2+2) must show in the pane. If a Python
    # REPL prompt (>>>) shows but no 4, the print was swallowed
    # by the REPL's input buffer and never executed — points at
    # the keys=Enter handling regressing.
    assert "4" in combined, (
        f"Python REPL output '4' missing from pane after "
        f"print(2+2). Combined reads:\n{combined!r}\n"
        f"If the pane shows '>>>' but no 4, the second send's "
        f"Enter didn't reach python's stdin. If the pane shows "
        f"nothing useful at all, python3 may not be on PATH in "
        f"the tmux env."
    )


def test_sys_terminal_ten_parallel_dispatches_complete_e2e(
    live_server: str,
    sys_terminal_test_agent: str,
    http_client: httpx.Client,
) -> None:
    """
    Ten parallel ``sys_terminal_*`` dispatches in a single turn must
    all succeed. Direct repro of the parallel-dispatch race: pre-fix, concurrent
    action_required dispatches raced on the parent agent workflow's
    ``function_id`` counter and produced
    ``DBOSUnexpectedStepError``, which surfaced in the REPL as a
    ``failed`` response.

    Per ``designs/TOOL_DISPATCH_CHILD_WORKFLOWS.md``: each dispatch
    now spawns its own DBOS workflow with an independent
    ``function_id`` namespace, so the race is gone by construction.

    The test:

    1. Asks the LLM to launch ten sandboxed/unsandboxed terminals
       in a single turn. The exact tool count varies because the LLM
       might split the work, but ten launches produces enough
       concurrent action_required events to expose the race.
    2. Asserts response status = ``"completed"``. Any
       ``DBOSUnexpectedStepError`` would surface as ``"failed"``.
    3. Asserts at least N child ``kind="tool"`` task rows under
       the parent — proving each dispatch DID spawn a child
       workflow (the architecture from the design doc, not a
       silent fallback).
    4. Asserts every persisted ``function_call_output`` has a
       non-empty ``output`` field — proving the PATCH back ran
       through the child workflow's ``_patch_to_harness`` step
       and the parent's ``response.completed`` flush stamped the
       result on the conversation history.

    Skipped automatically when ``tmux`` is missing — the
    ``pytestmark`` at module level handles that. Requires the
    ``--llm-api-key`` option (Databricks test-profile PAT for the
    claude-sdk + databricks gateway path).
    """
    prompt = (
        "Launch 10 separate bash terminals using sys_terminal_launch. Use "
        "session keys 't0', 't1', 't2', ..., 't9'. Just call sys_terminal_launch "
        "once per terminal — do not send anything into them and do not read "
        "from them. Reply 'done' once all 10 launches return."
    )
    resp = http_client.post(
        "/v1/responses",
        json={
            "model": sys_terminal_test_agent,
            "input": prompt,
            "stream": False,
        },
        timeout=300.0,
    )
    resp.raise_for_status()
    response_id = resp.json()["id"]
    body = poll_until_terminal(http_client, response_id, timeout=300)

    assert body["status"] == "completed", (
        f"Expected status='completed' but got status={body['status']!r}, "
        f"error={body.get('error')!r}. A 'failed' status here is the "
        f"exact regression that was fixed: concurrent action_required "
        f"dispatches racing on the parent agent workflow's "
        f"function_id counter. Re-check whether each dispatch is "
        f"spawning its own child workflow per "
        f"designs/TOOL_DISPATCH_CHILD_WORKFLOWS.md."
    )

    conv_id = body["conversation"]["id"]

    # Count actual launch tool calls + non-empty outputs. The LLM
    # may launch slightly more than 10 (retries on transient
    # errors) but at least 10 should land. Ten is the threshold
    # that historically reproduces the race; fewer wouldn't prove
    # the parallel-dispatch path was exercised.
    launches = _get_function_call_outputs(http_client, conv_id, "sys_terminal_launch")
    assert len(launches) >= 10, (
        f"Expected at least 10 sys_terminal_launch calls; got "
        f"{len(launches)}. The LLM may have collapsed the request — "
        f"if so the test no longer exercises the parallel-dispatch "
        f"path and needs a stronger prompt. Outputs seen: "
        f"{launches[:3]!r}{'...' if len(launches) > 3 else ''}"
    )
    # Every recorded output must be a real result envelope, not an
    # empty string. The pre-fix bug surfaced as response='failed'
    # with empty function_call_outputs because the workflow died
    # before the harness's response.completed flush ran.
    succeeded = 0
    for idx, out in enumerate(launches):
        if not out:
            # An empty outputs slipped through: not the race we're
            # guarding (the workflow completed) but worth surfacing
            # so the test author can investigate. Don't fail the
            # test on a single bad launch — the race regression
            # would produce N>>1 empties, which the
            # ``succeeded >= 10`` floor below catches.
            continue
        try:
            parsed_out = json.loads(out)
        except json.JSONDecodeError:
            continue
        if parsed_out.get("status") in {"launched", "already_running"}:
            succeeded += 1
    assert succeeded >= 10, (
        f"Expected at least 10 launches to report a successful "
        f"status, got {succeeded} of {len(launches)}. The pre-fix "
        f"race was that several dispatches died with empty outputs; "
        f"if this assertion fails, look at server.log for "
        f"DBOSUnexpectedStepError or other async-dispatch errors."
    )
