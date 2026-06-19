"""Opt-in e2e for per-dispatch sub-agent model control on polly.

Real models, real fan-out: boots a throwaway LOCAL server from this working
tree, has the polly orchestrator dispatch all three workers in ONE turn with
a DIFFERENT explicit ``args.model`` each (including a cross-family GPT model
on the multi-provider pi worker), and asserts the server persisted exactly
the requested override on every child row. A second test proves the family
guard end-to-end: a deliberate GPT-model dispatch to ``claude_code`` must
fail loud at the tool boundary and create no child. A third test proves
model awareness: the brain calls ``sys_list_models`` and dispatches pi on a
Claude-family id chosen FROM the returned gateway listing.

Why e2e and not unit: the unit/dispatch tests stub the server; this is the
only layer that proves the full chain LLM tool-call -> ``sys_session_send``
args -> runner validation -> ``POST /v1/sessions`` ``model_override`` ->
persisted child row, with a real brain emitting the tool calls.

OPT-IN like ``test_polly_e2e.py`` (same dev-box toolset: ``oss`` OAuth,
``claude``/``codex``/``pi`` binaries):

    OMNIGENT_E2E_POLLY=1 uv run --extra dev python -m pytest \
        tests/e2e/test_polly_subagent_model_e2e.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.test_polly_e2e import (
    _SERVER_BOOT_TIMEOUT_SEC,
    _clean_env,
    _free_port,
    _wait_for_health,
)

# tests/e2e/test_polly_subagent_model_e2e.py -> repo root is 2 parents up.
_REPO = Path(__file__).resolve().parents[2]
_POLLY = _REPO / "examples" / "polly"
# Three workers including two native boots: give the dispatch turn headroom.
_RUN_TIMEOUT_SEC = 600

# The contract under test: one explicit, distinct model per worker. The pi
# entry is deliberately a GPT id — the multi-provider worker must accept a
# cross-family model that the single-vendor workers would reject.
_EXPECTED_MODELS = {
    "claude_code": "claude-sonnet-4-6",
    "codex": "databricks-gpt-5-4-mini",
    "pi": "databricks-gpt-5-4",
}

# Verbatim-JSON args blocks: a looser phrasing let the brain "helpfully"
# substitute its own idea of a vendor's model id in a live run.
_DISPATCH_PROMPT = (
    "Dispatch exactly THREE read-only explore tasks via sys_session_send, all "
    "in THIS turn, one per worker. Copy each args object below VERBATIM - do "
    "not substitute a different model id even if you believe another is more "
    "correct:\n"
    '1. agent=claude_code title=explore-readme args={"purpose": "explore", '
    '"model": "claude-sonnet-4-6", "input": "Report the first heading line '
    'of README.md at the repo root. Read-only."}\n'
    '2. agent=codex title=explore-pyproject args={"purpose": "explore", '
    '"model": "databricks-gpt-5-4-mini", "input": "Report the project name '
    'from pyproject.toml at the repo root. Read-only."}\n'
    '3. agent=pi title=explore-license args={"purpose": "explore", '
    '"model": "databricks-gpt-5-4", "input": "Report the license name from '
    'the LICENSE file at the repo root. Read-only."}\n'
    "After dispatching all three, end your turn and wait for inbox notices."
)

# Model-awareness flow: the brain must consult sys_list_models and pick a
# dispatchable id FROM the returned pi list, not from its own priors.
_LIST_THEN_DISPATCH_PROMPT = (
    "Step 1: call sys_list_models with no arguments. Step 2: from the 'pi' "
    "entry of the result, pick the FIRST model id whose family is 'claude'. "
    "Step 3: dispatch exactly ONE read-only explore task via sys_session_send "
    'with agent=pi title=explore-models and args={"purpose": "explore", '
    '"model": "<the id you picked>", "input": "Report the first heading line '
    'of README.md at the repo root. Read-only."}. Use the picked id VERBATIM '
    "as the model value - do not invent, shorten, or substitute another id. "
    "Dispatch to NO other worker. After dispatching, end your turn and wait "
    "for inbox notices."
)

_VIOLATION_PROMPT = (
    "Dispatch ONE explore task via sys_session_send: agent claude_code, title "
    "explore-violation, args={purpose: explore, model: 'databricks-gpt-5-4-mini', "
    "input: 'Report the first line of README.md. Read-only.'}. Pass that model "
    "value EXACTLY as given even though it is a GPT model. When the tool call "
    "returns an error, do NOT retry or re-dispatch with any other model or "
    "worker: quote the tool's error message verbatim in your reply and end "
    "your turn."
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_POLLY") != "1",
    reason=(
        "polly e2e needs the dev-box toolset (oss OAuth login) absent on CI - "
        "set OMNIGENT_E2E_POLLY=1 to opt in."
    ),
)


def _api(base_url: str, path: str) -> dict[str, Any]:
    """
    GET a local-server AP API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


