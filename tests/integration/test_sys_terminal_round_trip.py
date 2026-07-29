"""Mock-LLM sessions coverage for re-homed sequential ``sys_terminal_*`` e2e.

Re-homes three suppressed e2e tests that lived in
``tests/e2e/test_sys_terminal_e2e.py`` and only stayed red because they
drove the **removed** ``POST /v1/responses`` route (plus
``poll_until_terminal``'s ``GET /v1/responses/{id}``). That route no
longer exists under ``omnigent/server/routes/``; the cited
"500 / runner availability" reason on issue 532 was a stale
misdiagnosis. These replacements drive the current runner-bound
sessions API in mock-LLM mode instead — the same path the merged D6
re-homes (``test_d6_async_cancel_round_trip``,
``test_d6_parallel_fan_out_round_trip``) use.

Why this faithfully exercises the same surface
-----------------------------------------------
``sys_terminal_*`` are AP-side / server-executed tools (the runner's
tool dispatcher runs ``TerminalRegistry`` → real tmux and threads the
result back to the model), NOT client-side ``action_required`` tools.
So a mock LLM scripted to emit ``sys_terminal_launch`` / ``send`` /
``read`` / ``list`` / ``close`` calls actually drives real tmux on the
server — no external client has to fulfill anything. The agent loop
consumes exactly one queued mock response per model call, so a single
user turn with a queue of ``[launch, send, read, ..., final_text]``
executes the steps in strict sequential order (each tool result is
posted back before the next queued response is consumed). That gives
the same launch→send→read→list→close ordering the old real-LLM e2e
relied on, without trusting an LLM to follow a prompt.

Harness choice
--------------
Runs on the ``openai-agents`` harness — the default in mock mode (see
``tests/integration/conftest.py::harness_name``). The mock LLM speaks
the OpenAI ``/v1/responses`` SSE shape that harness consumes. Terminal
tool execution is harness-agnostic (it is a runner-side registry
lookup + tmux spawn), so the on-disk ``sys-terminal-test`` claude-sdk
agent is not usable in mock mode; instead each test registers a minimal
inline ``openai-agents`` agent carrying the same ``terminals:`` block
via ``register_inline_agent(extra_config=...)``. This mirrors
``test_d6_parallel_fan_out_round_trip.py::terminal_mock_agent``.

Coverage deltas vs. the old e2e (honest notes)
----------------------------------------------
* The old tests proved a *real LLM* chose to call the tools from a
  natural-language prompt. The mock layer scripts the calls, so it does
  NOT prove prompt-following / tool-selection — only that the
  server-executed terminal plumbing round-trips correctly. The
  registration→dispatch→tmux→result path, the load-bearing behavior, is
  exercised identically.
* Markers are produced by the shell itself (``echo`` output captured by
  ``sys_terminal_read``), so a passing assertion still proves data flowed
  send→tmux→read, exactly as before.

Runs in mock mode (no ``--llm-api-key``); the ``tests/integration``
package gate is lifted in mock mode by
``tests/integration/conftest.py``. Skipped when tmux is not installed.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.terminals import TerminalRegistry
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.runner.helpers import NullServerClient


def _list_session_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return all persisted items for a session in one paginated snapshot.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    """
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"order": "asc", "limit": 1000}
        if after is not None:
            params["after"] = after
        resp = client.get(f"/v1/sessions/{session_id}/items", params=params)
        resp.raise_for_status()
        page = resp.json()
        items.extend(page["data"])
        if not page.get("has_more"):
            return items
        after = page.get("last_id")
        if after is None:
            raise AssertionError(f"items page had has_more without last_id: {page}")


def _flat_item(item: dict[str, Any]) -> dict[str, Any]:
    """Flatten a session item's ``data`` block into the Responses-style shape.

    ``GET /v1/sessions/{id}/items`` serializes conversation items with
    type-specific fields nested under ``data``; the e2e assertions
    historically read ``name`` / ``call_id`` / ``output`` as top-level
    fields. Flatten so the same accessors work here.

    :param item: A raw conversation item dict.
    """
    data = item.get("data")
    if not isinstance(data, dict):
        return item
    return {
        "id": item.get("id"),
        "response_id": item.get("response_id"),
        "type": item.get("type"),
        "status": item.get("status"),
        **data,
    }


