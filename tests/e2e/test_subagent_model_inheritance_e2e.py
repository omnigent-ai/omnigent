"""Mock-LLM e2e for user-selected-model propagation to sub-agents.

Reproduces the reported journey: a user launches the polly orchestrator with
an explicitly selected model (``omnigent run <polly> --model
claude-sonnet-4-6``), polly's brain dispatches a sub-agent WITHOUT an explicit
``args.model`` (polly's worker configs pin no model), and the user then finds
the sub-agent running on a *different* model than the one they selected — in
the reporter's Databricks environment the workers silently landed on the
provider's default Opus endpoint while the UI showed Sonnet.

Two scenarios:

1. **Main session honors the selection** (guards the already-working half):
   the brain session's effective model and every provider request from the
   brain carry exactly the CLI-selected model.
2. **Sub-agent inherits the selection**: a child dispatched with no explicit
   ``args.model`` (and whose worker spec pins no model) must run on the
   user-selected model — the dispatch gate reads the parent session's own
   selection and persists it as the child's ``model_override`` instead of
   silently falling back to the worker/provider default.

The claude-sdk harness is swapped for openai-agents against a mock LLM (the
standard mock-polly pattern from ``test_polly_e2e``); the plumbing under test
— CLI ``--model`` → spec → session → ``sys_session_send`` dispatch → child
session model resolution — is the real production path.

Run::

    pytest tests/e2e/test_subagent_model_inheritance_e2e.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from typing import Any

from tests.e2e.test_polly_e2e import (
    _REPO,
    _mock_env,
    _mock_polly_spec_dir,
)
from tests.e2e.test_polly_subagent_model_e2e import (
    _RUN_TIMEOUT_SEC,
    _api,
    _polly_parent_id,
    local_polly_server,  # noqa: F401  (imported fixture)
)

# The model the user explicitly selects for the session (the report's
# ``claude-sonnet-4-6``). A canonical vendor id so the test is independent of
# which gateway endpoint is configured.
_SELECTED_MODEL = "claude-sonnet-4-6"


def _run_env(mock_llm_server_url: str) -> dict[str, str]:
    """
    Build the env for the ``omnigent run`` client process.

    Extends :func:`_mock_env` with a ``PYTHONPATH`` pointing at this working
    tree (plus the in-repo SDKs), so the local runner the CLI spawns — which
    executes ``python -m omnigent.runner._entry`` from outside the repo cwd —
    imports the code under test even when the active venv installed omnigent
    from a different checkout (worktree/CI layouts).

    :param mock_llm_server_url: The mock LLM server base URL.
    :returns: The subprocess env.
    """
    env = _mock_env(mock_llm_server_url)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO),
            str(_REPO / "sdks" / "python-client"),
            str(_REPO / "sdks" / "ui"),
            *(p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p),
        ]
    )
    return env


def _run_polly_with_selected_model(
    base_url: str,
    prompt: str,
    mock_llm_server_url: str,
    *,
    polly_dir: Any,
) -> subprocess.CompletedProcess[str]:
    """
    Run one headless polly turn with the user's ``--model`` selection.

    This is the reported user journey verbatim: ``omnigent run <polly>
    --model claude-sonnet-4-6 -p <task>`` (the CLI spelling of picking the
    model in the UI/YAML).

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
            "--model",
            _SELECTED_MODEL,
            "-p",
            prompt,
        ],
        cwd=str(_REPO),
        env=_run_env(mock_llm_server_url),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )


def _mock_requests(mock_llm_server_url: str) -> list[dict[str, Any]]:
    """
    Fetch every request body the mock LLM captured.

    This is the test's stand-in for the report's "Databricks AI Gateway
    metrics" view: which models actually received provider traffic.

    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: Captured request bodies.
    """
    with urllib.request.urlopen(f"{mock_llm_server_url}/mock/requests", timeout=15) as resp:
        return json.load(resp).get("requests", [])