def _terminal_sockets() -> set[str]:
    """
    Snapshot the omnigent-terminal tmux socket dirs currently present.

    :returns: Absolute socket-dir paths under ``/tmp``.
    """
    import glob

    return set(glob.glob("/tmp/omnigent-terminal-*"))


@pytest.fixture
def reap_spawned_terminals() -> Iterator[None]:
    """
    Kill tmux servers (and their child CLIs) this test spawned.

    The headless ``-p`` run exits after the turn, but native sub-agent
    terminals live in detached tmux servers that outlive the runner. Only
    sockets that appeared during the test are killed, so a developer's own
    sessions are untouched.

    :yields: None.
    """
    before = _terminal_sockets()
    try:
        yield
    finally:
        for sock_dir in _terminal_sockets() - before:
            subprocess.run(
                ["tmux", "-S", f"{sock_dir}/tmux.sock", "kill-server"],
                capture_output=True,
                timeout=10,
                check=False,
            )


@pytest.fixture
def local_polly_server(tmp_path: Path) -> Iterator[str]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    Mirrors ``test_polly_e2e.local_polly_server`` (own sqlite DB + artifact
    dir under ``tmp_path``); duplicated as a fixture because pytest fixtures
    don't import across modules without a conftest, and this file must stay
    droppable next to its sibling.

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: The base URL of the running server.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'polly_model_e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        cwd=str(_REPO),
        env=_clean_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, time.monotonic() + _SERVER_BOOT_TIMEOUT_SEC)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_polly_turn(base_url: str, prompt: str) -> subprocess.CompletedProcess[str]:
    """
    Run one headless polly turn against the local server.

    :param base_url: Local server base URL.
    :param prompt: The ``-p`` one-shot prompt.
    :returns: The completed ``omnigent run`` process.
    """
    # Earlier work removed `omnigent run --profile`: provider auth comes from
    # `omnigent setup` / `omnigent login` on the dev box running this suite.
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(_POLLY),
            "--server",
            base_url,
            "-p",
            prompt,
        ],
        cwd=str(_REPO),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _polly_parent_id(base_url: str) -> str:
    """
    Find the polly parent session on the throwaway server.

    The server DB is per-test, so the only polly session is ours.

    :param base_url: Local server base URL.
    :returns: The parent conversation id.
    """
    sessions = _api(base_url, "/v1/sessions").get("data", [])
    parents = [s["id"] for s in sessions if s.get("agent_name") == "polly"]
    assert parents, f"no polly session found among {len(sessions)} sessions"
    return parents[0]


