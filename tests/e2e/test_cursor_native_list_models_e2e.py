"""Mock-LLM e2e regression: a dispatchable cursor-native worker must not list as dead.

Bug: ``sys_list_models`` reports a fully dispatchable ``cursor-native``
sub-agent worker as ``source: "none"`` — the dead-worker shape whose note
tells the driving agent the worker "cannot run here" — so orchestrators
skip a worker that would run fine.

The user journey (from the report):

1. Configure an orchestrator with a ``cursor-native`` sub-agent (the shipped
   ``examples/polly`` bundle's ``cursor`` worker).
2. Ask the orchestrator to call ``sys_list_models``.
3. The catalog row for the cursor worker comes back ``source: "none"`` with
   an empty model list, even though the worker is dispatchable.

The trigger is the pre-launch ``cursor-agent models`` listing probe failing
(CLI missing from the probe environment, not logged in for listing, or a
transient error) while ``cursor-agent`` itself launches fine — cursor-native
owns its own stored login, so dispatchability is independent of the listing
probe. Sibling subscription-CLI workers (claude-native, codex-native) report
``source: "static"`` in the same pre-launch state; the cursor path instead
collapses to ``source: "none"``.

This test drives the REAL chain — mock brain tool-call -> runner dispatch ->
``catalog_for_spec`` -> persisted transcript row — by booting a throwaway
local server from this working tree and running the polly orchestrator
headless with a mock LLM brain, exactly like its siblings in
``test_polly_subagent_model_e2e.py``. A stub ``cursor-agent`` (prepended to
the runner's ``PATH``) is launchable but fails its ``models`` subcommand,
pinning the dispatchable-worker / failing-probe state deterministically.

The test FAILS on un-fixed code (cursor row is ``source: "none"``) and must
PASS once the cursor listing failure falls back to a usable row the way the
other subscription CLIs do.

Run::

    pytest tests/e2e/test_cursor_native_list_models_e2e.py -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
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

# Mock runs are fast (no real model inference) so a short timeout is enough.
_RUN_TIMEOUT_SEC = 300


def _api(base_url: str, path: str) -> dict[str, Any]:
    """GET a local-server API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


def _write_cursor_agent_stub(tmp_path: Path) -> Path:
    """Write a launchable ``cursor-agent`` stub whose ``models`` probe fails.

    Mirrors the reported state: the worker is dispatchable (the binary
    resolves and launches), but the pre-launch model-listing probe errors —
    e.g. an account not logged in for listing, or a transient CLI failure.

    :param tmp_path: Per-test temp dir to write the stub into.
    :returns: Absolute path to the executable stub.
    """
    stub = tmp_path / "cursor-agent"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Stub cursor-agent: launchable, but the `models` listing probe fails.\n"
        'if [ "$1" = "models" ]; then\n'
        '  echo "Error: unable to list models" >&2\n'
        "  exit 1\n"
        "fi\n"
        'echo "stub cursor-agent: $*"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


@pytest.fixture
def local_polly_server(tmp_path: Path) -> Iterator[str]:
    """Start a throwaway local ``omnigent server`` from this working tree.

    Mirrors ``test_polly_subagent_model_e2e.local_polly_server`` (own sqlite
    DB + artifact dir under ``tmp_path``); duplicated as a fixture because
    pytest fixtures don't import across modules without a conftest, and this
    file must stay droppable next to its siblings.

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
            f"sqlite:///{tmp_path / 'cursor_list_models_e2e.db'}",
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


def _polly_parent_id(base_url: str) -> str:
    """Find the polly parent session on the throwaway server.

    The server DB is per-test, so the only polly session is ours.

    :param base_url: Local server base URL.
    :returns: The parent conversation id.
    """
    sessions = _api(base_url, "/v1/sessions").get("data", [])
    parents = [s["id"] for s in sessions if s.get("agent_name") == "polly"]
    assert parents, f"no polly session found among {len(sessions)} sessions"
    return parents[0]


@pytest.mark.timeout(600)
def test_cursor_native_worker_not_reported_source_none(
    local_polly_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A dispatchable cursor-native worker must not list as ``source: "none"``.

    The mock brain calls ``sys_list_models``; the runner-dispatched tool
    enumerates every polly worker. The ``cursor`` worker's ``cursor-agent``
    (a stub) is launchable — the worker can run — but its ``models`` listing
    probe fails. Sibling subscription-CLI workers degrade to a usable
    ``source: "static"`` row in this state; the bug is that the cursor row
    instead collapses to ``source: "none"`` (empty models, dead-worker
    note), telling the orchestrator a runnable worker cannot run here.

    Fails on un-fixed code with the cursor row at ``source: "none"``; must
    pass once the cursor listing failure degrades to a usable row.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the spec copy and the CLI stub.
    """
    import uuid

    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    polly_dir = _mock_polly_spec_dir(tmp_path, mock_llm_server_url)
    stub = _write_cursor_agent_stub(tmp_path)
    tag = uuid.uuid4().hex[:8]

    configure_mock_llm(
        mock_llm_server_url,
        [
            # Step 1: the brain asks for the per-worker model catalog.
            {
                "tool_calls": [
                    {
                        "call_id": f"call-lm-{tag}",
                        "name": "sys_list_models",
                        "arguments": "{}",
                    }
                ]
            },
            # Step 2: end the turn after receiving the catalog.
            {"text": "Catalog received."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    env = _mock_env(mock_llm_server_url)
    import os

    # The runner resolves cursor-agent from its own environment, and the
    # CLI->runner env strip only forwards allowlisted vars (PATH is one;
    # OMNIGENT_CURSOR_PATH is not). Prepend the stub's dir to PATH so the
    # runner sees an "installed" cursor-agent whose listing probe fails,
    # regardless of whether the host has a real cursor-agent.
    env["PATH"] = f"{stub.parent}{os.pathsep}{env.get('PATH', '')}"
    # The spawned runner subprocess doesn't inherit sys.path[0] from the CLI's
    # cwd; make this working tree importable there (mirrors the e2e_ui
    # conftest's runner env) so worktree runs work without an editable install.
    env["PYTHONPATH"] = f"{_REPO}{os.pathsep}{env.get('PYTHONPATH', '')}"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(polly_dir),
            "--server",
            local_polly_server,
            "-p",
            "Call sys_list_models and report the catalog.",
        ],
        cwd=str(_REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    items = _api(local_polly_server, f"/v1/sessions/{parent}/items").get("data", [])

    # The sys_list_models call ran and its result persisted in the transcript.
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
    catalog = catalogs[-1]

    cursor_row = catalog.get("cursor")
    assert isinstance(cursor_row, dict), (
        f"sys_list_models result has no 'cursor' row: {sorted(catalog)}"
    )

    # THE BUG: a dispatchable cursor-native worker whose listing
    # probe failed is reported with the dead-worker source "none". Any usable
    # provenance ("cli" from a live listing, "static" from the subscription
    # fallback the other CLI harnesses use) passes; "none" is the regression.
    assert cursor_row.get("source") != "none", (
        "Bug reproduced: sys_list_models reports the dispatchable "
        f"cursor-native worker as source='none' — full row: {cursor_row}"
    )