def _function_call_outputs_for(
    client: httpx.Client,
    *,
    session_id: str,
    tool_name: str,
) -> list[str]:
    """Return raw outputs of every *tool_name* call in conversation order.

    Walks the persisted ``function_call`` / ``function_call_output``
    items so assertions land on deterministic tool output strings, not
    on flaky model prose. Mirrors the old e2e's
    ``_get_function_call_outputs``.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    :param tool_name: Only outputs of calls to this tool are returned.
    :returns: Ordered list of raw output strings.
    """
    items = [_flat_item(item) for item in _list_session_items(client, session_id)]
    call_ids = {
        item["call_id"]
        for item in items
        if item.get("type") == "function_call"
        and item.get("name") == tool_name
        and item.get("call_id")
    }
    return [
        str(item.get("output") or "")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") in call_ids
    ]


def _register_terminal_agent(
    http_client: httpx.Client,
    *,
    live_runner_id: str,
    harness_name: str,
    model_name: str,
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
) -> str:
    """Register a minimal inline agent with the ``sys_terminal_*`` tools and bind a session.

    Carries the same ``terminals: {bash: ...}`` block the on-disk
    ``sys-terminal-test`` agent declares, threaded through the compat
    translator via ``register_inline_agent(extra_config=...)`` so the
    five ``sys_terminal_*`` tools register on the AP-side ToolManager.

    :returns: The runner-bound session id.
    """
    agent_name = register_inline_agent(
        http_client,
        name=f"terminal-seq-{uuid.uuid4().hex[:6]}",
        harness=harness_name,
        model=model_name,
        profile=request.config.getoption("--profile"),
        prompt=(
            "You are a terminal test assistant. Follow the scripted mock LLM tool calls exactly."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none"},
            },
            "terminals": {
                "bash": {
                    "command": "bash",
                    "os_env": {
                        "type": "caller_process",
                        "sandbox": {"type": "none"},
                    },
                },
            },
        },
    )
    return create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )


@pytest.fixture
def terminal_session(
    http_client: httpx.Client,
    live_runner_id: str,
    harness_name: str,
    model_name: str,
    request: pytest.FixtureRequest,
    mock_llm_server_url: str | None,
) -> tuple[str, str]:
    """A fresh runner-bound session on an inline agent with terminals enabled.

    :returns: ``(session_id, model_name)`` — the model name keys the
        mock LLM response queue.
    """
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed; sys_terminal_* tests need tmux on PATH")
    session_id = _register_terminal_agent(
        http_client,
        live_runner_id=live_runner_id,
        harness_name=harness_name,
        model_name=model_name,
        request=request,
        mock_llm_server_url=mock_llm_server_url,
    )
    return session_id, model_name


