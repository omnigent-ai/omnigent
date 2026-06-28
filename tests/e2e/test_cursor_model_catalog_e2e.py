"""Mock-LLM e2e: ``sys_list_models`` reports a cursor-native worker.

Boots a throwaway LOCAL server from this working tree and drives a minimal
orchestrator (one cursor-native sub-agent) headless using a mock LLM. The mock
brain emits a single scripted ``sys_list_models`` tool call, so the runner
exercises the real catalog-enumeration path the tool dispatches without
requiring a ``CURSOR_API_KEY`` or the ``cursor-agent`` binary — cursor-native's
listing is a curated static set, not an authenticated fetch.

Regression guard: cursor-native was absent from the model-catalog provider
resolution map, so this row came back ``source: "none"`` even though the worker
runs fine when dispatched. After the fix it must resolve to ``source: "static"``
with the curated Cursor model ids (incl. the pinned ``gpt-5.5``).

The repo's ``examples/polly`` has no cursor sub-agent, so this test materializes
its own minimal orchestrator bundle rather than reuse ``_mock_polly_spec_dir``.

Run::

    pytest tests/e2e/test_cursor_model_catalog_e2e.py -v
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
import yaml

from tests.e2e.test_polly_e2e import (
    _MOCK_BRAIN_MODEL,
    _REPO,
    _SERVER_BOOT_TIMEOUT_SEC,
    _free_port,
    _mock_env,
    _wait_for_health,
)

# Mock runs are fast (no real model inference) so a short timeout is enough.
_RUN_TIMEOUT_SEC = 300
_ORCH_NAME = "cursor_orch"


def _api(base_url: str, path: str) -> dict[str, Any]:
    """
    GET a local-server API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


def _mock_cursor_orchestrator_dir(tmp_path: Path, mock_llm_server_url: str) -> Path:
    """
    Materialize a minimal orchestrator bundle with one cursor-native sub-agent.

    The brain runs the ``openai-agents`` harness wired to the mock LLM server
    (api_key + connection blocks, mirroring ``_mock_polly_spec_dir``); the sole
    sub-agent (``cursor``) keeps the ``cursor-native`` harness with a pinned
    model so ``sys_list_models`` enumerates it. The cursor worker is never
    dispatched — only listed — so no Cursor credential or binary is needed.

    :param tmp_path: Per-test temp dir to write the bundle into.
    :param mock_llm_server_url: The mock LLM server base URL.
    :returns: Path to the orchestrator bundle directory.
    """
    base = f"{mock_llm_server_url}/v1"
    dst = tmp_path / _ORCH_NAME
    (dst / "agents" / "cursor").mkdir(parents=True)

    orchestrator = {
        "spec_version": 1,
        "name": _ORCH_NAME,
        "description": "Test orchestrator with a cursor-native sub-agent.",
        "async": True,
        "executor": {
            "type": "omnigent",
            "model": _MOCK_BRAIN_MODEL,
            "config": {"harness": "openai-agents"},
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": base},
            "connection": {"base_url": base, "api_key": "mock-key"},
        },
        "prompt": "You are a test orchestrator. Call sys_list_models, then stop.",
        "tools": {"agents": ["cursor"]},
    }
    (dst / "config.yaml").write_text(yaml.safe_dump(orchestrator, sort_keys=False))

    cursor_worker = {
        "spec_version": 1,
        "name": "cursor",
        "description": "Cursor coding worker (cursor-native harness, pinned model).",
        "executor": {
            "type": "omnigent",
            "model": "gpt-5.5",
            "config": {"harness": "cursor-native"},
        },
        "prompt": "You are a Cursor coding sub-agent.",
    }
    (dst / "agents" / "cursor" / "config.yaml").write_text(
        yaml.safe_dump(cursor_worker, sort_keys=False)
    )
    return dst


@pytest.fixture
def local_server(tmp_path: Path) -> Iterator[str]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    Own sqlite DB + artifact dir under ``tmp_path`` keep it isolated from the
    developer's real omnigent state.

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
            f"sqlite:///{tmp_path / 'cursor_model_e2e.db'}",
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


def test_sys_list_models_reports_cursor_worker_as_static(
    local_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    The mock brain calls ``sys_list_models``; the cursor worker row is static.

    End-to-end proof that the runner-dispatched ``sys_list_models`` enumerates a
    cursor-native worker as ``source: "static"`` with the curated Cursor models
    (incl. the pinned ``gpt-5.5``) — not the pre-fix ``source: "none"`` — without
    any cursor credential or binary on the box.

    :param local_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the orchestrator bundle.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    orch_dir = _mock_cursor_orchestrator_dir(tmp_path, mock_llm_server_url)
    tag = uuid.uuid4().hex[:8]

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
            # Step 2: after the catalog arrives, end the turn.
            {"text": "Listed models. Cursor worker enumerated."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(orch_dir),
            "--server",
            local_server,
            "-p",
            "Call sys_list_models and report the cursor worker's models.",
        ],
        cwd=str(_REPO),
        env=_mock_env(mock_llm_server_url),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"orchestrator run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    sessions = _api(local_server, "/v1/sessions").get("data", [])
    parents = [s["id"] for s in sessions if s.get("agent_name") == _ORCH_NAME]
    assert parents, f"no {_ORCH_NAME} session found among {len(sessions)} sessions"
    parent = parents[0]

    items = _api(local_server, f"/v1/sessions/{parent}/items").get("data", [])
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

    cursor_row = catalogs[-1].get("cursor")
    assert cursor_row, f"sys_list_models result has no 'cursor' row: {catalogs[-1]}"
    # The regression: this row was "none" before cursor was added to the catalog.
    assert cursor_row["source"] == "static", (
        f"cursor worker row should be static, got: {cursor_row}"
    )
    assert "gpt-5.5" in {m["id"] for m in cursor_row["models"]}, (
        f"cursor models missing the pinned gpt-5.5: {cursor_row['models']}"
    )