def test_polly_dispatches_distinct_models_per_worker(
    local_polly_server: str, reap_spawned_terminals: None, using_mock_llm: bool
) -> None:
    """
    One turn, three workers, three different explicit models — each child row
    persists exactly the requested ``model_override``.

    This is the end-to-end proof for per-dispatch model control: the brain's
    tool calls carry ``args.model``, the runner validates family rules (the
    GPT id on pi exercises the multi-provider allowance), the server persists
    the override on the child row, and the native/scaffold launch paths read
    it from there (covered by unit tests + the runner's launch-config log).

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param reap_spawned_terminals: Teardown fixture for native terminals.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly sub-agent model e2e requires real model inference and real "
            "subprocess omnigent run invocations; not feasible under mock LLM"
        )
    result = _run_polly_turn(local_polly_server, _DISPATCH_PROMPT)
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    # Exactly the three instructed workers — a missing vendor means the
    # orchestrator didn't fan out as instructed, an extra one means a retry
    # the prompt forbade.
    tools = sorted(k.get("tool") or "" for k in kids)
    assert tools == ["claude_code", "codex", "pi"], (
        f"expected one child per worker, got {tools}; run stdout tail: {result.stdout[-400:]!r}"
    )

    # The core assertion: each child row persisted EXACTLY the model the
    # orchestrator was told to pass — content, not just presence.
    seen: dict[str, str | None] = {}
    for k in kids:
        child_id = k.get("session_id") or k.get("id")
        snap = _api(local_polly_server, f"/v1/sessions/{child_id}")
        seen[str(k.get("tool"))] = snap.get("model_override")
    assert seen == _EXPECTED_MODELS, (
        f"per-child model_override mismatch:\n  expected {_EXPECTED_MODELS}\n  got      {seen}"
    )


def test_polly_rejects_cross_family_model_dispatch(
    local_polly_server: str, reap_spawned_terminals: None, using_mock_llm: bool
) -> None:
    """
    A GPT model on ``claude_code`` fails loud at dispatch and creates NO child.

    Proves the family guard end-to-end with a real brain: the tool returns the
    rejection (naming the rule) instead of creating a child that would die
    opaquely at the gateway, and the orchestrator surfaces the message.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param reap_spawned_terminals: Teardown fixture for native terminals.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly sub-agent model e2e requires real model inference and real "
            "subprocess omnigent run invocations; not feasible under mock LLM"
        )
    result = _run_polly_turn(local_polly_server, _VIOLATION_PROMPT)
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    items = _api(local_polly_server, f"/v1/sessions/{parent}/items").get("data", [])
    transcript = json.dumps(items)
    # The fail-loud rule text must surface in the turn (tool output and/or the
    # quoted reply) — this is the exact string the dispatch gate emits.
    assert "only runs Claude models" in transcript, (
        "family-guard rejection text not found in the parent transcript; "
        f"last items: {transcript[-600:]!r}"
    )

    # The rejection happens BEFORE child creation, and the prompt forbade any
    # retry or re-dispatch — so the dispatch must create NO child at all, not
    # merely avoid a wrongly-moded one.
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    assert kids == [], (
        f"dispatch was rejected but a child was still created: {[k.get('tool') for k in kids]}"
    )