@pytest.mark.asyncio
async def test_runner_terminal_input_route_live_tmux_round_trip(tmp_path: Path) -> None:
    """Create a real shell, POST literal input, and read its output marker."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed; runner terminal input needs tmux on PATH")

    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        runner_workspace=tmp_path,
        per_session_workspace=False,
        server_client=NullServerClient(),
    )
    transport = httpx.ASGITransport(app=app)
    marker = f"RUNNER_INPUT_MARKER_{uuid.uuid4().hex[:8]}"
    output_marker = f"OUT-{marker}"

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            create = await client.post(
                "/v1/sessions/conv_route/resources/terminals",
                json={
                    "terminal": "bash",
                    "session_key": "route",
                    "spec": {"command": "bash", "cwd": str(tmp_path)},
                },
            )
            assert create.status_code == 200, create.text

            sent = await client.post(
                "/v1/sessions/conv_route/resources/terminals/terminal_bash_route/input",
                json={"text": f"printf 'OUT-%s\\n' {marker}"},
            )
            assert sent.status_code == 200, sent.text
            assert sent.json() == {"status": "sent", "outcome": "sent"}

            instance = registry.get("conv_route", "bash", "route")
            assert instance is not None
            screens: list[str] = []
            for _ in range(20):
                read_result = await instance.read()
                screens.append(str(read_result.get("screen", "")))
                if output_marker in screens[-1]:
                    break
                await asyncio.sleep(0.05)

            assert output_marker in "\n".join(screens)
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_runner_terminal_input_waits_past_startup_stdin_consumer(tmp_path: Path) -> None:
    """Startup stdin cannot swallow a command behind a false readiness ACK."""
    if shutil.which("tmux") is None or shutil.which("bash") is None:
        pytest.skip("tmux and bash are required for live readiness coverage")

    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        runner_workspace=tmp_path,
        per_session_workspace=False,
        server_client=NullServerClient(),
    )
    transport = httpx.ASGITransport(app=app)
    marker = f"READY_INPUT_MARKER_{uuid.uuid4().hex[:8]}"
    startup = (
        "import os,select,sys;"
        "ready,_,_=select.select([sys.stdin],[],[],0.25);"
        "sys.stdin.readline() if ready else None;"
        "os.execvp('bash',['bash','--noprofile','--norc','-i'])"
    )

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            create = await client.post(
                "/v1/sessions/conv_ready/resources/terminals",
                json={
                    "terminal": "shell",
                    "session_key": "startup",
                    "spec": {
                        "command": sys.executable,
                        "args": ["-c", startup],
                        "cwd": str(tmp_path),
                        "ready_process": "bash",
                    },
                },
            )
            assert create.status_code == 200, create.text

            sent = await client.post(
                "/v1/sessions/conv_ready/resources/terminals/terminal_shell_startup/input",
                json={
                    "attempt_id": str(uuid.uuid4()),
                    "text": f"printf '%s\\n' {marker}",
                    "wait_for_ready": True,
                },
            )
            if sent.status_code == 409:
                assert sent.json()["error"]["code"] == "terminal_not_ready"
                return

            assert sent.status_code == 200, sent.text
            assert sent.json()["outcome"] == "sent"
            instance = registry.get("conv_ready", "shell", "startup")
            assert instance is not None
            screens: list[str] = []
            for _ in range(40):
                read_result = await instance.read()
                screens.append(str(read_result.get("screen", "")))
                if marker in screens[-1]:
                    break
                await asyncio.sleep(0.05)
            assert marker in "\n".join(screens), (
                "a 200/sent readiness result must prove the final shell, not "
                "the startup stdin consumer, received the command"
            )
    finally:
        await registry.shutdown()


def test_sys_terminal_basic_round_trip(
    live_server: str,
    http_client: httpx.Client,
    terminal_session: tuple[str, str],
    mock_llm_server_url: str | None,
) -> None:
    """Launch → send (echo a marker) → read → the marker comes back.

    Re-homes ``test_sys_terminal_basic_round_trip_e2e``. The mock LLM
    is scripted with one tool call per loop step; the agent executes
    each server-side against real tmux and the turn ends on a text
    response. Asserts:

    * the turn completes (the runner accepted and round-tripped every
      AP-side terminal result),
    * the first launch reports ``status="launched"``,
    * the unique marker echoed by ``echo`` appears in a
      ``sys_terminal_read`` capture — proving send reached tmux and
      read saw the output.

    Two reads are scripted purely for timing resilience: ``echo``
    output lands in the pane asynchronously after Enter, so a single
    capture can occasionally race ahead of it. The marker need only
    appear in one.
    """
    session_id, model_name = terminal_session
    marker = f"TERMINAL_RT_MARKER_{uuid.uuid4().hex[:8]}"

    def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_calls": [
                {
                    "call_id": f"call_{name}_{uuid.uuid4().hex[:6]}",
                    "name": name,
                    "arguments": json.dumps(args),
                }
            ]
        }

    configure_mock_llm(
        mock_llm_server_url,
        [
            _call("sys_terminal_launch", {"terminal": "bash", "session": "s1"}),
            _call(
                "sys_terminal_send",
                {"terminal": "bash", "session": "s1", "text": f"echo {marker}", "keys": "Enter"},
            ),
            _call("sys_terminal_read", {"terminal": "bash", "session": "s1"}),
            _call("sys_terminal_read", {"terminal": "bash", "session": "s1"}),
            {"text": "done"},
        ],
        key=model_name,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Launch bash s1, echo the marker, read it back, then say done.",
    )
    result = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert result["status"] == "completed", (
        f"terminal round-trip turn should complete; got {result['status']!r}, "
        f"error={result.get('error')!r}, output={str(result.get('output'))[:600]!r}"
    )

    launches = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_launch"
    )
    assert len(launches) >= 1, (
        f"sys_terminal_launch produced no output; session_id={session_id}. "
        f"If 0, the terminals block never registered the AP-side tools."
    )
    launch_result = json.loads(launches[0])
    assert launch_result.get("status") == "launched", (
        f"first launch should report status='launched'; got {launch_result!r}"
    )

    reads = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_read"
    )
    assert len(reads) >= 1, f"sys_terminal_read produced no output; session_id={session_id}"
    combined = " ".join(reads)
    assert marker in combined, (
        f"echo marker {marker!r} not seen in any sys_terminal_read output. "
        f"Reads: {reads!r}. If empty the send didn't reach tmux; if a prompt "
        f"shows but not the echo, the command failed in tmux."
    )


def test_sys_terminal_full_workflow(
    live_server: str,
    http_client: httpx.Client,
    terminal_session: tuple[str, str],
    mock_llm_server_url: str | None,
) -> None:
    """All five ``sys_terminal_*`` tools, in one ordered sequence.

    Re-homes ``test_sys_terminal_full_workflow_e2e``:
    launch → send (echo a marker) → read → list → close → list. Asserts
    state at each step:

    * all five tools produced output (so list/close, which the focused
      round-trip test never exercises, have coverage here),
    * the echoed marker is captured by read,
    * a ``list`` call shows ``bash:investigate`` running (pre-close),
      and a later ``list`` no longer shows it (post-close) — the only
      check that ``close`` removed the registry entry, not just killed
      the process,
    * ``close`` returned ``status="closed"`` (not ``not_found``).

    Because the agent loop consumes the scripted calls in strict order,
    the pre-close list provably runs before close and the post-close
    list after — no reliance on an LLM to order the steps.
    """
    session_id, model_name = terminal_session
    marker = f"FULL_WORKFLOW_MARKER_{uuid.uuid4().hex[:8]}"
    term = {"terminal": "bash", "session": "investigate"}

    def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_calls": [
                {
                    "call_id": f"call_{name}_{uuid.uuid4().hex[:6]}",
                    "name": name,
                    "arguments": json.dumps(args),
                }
            ]
        }

    configure_mock_llm(
        mock_llm_server_url,
        [
            _call("sys_terminal_launch", term),
            _call("sys_terminal_send", {**term, "text": f"echo {marker}", "keys": "Enter"}),
            _call("sys_terminal_read", term),
            _call("sys_terminal_read", term),
            _call("sys_terminal_list", {}),
            _call("sys_terminal_close", term),
            _call("sys_terminal_list", {}),
            {"text": "done"},
        ],
        key=model_name,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Run the full terminal workflow, then say done.",
    )
    result = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert result["status"] == "completed", (
        f"full-workflow turn should complete; got {result['status']!r}, "
        f"error={result.get('error')!r}, output={str(result.get('output'))[:600]!r}"
    )

    launches = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_launch"
    )
    sends = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_send"
    )
    reads = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_read"
    )
    lists = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_list"
    )
    closes = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_close"
    )

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
        f"these terminal tools produced no output: {missing!r}. The "
        f"full-workflow test requires all five; list/close have no other "
        f"server-executed coverage at this layer."
    )

    combined_reads = " ".join(reads)
    assert marker in combined_reads, (
        f"marker {marker!r} not seen in sys_terminal_read output: {reads!r}. "
        f"send/read flow broken."
    )

    # One list (pre-close) must show bash:investigate running; one
    # (post-close) must not. The list entries expose ``session`` (the
    # LLM-facing key), not the registry's internal field.
    saw_running = False
    saw_gone = False
    for raw in lists:
        try:
            entries = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entries, list):
            continue
        has_investigate = any(
            isinstance(e, dict) and e.get("session") == "investigate" for e in entries
        )
        if has_investigate:
            saw_running = True
        else:
            saw_gone = True
    assert saw_running, (
        f"no sys_terminal_list call showed bash:investigate as running. "
        f"Lists: {lists!r}. Either launch never registered or list returns "
        f"the wrong shape."
    )
    assert saw_gone, (
        f"no sys_terminal_list call ran with bash:investigate absent. "
        f"Lists: {lists!r}. Either close didn't remove the registry entry "
        f"(leak) or the post-close list never ran."
    )

    close_results = [json.loads(r) for r in closes if r]
    assert any(c.get("status") == "closed" for c in close_results), (
        f"no sys_terminal_close returned status='closed'. Got: {close_results!r}. "
        f"close didn't find the registered entry."
    )


def test_sys_terminal_send_keys_drives_interactive(
    live_server: str,
    http_client: httpx.Client,
    terminal_session: tuple[str, str],
    mock_llm_server_url: str | None,
) -> None:
    """Two distinct sends drive an interactive Python REPL across calls.

    Re-homes ``test_sys_terminal_send_keys_drives_interactive_e2e`` —
    the load-bearing capability ``sys_terminal_*`` adds over a one-shot
    ``sys_os_shell``: a process that stays alive across two separate
    ``send`` calls, with the second send interpreted by the live process
    from the first. The script:

    1. launch bash:pyrepl,
    2. send ``python3`` + Enter (start the REPL),
    3. send ``print(2+2)`` + Enter (the second send the REPL must
       interpret),
    4. read (x3 for timing resilience — see below).

    Asserts ``>= 2`` sends fired (a single merged send would not prove
    interactive driving) and that ``4`` — the REPL's evaluation of the
    *second* send — appears in a read capture. The two sends crossing
    Enter-key handling and the REPL surviving between them is what
    distinguishes this from a stateless shell command.

    Honest coverage delta vs. the old e2e: identical load-bearing
    assertions (``>=2`` sends + ``4`` in the pane). The only difference
    is the calls are scripted rather than chosen by a real LLM, so this
    does not prove a model would *decide* to drive the REPL with two
    sends — only that the server-side send/Enter/REPL plumbing works
    when it does. Three reads (extra model round-trips) give the cold
    ``python3`` interpreter time to boot and evaluate before capture;
    the old e2e leaned on real-LLM "wait briefly" pauses for the same
    reason.
    """
    session_id, model_name = terminal_session
    term = {"terminal": "bash", "session": "pyrepl"}

    def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_calls": [
                {
                    "call_id": f"call_{name}_{uuid.uuid4().hex[:6]}",
                    "name": name,
                    "arguments": json.dumps(args),
                }
            ]
        }

    configure_mock_llm(
        mock_llm_server_url,
        [
            _call("sys_terminal_launch", term),
            _call("sys_terminal_send", {**term, "text": "python3", "keys": "Enter"}),
            _call("sys_terminal_send", {**term, "text": "print(2+2)", "keys": "Enter"}),
            _call("sys_terminal_read", term),
            _call("sys_terminal_read", term),
            _call("sys_terminal_read", term),
            {"text": "done"},
        ],
        key=model_name,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Start python3, print(2+2), read the pane, then say done.",
    )
    result = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert result["status"] == "completed", (
        f"interactive turn should complete; got {result['status']!r}, "
        f"error={result.get('error')!r}, output={str(result.get('output'))[:600]!r}"
    )

    sends = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_send"
    )
    assert len(sends) >= 2, (
        f"expected >= 2 sys_terminal_send calls (python3 start + print(2+2)), "
        f"got {len(sends)}: {sends!r}. Fewer means the interactive driving was "
        f"not exercised."
    )

    reads = _function_call_outputs_for(
        http_client, session_id=session_id, tool_name="sys_terminal_read"
    )
    assert len(reads) >= 1, f"sys_terminal_read produced no output; session_id={session_id}"
    combined = " ".join(reads)
    assert "4" in combined, (
        f"Python REPL output '4' missing after print(2+2). Combined reads:\n"
        f"{combined!r}\nIf the pane shows '>>>' but no 4, the second send's "
        f"Enter didn't reach python's stdin; if nothing useful shows, python3 "
        f"may not be on PATH in the tmux env."
    )


def _write_probe_residue_delayed_shell(
    tmp_path: Path,
    *,
    shell_name: str,
    real_shell: str,
) -> Path:
    """Create a shell-named shim that waits before preserving the real argv."""
    wrapper_dir = tmp_path / "probe-residue-shells"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / shell_name
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "time.sleep(1.35)\n"
        f"real_shell = {real_shell!r}\n"
        "os.execv(real_shell, [real_shell, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _probe_residue_output_command(marker: str) -> str:
    """Return a command whose typed representation does not include *marker*."""
    encoded = "".join(f"\\0{ord(char):03o}" for char in marker)
    return f"builtin printf '%b\\n' '{encoded}'"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shell_name", "shell_args"),
    [
        ("bash", ["--noprofile", "--norc"]),
        ("zsh", ["-f"]),
    ],
)
async def test_runner_terminal_input_cold_spawn_hides_readiness_probe(
    tmp_path: Path,
    shell_name: str,
    shell_args: list[str],
) -> None:
    """A delayed direct shell shows only the one user command and its output."""
    real_shell = shutil.which(shell_name)
    if shutil.which("tmux") is None or real_shell is None:
        pytest.skip(f"tmux and {shell_name} are required for live readiness coverage")

    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        runner_workspace=tmp_path,
        per_session_workspace=False,
        server_client=NullServerClient(),
    )
    transport = httpx.ASGITransport(app=app)
    marker = f"O{uuid.uuid4().hex[:6]}"
    command = _probe_residue_output_command(marker)
    wrapper = _write_probe_residue_delayed_shell(
        tmp_path,
        shell_name=shell_name,
        real_shell=real_shell,
    )
    session_key = f"clean-{shell_name}"

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            create = await client.post(
                "/v1/sessions/conv_clean/resources/terminals",
                json={
                    "terminal": shell_name,
                    "session_key": session_key,
                    "spec": {
                        "command": str(wrapper),
                        "args": shell_args,
                        "cwd": str(tmp_path),
                    },
                },
            )
            assert create.status_code == 200, create.text

            instance = registry.get("conv_clean", shell_name, session_key)
            assert instance is not None
            nonce = instance.shell_ready_nonce
            assert nonce is not None

            started = time.monotonic()
            sent = await client.post(
                (
                    "/v1/sessions/conv_clean/resources/terminals/"
                    f"terminal_{shell_name}_{session_key}/input"
                ),
                json={
                    "attempt_id": str(uuid.uuid4()),
                    "text": command,
                    "keys": "Enter",
                    "wait_for_ready": True,
                },
            )
            elapsed = time.monotonic() - started
            assert sent.status_code == 200, sent.text
            assert sent.json() == {"status": "sent", "outcome": "sent"}
            assert elapsed > 1.0

            screens: list[str] = []
            for _ in range(40):
                result = await instance.read(scrollback=100)
                screen = str(result.get("screen", ""))
                screens.append(screen)
                if any(line.strip() == marker for line in screen.splitlines()):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError(f"shell output {marker!r} did not appear:\n{screens[-1]}")

            nonempty_lines = [line for line in screen.splitlines() if line.strip()]
            assert command in nonempty_lines[0], screen
            assert screen.count(command) == 1, screen
            assert screen.count(marker) == 1, screen
            assert "BASH_SOURCE" not in screen, screen
            assert "ZSH_EVAL_CONTEXT" not in screen, screen
            assert "OMNIGENT_SHELL_READY_" not in screen, screen
            assert nonce not in screen, screen
    finally:
        await registry.shutdown()
