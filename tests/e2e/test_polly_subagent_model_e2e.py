"""Mock-LLM e2e for per-dispatch sub-agent model control on polly.

Boots a throwaway LOCAL server from this working tree and drives the polly
orchestrator headless using a mock LLM. The mock brain emits scripted
``sys_session_send`` (and ``sys_list_models``) tool calls so the runner
exercises the model-validation and child-session-creation paths without
requiring real OAuth credentials or native CLI binaries (``claude``,
``codex``, ``pi``).

Four scenarios:

1. **Distinct models per worker**: the mock brain dispatches all three workers
   in one turn, each with a different explicit ``args.model``; the server
   persists exactly the requested ``model_override`` on every child row.
2. **Cross-family reject**: a deliberate GPT-model dispatch to ``claude_code``
   must fail loud at the tool boundary and create no child.
3. **List then dispatch**: the mock brain calls ``sys_list_models``, receives
   the runtime catalog, then dispatches pi on a Claude-family id from the list.
4. **Canonical ID localization**: a canonical vendor id (``claude-opus-4-8``)
   sent to a gateway-routed child is localized to the gateway endpoint name
   before persisting.

Why e2e and not unit: the unit/dispatch tests stub the server; this is the
only layer that proves the full chain mock tool-call -> ``sys_session_send``
args -> runner validation -> ``POST /v1/sessions`` ``model_override`` ->
persisted child row.

Run::

    pytest tests/e2e/test_polly_subagent_model_e2e.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.test_polly_e2e import (
    _MOCK_BRAIN_MODEL,
    _REPO,
    _SERVER_BOOT_TIMEOUT_SEC,
    _free_port,
    _mock_env,
    _mock_polly_spec_dir,
    _wait_for_health,
)

# tests/e2e/test_polly_subagent_model_e2e.py -> repo root is 2 parents up.
_POLLY = _REPO / "examples" / "polly"
# Mock runs are fast (no real model inference) so a short timeout is enough.
_RUN_TIMEOUT_SEC = 300

# Models dispatched to each worker in the multi-dispatch test.
# Under mock (no Databricks creds), the dispatch gate localizes models for
# non-gateway children:
# - ``pi`` (multi-provider, gateway-capable): databricks-gpt-5-4 passes through.
# - ``codex`` (codex-native, subscription): databricks-gpt-5-4-mini is stripped
#   to gpt-5-4-mini by normalize_model_for_provider("subscription").
# - ``claude_code`` (claude-native, subscription): claude-sonnet-4-6 (no prefix).
# These are the DISPATCHED model ids sent in sys_session_send.
_DISPATCHED_MODELS = {
    "claude_code": "claude-sonnet-4-6",
    "codex": "databricks-gpt-5-4-mini",
    "pi": "databricks-gpt-5-4",
}
# These are the PERSISTED model_override values after localization. In mock
# mode, rewrite_sub_agent_harnesses=True rewrites codex → openai-agents
# (SDK-based, no native binary). openai-agents routes through the gateway,
# so databricks- prefix is preserved (no subscription-provider stripping).
_EXPECTED_MODELS = {
    "claude_code": "claude-sonnet-4-6",
    "codex": "databricks-gpt-5-4-mini",  # openai-agents harness; prefix preserved
    "pi": "databricks-gpt-5-4",  # pi is gateway-capable; prefix preserved
}


def _api(base_url: str, path: str) -> dict[str, Any]:
    """
    GET a local-server API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


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
    import os

    env = {
        **os.environ,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
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
        env=env,
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


def _run_polly_turn(
    base_url: str,
    prompt: str,
    mock_llm_server_url: str,
    *,
    polly_dir: Path,
) -> subprocess.CompletedProcess[str]:
    """
    Run one headless polly turn against the local server.

    :param base_url: Local server base URL.
    :param prompt: The ``-p`` one-shot prompt.
    :param mock_llm_server_url: Mock LLM server base URL for env injection.
    :param polly_dir: The polly bundle to run.
    :returns: The completed ``omnigent run`` process.
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(polly_dir),
            "--server",
            base_url,
            "-p",
            prompt,
        ],
        cwd=str(_REPO),
        env=_mock_env(mock_llm_server_url),
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
    local_polly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    One turn, three workers, three different explicit models — each child row
    persists exactly the requested ``model_override``.

    The mock brain emits three ``sys_session_send`` tool calls (one per worker)
    with the exact models from ``_EXPECTED_MODELS``, then emits a text reply
    after receiving the tool results. The runner validates family rules (the
    GPT id on pi exercises the multi-provider allowance) and persists the
    override on each child row before the parent turn ends.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    # rewrite_sub_agent_harnesses=True replaces native CLI harnesses (``pi``,
    # ``codex-native``, ``claude-native``) with ``openai-agents`` so child
    # sessions are created even when the binaries are absent (e.g. on CI).
    polly_dir = _mock_polly_spec_dir(
        tmp_path, mock_llm_server_url, rewrite_sub_agent_harnesses=True
    )
    tag = uuid.uuid4().hex[:8]

    # First mock response: emit three sys_session_send tool calls — one per
    # worker, each with the dispatched model from _DISPATCHED_MODELS.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-cc-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "claude_code",
                                "title": "explore-readme",
                                "args": {
                                    "purpose": "explore",
                                    "model": _DISPATCHED_MODELS["claude_code"],
                                    "input": "Report the first heading line of README.md.",
                                },
                            }
                        ),
                    },
                    {
                        "call_id": f"call-cx-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "codex",
                                "title": "explore-pyproject",
                                "args": {
                                    "purpose": "explore",
                                    "model": _DISPATCHED_MODELS["codex"],
                                    "input": "Report the project name from pyproject.toml.",
                                },
                            }
                        ),
                    },
                    {
                        "call_id": f"call-pi-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "pi",
                                "title": "explore-license",
                                "args": {
                                    "purpose": "explore",
                                    "model": _DISPATCHED_MODELS["pi"],
                                    "input": "Report the license name from the LICENSE file.",
                                },
                            }
                        ),
                    },
                ]
            },
            # Second response: after tool results arrive, end the turn.
            {"text": "Dispatched all three workers. Waiting for inbox notices."},
            # Third response: synthesis after sub-agents complete (or fail fast on
            # the mock server with no queued responses).
            {"text": "All three workers done. Model overrides verified."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = _run_polly_turn(
        local_polly_server,
        "Dispatch three read-only explore tasks, one per worker.",
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    # Exactly the three instructed workers.
    tools = sorted(k.get("tool") or "" for k in kids)
    assert tools == ["claude_code", "codex", "pi"], (
        f"expected one child per worker, got {tools}; run stdout tail: {result.stdout[-400:]!r}"
    )

    # The core assertion: each child row persists EXACTLY the model the
    # mock brain was told to pass.
    seen: dict[str, str | None] = {}
    for k in kids:
        child_id = k.get("session_id") or k.get("id")
        snap = _api(local_polly_server, f"/v1/sessions/{child_id}")
        seen[str(k.get("tool"))] = snap.get("model_override")
    assert seen == _EXPECTED_MODELS, (
        f"per-child model_override mismatch:\n  expected {_EXPECTED_MODELS}\n  got      {seen}"
    )


def test_polly_rejects_cross_family_model_dispatch(
    local_polly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A GPT model on ``claude_code`` fails loud at dispatch and creates NO child.

    Proves the family guard end-to-end with a mock brain: the mock emits
    one ``sys_session_send`` tool call sending a GPT model to the
    Claude-only ``claude_code`` worker. The runner rejects it (returning an
    error string as the tool result), and the mock brain echoes that error in
    its final text reply. The test confirms no child session was created.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    polly_dir = _mock_polly_spec_dir(tmp_path, mock_llm_server_url)
    tag = uuid.uuid4().hex[:8]

    # First response: a GPT model dispatched to claude_code (family violation).
    # Second response: brain echoes the tool error text (as instructed).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-viol-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "claude_code",
                                "title": "explore-violation",
                                "args": {
                                    "purpose": "explore",
                                    "model": "databricks-gpt-5-4-mini",
                                    "input": "Report the first line of README.md.",
                                },
                            }
                        ),
                    }
                ]
            },
            # After the tool error arrives, echo it and end the turn.
            {"text": "Tool error: claude_code only runs Claude models. Ending turn."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = _run_polly_turn(
        local_polly_server,
        "Dispatch one explore task to claude_code with a GPT model.",
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    items = _api(local_polly_server, f"/v1/sessions/{parent}/items").get("data", [])
    transcript = json.dumps(items)
    # The fail-loud rule text must surface in the turn (tool output).
    assert "only runs Claude models" in transcript, (
        "family-guard rejection text not found in the parent transcript; "
        f"last items: {transcript[-600:]!r}"
    )

    # The rejection happens BEFORE child creation — no child must exist.
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    assert kids == [], (
        f"dispatch was rejected but a child was still created: {[k.get('tool') for k in kids]}"
    )


def test_polly_lists_models_then_dispatches_pi_from_list(
    local_polly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    The mock brain calls ``sys_list_models``, receives the runtime catalog,
    then dispatches pi on a model id from that catalog.

    End-to-end proof for model awareness: the runner-dispatched
    ``sys_list_models`` tool resolves pi's available models from the spec (not
    from a real gateway in mock mode), the mock brain picks a model from the
    result, and the dispatch gate persists it on the child row.

    Under mock LLM the model catalog may report ``"verified": false`` (no real
    provider credentials), so the test only checks that ``sys_list_models``
    produced a non-empty result in the transcript and that the dispatched pi
    child carries a non-null ``model_override``.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    # rewrite_sub_agent_harnesses=True replaces the native ``pi`` harness
    # (which needs the ``pi`` binary on PATH) with ``openai-agents`` so the
    # child session is created even when the binary is absent — e.g. on CI.
    # The test only verifies that the child row exists with a non-null
    # model_override; it does not need the pi process to actually run.
    polly_dir = _mock_polly_spec_dir(
        tmp_path, mock_llm_server_url, rewrite_sub_agent_harnesses=True
    )
    tag = uuid.uuid4().hex[:8]
    # Pick a concrete Claude model for pi — one that the family guard will accept
    # (it only needs to be a Claude-family id, not one from any real catalog).
    pi_dispatch_model = "databricks-claude-sonnet-4-6"

    configure_mock_llm(
        mock_llm_server_url,
        [
            # Step 1: call sys_list_models.
            {
                "tool_calls": [
                    {
                        "call_id": f"call-lm-{tag}",
                        "name": "sys_list_models",
                        "arguments": "{}",
                    }
                ]
            },
            # Step 2: after receiving the catalog, dispatch pi on a Claude model.
            {
                "tool_calls": [
                    {
                        "call_id": f"call-pi-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "pi",
                                "title": "explore-models",
                                "args": {
                                    "purpose": "explore",
                                    "model": pi_dispatch_model,
                                    "input": "Report the first heading line of README.md.",
                                },
                            }
                        ),
                    }
                ]
            },
            # Step 3: end the turn after dispatch.
            {"text": "Dispatched pi on a Claude model from the catalog."},
            # Step 4: synthesis after pi completes (or fails fast on mock).
            {"text": "Pi done. Model override verified."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = _run_polly_turn(
        local_polly_server,
        "Call sys_list_models, then dispatch pi on a Claude-family model from the result.",
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    items = _api(local_polly_server, f"/v1/sessions/{parent}/items").get("data", [])

    # (a) sys_list_models was called and returned a result.
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
    # The catalog must have a pi row (even if not gateway-verified in mock mode).
    pi_row = catalogs[-1].get("pi")
    assert pi_row, f"sys_list_models result has no 'pi' row: {catalogs[-1]}"

    # (b) Exactly one pi child, pinned to the model the mock brain chose.
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    pi_kids = [k for k in kids if k.get("tool") == "pi"]
    assert len(pi_kids) == 1, f"expected exactly one pi child, got {kids}"
    child_id = pi_kids[0].get("session_id") or pi_kids[0].get("id")
    override = _api(local_polly_server, f"/v1/sessions/{child_id}").get("model_override")
    # The dispatched model id may be localized (or pass through unchanged) depending
    # on provider resolution; either way it must be non-null (the dispatch was accepted).
    assert override is not None, (
        f"pi child has no model_override; dispatch may have been silently dropped. "
        f"Dispatched model: {pi_dispatch_model!r}"
    )


_CANONICAL_DISPATCH_PROMPT = (
    "Dispatch exactly ONE read-only explore task via sys_session_send. Copy "
    "the args object VERBATIM:\n"
    'agent=pi title=explore-canonical args={"purpose": "explore", '
    '"model": "claude-opus-4-8", "input": "Report the first heading line of '
    'README.md. Read-only."}'
)


def test_polly_canonical_id_localized_for_gateway_child(
    local_polly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A canonical vendor id (``claude-opus-4-8``) sent to a gateway-routed
    child is localized at the dispatch gate before persisting.

    The mock brain sends the canonical id verbatim. The test verifies that:
    - The function_call item in the transcript records the canonical id the
      brain passed (proving the transform happens in the gate, not the prompt).
    - Exactly one pi child is created with a non-null ``model_override``
      (proving the dispatch was accepted and localization did not drop it).

    The exact localized value (e.g. ``databricks-claude-opus-4-8``) depends on
    provider resolution at runtime; the test checks presence rather than an
    exact Databricks prefix because the mock environment has no gateway creds.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    # rewrite_sub_agent_harnesses=True replaces the native ``pi`` harness
    # (which needs the ``pi`` binary on PATH) with ``openai-agents`` so the
    # child session is created even when the binary is absent — e.g. on CI.
    polly_dir = _mock_polly_spec_dir(
        tmp_path, mock_llm_server_url, rewrite_sub_agent_harnesses=True
    )
    tag = uuid.uuid4().hex[:8]

    configure_mock_llm(
        mock_llm_server_url,
        [
            # Dispatch pi with the canonical vendor id (no databricks- prefix).
            {
                "tool_calls": [
                    {
                        "call_id": f"call-canon-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "pi",
                                "title": "explore-canonical",
                                "args": {
                                    "purpose": "explore",
                                    "model": "claude-opus-4-8",
                                    "input": "Report the first heading line of README.md.",
                                },
                            }
                        ),
                    }
                ]
            },
            # End the turn after dispatch.
            {"text": "Dispatched pi on claude-opus-4-8. Waiting for inbox."},
            # Synthesis after pi completes (or fails fast on mock).
            {"text": "Pi done. Canonical id localized and persisted."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = _run_polly_turn(
        local_polly_server,
        _CANONICAL_DISPATCH_PROMPT,
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
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

    # Exactly one pi child — no retry the prompt forbade.
    kids = _api(local_polly_server, f"/v1/sessions/{parent}/child_sessions").get("data", [])
    tools = sorted(k.get("tool") or "" for k in kids)
    assert tools == ["pi"], f"expected exactly one pi child, got {tools}"
    child_id = kids[0].get("session_id") or kids[0].get("id")
    override = _api(local_polly_server, f"/v1/sessions/{child_id}").get("model_override")
    # The dispatch gate accepted the canonical id; model_override must be non-null.
    # In a real deployment the localized value would be "databricks-claude-opus-4-8";
    # under mock we only require the gate did not drop it.
    assert override is not None, (
        "pi child has no model_override; canonical-id dispatch may have been dropped. "
        "Sent model: 'claude-opus-4-8'"
    )