def _wait_for_children(
    base_url: str, parent_id: str, *, timeout: float = 60.0
) -> list[dict[str, Any]]:
    """
    Poll until at least one child session appears under *parent_id*.

    :param base_url: Local server base URL.
    :param parent_id: Parent conversation id.
    :param timeout: Seconds before giving up.
    :returns: Child session rows.
    :raises TimeoutError: If no child appears within *timeout* seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        kids = _api(base_url, f"/v1/sessions/{parent_id}/child_sessions").get("data", [])
        if kids:
            return kids
        time.sleep(1)
    raise TimeoutError(f"no child session appeared under {parent_id} within {timeout}s")


def test_main_session_runs_on_cli_selected_model(
    local_polly_server: str,  # noqa: F811  (imported fixture)
    mock_llm_server_url: str,
    tmp_path: Any,
) -> None:
    """
    The brain session and its provider traffic honor ``--model``.

    Guards the already-working half of the selected-model journey: the
    top-level session's effective model is exactly the CLI selection, and
    every LLM request the turn produced names it — no silent default.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    polly_dir = _mock_polly_spec_dir(
        tmp_path,
        mock_llm_server_url,
        # The CLI --model wins over the baked spec model, so queue the mock
        # responses under the SELECTED model id — if the harness requested
        # any other model, the queue would not serve and the turn would fail.
        brain_model=_SELECTED_MODEL,
        rewrite_sub_agent_harnesses=True,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "No dispatch needed. Done."}],
        key=_SELECTED_MODEL,
    )

    result = _run_polly_with_selected_model(
        local_polly_server,
        "Reply with a single short sentence and end the turn.",
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    snap = _api(local_polly_server, f"/v1/sessions/{parent}")
    effective = snap.get("model_override") or snap.get("llm_model")
    assert effective == _SELECTED_MODEL, (
        f"main session should run on the CLI-selected model {_SELECTED_MODEL!r}, "
        f"but its effective model is {effective!r} "
        f"(model_override={snap.get('model_override')!r}, llm_model={snap.get('llm_model')!r})"
    )

    models_hit = {str(r.get("model")) for r in _mock_requests(mock_llm_server_url)}
    assert models_hit == {_SELECTED_MODEL}, (
        f"provider should only see the selected model {_SELECTED_MODEL!r}, "
        f"but captured requests named {sorted(models_hit)}"
    )


def test_sub_agent_inherits_cli_selected_model(
    local_polly_server: str,  # noqa: F811  (imported fixture)
    mock_llm_server_url: str,
    tmp_path: Any,
) -> None:
    """
    A sub-agent dispatched with no explicit model inherits the user's selection.

    Without inheritance, polly's workers pin no model, the
    brain dispatches them without ``args.model``, and the child session is
    created with ``model_override=None`` — so it silently runs on the
    provider/worker default (Opus in the reporter's gateway) instead of the
    model the user selected for the session. Guards that sub-agent creation
    propagates the parent session's selected model.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    polly_dir = _mock_polly_spec_dir(
        tmp_path,
        mock_llm_server_url,
        brain_model=_SELECTED_MODEL,
        rewrite_sub_agent_harnesses=True,
    )
    tag = uuid.uuid4().hex[:8]

    # The brain dispatches claude_code WITHOUT args.model — exactly what
    # polly does by default (its worker configs pin no model, and the prompt
    # says omitting `model` runs the worker's default).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-inherit-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "claude_code",
                                "title": "explore-readme",
                                "args": {
                                    "purpose": "explore",
                                    "input": "Report the first heading line of README.md.",
                                },
                            }
                        ),
                    }
                ]
            },
            {"text": "Dispatched claude_code without an explicit model."},
            {"text": "Turn complete."},
        ],
        key=_SELECTED_MODEL,
    )
    # Whatever model the CHILD resolves to draws from the default queue, so
    # the child turn completes regardless of which model it lands on.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "child done"}] * 3,
        key="default",
    )

    result = _run_polly_with_selected_model(
        local_polly_server,
        "Dispatch one read-only explore task to claude_code.",
        mock_llm_server_url,
        polly_dir=polly_dir,
    )
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    parent = _polly_parent_id(local_polly_server)
    kids = _wait_for_children(local_polly_server, parent)
    assert [k.get("tool") for k in kids] == ["claude_code"], (
        f"expected exactly the dispatched claude_code child, got {[k.get('tool') for k in kids]}"
    )

    child_id = kids[0].get("session_id") or kids[0].get("id")
    child = _api(local_polly_server, f"/v1/sessions/{child_id}")
    child_model = child.get("model_override")
    # Without inheritance the gate only sets model_override from an explicit
    # args.model, leaving None here — the worker silently runs on its default.
    assert child_model == _SELECTED_MODEL, (
        f"sub-agent dispatched without args.model must inherit the user's "
        f"selected model {_SELECTED_MODEL!r}, but the child session was "
        f"created with model_override={child_model!r} — it will silently run "
        f"on the worker/provider default instead of the selected model"
    )