def test_polly_lists_models_then_dispatches_pi_from_list(
    local_polly_server: str, reap_spawned_terminals: None, using_mock_llm: bool
) -> None:
    """
    The brain enumerates models via ``sys_list_models`` and dispatches pi
    on a Claude-family id chosen FROM the returned gateway list.

    End-to-end proof for model awareness: the runner-dispatched tool
    resolves pi's real provider (the Databricks gateway on this dev-box
    setup), returns a non-empty verified listing in the transcript, and
    the id the brain picks from it round-trips through the dispatch gate
    into the child row's ``model_override`` — closing the loop from
    "which models exist here?" to "child actually pinned to one of them".

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param reap_spawned_terminals: Teardown fixture for native terminals.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly sub-agent model e2e requires real model inference and real "
            "subprocess omnigent run invocations; not feasible under mock LLM"
        )
    result = _run_polly_turn(local_polly_server, _LIST_THEN_DISPATCH_PROMPT)
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    items = _api(local_polly_server, f"/v1/sessions/{parent}/items").get("data", [])

    # (a) The sys_list_models tool result is in the transcript and carries
    # a non-empty, gateway-sourced pi listing (claude family present).
    call_ids = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call" and item.get("name") == "sys_list_models"
    }
    assert call_ids, "no sys_list_models function_call found in the parent transcript"
    catalogs = [
        json.loads(item.get("output") or "{}")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") in call_ids
    ]
    assert catalogs, "no sys_list_models tool result found in the parent transcript"
    pi_row = catalogs[-1].get("pi")
    assert pi_row, f"sys_list_models result has no 'pi' row: {catalogs[-1]}"
    assert pi_row["source"] == "gateway", f"pi row not gateway-sourced: {pi_row}"
    assert pi_row["verified"] is True
    pi_ids = [m["id"] for m in pi_row["models"]]
    claude_ids = [m["id"] for m in pi_row["models"] if m["family"] == "claude"]
    assert claude_ids, f"pi listing has no claude-family ids to dispatch from: {pi_ids}"

    # (b) Exactly one pi child, pinned to one of the LISTED ids (claude
    # family) — the brain picked from the catalog, not from its priors.
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    pi_kids = [k for k in kids if k.get("tool") == "pi"]
    assert len(pi_kids) == 1, f"expected exactly one pi child, got {kids}"
    child_id = pi_kids[0].get("session_id") or pi_kids[0].get("id")
    override = _api(local_polly_server, f"/v1/sessions/{child_id}").get("model_override")
    assert override in claude_ids, (
        f"child model_override {override!r} is not one of the listed claude-family "
        f"pi ids {claude_ids}"
    )


_CANONICAL_DISPATCH_PROMPT = (
    "Dispatch exactly ONE read-only explore task via sys_session_send. Copy "
    "the args object VERBATIM - do not substitute or localize the model id "
    "yourself:\n"
    'agent=pi title=explore-canonical args={"purpose": "explore", '
    '"model": "claude-opus-4-8", "input": "Report the first heading line of '
    'README.md at the repo root. Read-only."}\n'
    "After dispatching, end your turn and wait for inbox notices."
)


def test_polly_canonical_id_localized_for_gateway_child(
    local_polly_server: str, reap_spawned_terminals: None, using_mock_llm: bool
) -> None:
    """
    A canonical vendor id (``claude-opus-4-8``) sent to a gateway-routed
    child is localized at the dispatch gate to the gateway's endpoint name
    (``databricks-claude-opus-4-8``) before persisting.

    Live proof of deployment-portable model choices: the transcript shows
    the brain passed the CANONICAL id (so the transform provably happened
    in the gate, not the prompt), while the child row carries the LOCAL id
    the launch paths consume.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param reap_spawned_terminals: Teardown fixture for native terminals.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly sub-agent model e2e requires real model inference and real "
            "subprocess omnigent run invocations; not feasible under mock LLM"
        )
    result = _run_polly_turn(local_polly_server, _CANONICAL_DISPATCH_PROMPT)
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    # The brain sent the canonical id — ground truth from the tool call.
    items = _api(local_polly_server, f"/v1/sessions/{parent}/items").get("data", [])
    sent_models = []
    for item in items:
        if item.get("type") == "function_call" and "session_send" in str(item.get("name", "")):
            raw = item.get("arguments")
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            sent_models.append((parsed.get("args") or {}).get("model"))
    assert "claude-opus-4-8" in sent_models, (
        f"brain did not pass the canonical id verbatim; sent models: {sent_models}"
    )

    # The persisted child row carries the LOCALIZED gateway id. The prompt
    # dispatched exactly ONE task, so the child set must be exactly one pi
    # child — an extra child means a retry the prompt forbade.
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    tools = sorted(k.get("tool") or "" for k in kids)
    assert tools == ["pi"], f"expected exactly one pi child, got {tools}"
    child_id = kids[0].get("session_id") or kids[0].get("id")
    override = _api(local_polly_server, f"/v1/sessions/{child_id}").get("model_override")
    assert override == "databricks-claude-opus-4-8", (
        f"expected the canonical id localized to the gateway endpoint name, "
        f"got model_override={override!r}"
    )
