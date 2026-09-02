"""Mock-LLM e2e guarding against the first-turn model-switch respawn.

Reproduces the reported journey: an orchestrator (polly) dispatches a
sub-agent via ``sys_session_send`` with an explicit ``args.model`` — the
atomic create+turn path. The child session is created with the requested
``model_override`` persisted, but its harness is warm-spawned at create time
from a spawn env built WITHOUT the override (``entry.model = None``); the
first turn then builds its spawn env WITH the override, so
``process_manager.get_client`` sees ``requested_model != entry.model`` and
tears down + respawns the harness (``harness_respawn_model_switch``) — a full
context reload before the child can answer, and (intermittently) a synthetic
``[System: interrupted]`` user-abandonment marker that can make the child
abandon its task.

Acceptance criteria from the issue: a model-pinned sub-agent dispatched via
``sys_session_send`` completes its first turn with **zero** model-switch
respawns and **zero** ``[System: interrupted]`` synthetic items on turn 1.

The claude-sdk brain harness is swapped for openai-agents against a mock LLM
(the standard mock-polly pattern from ``test_polly_e2e``); the plumbing under
test — ``sys_session_send`` args.model -> child create (model_override) ->
child warm spawn -> child turn-1 spawn env -> process-manager model
reconciliation — is the real production path. The respawn is observed via the
runner's own log (``OMNIGENT_DATA_DIR`` pins the log root per test), which is
where the process manager reports the model-switch teardown.

Run::

    pytest tests/e2e/test_subagent_model_override_respawn_e2e.py -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from tests.e2e.test_polly_e2e import (
    _MOCK_BRAIN_MODEL,
    _REPO,
    _mock_polly_spec_dir,
)
from tests.e2e.test_polly_subagent_model_e2e import (
    _RUN_TIMEOUT_SEC,
    _api,
    _polly_parent_id,
    local_polly_server,  # noqa: F401  (imported fixture)
)
from tests.e2e.test_subagent_model_inheritance_e2e import (
    _run_env,
    _wait_for_children,
)

# The model the orchestrator pins the sub-agent to (the report's model-pinned
# debater). A GPT-family id dispatched to the codex worker, which the mock
# spec rewrite maps onto the SDK openai-agents harness — the harness class
# whose model is baked into the spawn env (not in _LIVE_MODEL_CONFIG_HARNESSES),
# so a first-turn model mismatch respawns the subprocess.
_PINNED_MODEL = "gpt-5-4-mini"

# The process manager's model-switch teardown line
# (omnigent/runtime/harnesses/process_manager.py, reason
# ``harness_respawn_model_switch``). Matched per child conversation id.
_RESPAWN_LINE = "model changed {prior!r} -> {requested!r}; respawning"

_INTERRUPTED_MARKER = "[System: interrupted]"


def _runner_respawn_lines(data_dir: Path, child_id: str) -> list[str]:
    """
    Collect model-switch respawn log lines for *child_id* from the runner logs.

    The ``omnigent run`` client spawns a local runner whose process logs land
    under ``<OMNIGENT_DATA_DIR>/logs/runner/``. The model-switch teardown is
    reported there by ``HarnessProcessManager.get_client``.

    :param data_dir: The ``OMNIGENT_DATA_DIR`` passed to the run subprocess.
    :param child_id: The child conversation id whose respawns to collect.
    :returns: Matching log lines (empty when no respawn happened).
    """
    log_dir = data_dir / "logs" / "runner"
    if not log_dir.exists():
        return []
    pattern = re.compile(rf"conversation {re.escape(child_id)}: model changed .*; respawning")
    hits: list[str] = []
    for log_file in sorted(log_dir.glob("*.log")):
        for line in log_file.read_text(errors="replace").splitlines():
            if pattern.search(line):
                hits.append(line)
    return hits


def _child_items_after_turn_1(
    base_url: str, child_id: str, *, timeout: float = 120.0
) -> list[dict[str, Any]]:
    """
    Fetch the child's conversation items once its FIRST TURN has completed.

    ``sys_session_send`` returns a ``"launching"`` handle before the child's
    turn finishes, so the parent run can exit while the child is still
    mid-turn. Both failure modes under test (the model-switch respawn and the
    synthetic interrupted marker) happen *during* that turn — asserting before
    it completes would pass vacuously. Completion is observed as the child's
    assistant reply landing in its history (the mock LLM answers every child
    turn), or an error item as the terminal fallback.

    :param base_url: Local server base URL.
    :param child_id: Child conversation id.
    :param timeout: Seconds to wait for the turn to reach a terminal item.
    :returns: The item rows recorded by the completed turn.
    :raises TimeoutError: If the child's first turn never completes.
    """
    deadline = time.monotonic() + timeout
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        items = _api(base_url, f"/v1/sessions/{child_id}/items").get("data", [])
        done = any(
            (i.get("type") == "message" and i.get("role") == "assistant")
            or i.get("type") == "error"
            for i in items
        )
        if done:
            return items
        time.sleep(1)
    raise TimeoutError(
        f"child session {child_id} first turn reached no terminal item "
        f"(assistant reply or error) within {timeout:.0f}s; "
        f"items so far: {[i.get('type') for i in items]}"
    )


def test_model_pinned_subagent_first_turn_does_not_respawn(
    local_polly_server: str,  # noqa: F811  (imported fixture)
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    A ``sys_session_send`` dispatch with an explicit model must not respawn on turn 1.

    The child's harness should be spawned once, already at the pinned model
    (the override is known at create time — it is in the create body). Today
    the warm spawn runs at the default (``entry.model=None``) and turn 1
    triggers a ``harness_respawn_model_switch`` teardown + context reload; on
    the unlucky interleaving the respawn's cancel path also injects a
    synthetic ``[System: interrupted]`` user-abandonment marker into the
    child's turn-1 history, which can make the child abandon its task.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the spec copy and runner data dir.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    reset_mock_llm(mock_llm_server_url)
    # rewrite_sub_agent_harnesses=True maps the codex worker onto the SDK
    # openai-agents harness so the child spawns without a native binary; the
    # respawn under test lives in the shared process manager, not the harness.
    polly_dir = _mock_polly_spec_dir(
        tmp_path, mock_llm_server_url, rewrite_sub_agent_harnesses=True
    )
    # Pin the dispatched worker's credential to the mock, mirroring the brain's
    # own auth block. Without it the child's provider resolution falls through
    # to ambient detection, which picks up a cli-config provider from a
    # developer's ~/.codex/config.toml; that hard-raises for every harness but
    # codex, so the child's turn dies at setup and never reaches the respawn
    # this test is about. Scoped to the dispatched worker: the shared helper
    # must keep resolving ambiently for the siblings that assert on
    # provider-localized model ids.
    _codex_cfg = polly_dir / "agents" / "codex" / "config.yaml"
    _codex_spec = yaml.safe_load(_codex_cfg.read_text())
    _codex_spec["executor"]["auth"] = {
        "type": "api_key",
        "api_key": "mock-key",
        "base_url": f"{mock_llm_server_url}/v1",
    }
    _codex_cfg.write_text(yaml.safe_dump(_codex_spec, sort_keys=False))
    tag = uuid.uuid4().hex[:8]

    # The brain pins the child to a model — the report's journey (a debby
    # debate pins each debater; polly's per-dispatch args.model is the same
    # atomic create+turn path).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-pin-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "codex",
                                "title": "explore-readme",
                                "args": {
                                    "purpose": "explore",
                                    "model": _PINNED_MODEL,
                                    "input": "Report the first heading line of README.md.",
                                },
                            }
                        ),
                    }
                ]
            },
            {"text": "Dispatched the model-pinned worker."},
            {"text": "Turn complete."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )
    # Serve the child's turn whatever model id it lands on.
    configure_mock_llm(mock_llm_server_url, [{"text": "child done"}] * 4, key=_PINNED_MODEL)
    configure_mock_llm(mock_llm_server_url, [{"text": "child done"}] * 4, key="default")

    # Pin the spawned runner's data dir (logs/runner/*.log) per test so the
    # respawn observation reads exactly this run's logs.
    data_dir = tmp_path / "runner-data"
    env = _run_env(mock_llm_server_url)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)

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
            "Dispatch one read-only explore task to codex pinned to a model.",
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
    kids = _wait_for_children(local_polly_server, parent)
    assert [k.get("tool") for k in kids] == ["codex"], (
        f"expected exactly the dispatched codex child, got {[k.get('tool') for k in kids]}"
    )
    child_id = kids[0].get("session_id") or kids[0].get("id")
    assert isinstance(child_id, str) and child_id

    # Sanity: the dispatch really took the model-pinned path (otherwise the
    # respawn assertions below would be vacuous).
    child_row = _api(local_polly_server, f"/v1/sessions/{child_id}")
    assert child_row.get("model_override") == _PINNED_MODEL, (
        f"child session should persist model_override={_PINNED_MODEL!r}, "
        f"got {child_row.get('model_override')!r} — the dispatch did not take "
        f"the model-pinned path this test guards"
    )

    # Wait for the child's first turn to actually COMPLETE before asserting:
    # the respawn and the interrupted marker both happen mid-turn, so checking
    # earlier would pass without exercising either acceptance criterion.
    items = _child_items_after_turn_1(local_polly_server, child_id)

    # Acceptance criterion 1: zero harness model-switch respawns on turn 1.
    # The override is in the child's create body, so the first spawn should
    # already be at the pinned model; a "model changed None -> ...; respawning"
    # line is the wasteful teardown + context reload this issue is about.
    respawn_lines = _runner_respawn_lines(data_dir, child_id)
    assert respawn_lines == [], (
        f"model-pinned sub-agent {child_id} hit a first-turn harness "
        f"model-switch respawn (teardown + context reload) — the override "
        f"should be seeded into the FIRST spawn env instead of reconciled by "
        f"respawn on turn 1:\n" + "\n".join(respawn_lines)
    )

    # Acceptance criterion 2: zero synthetic [System: interrupted] items on
    # turn 1 — the respawn's cancel path must not tell the child the USER
    # abandoned the request (that is what intermittently makes it drop its
    # task).
    assert items, f"child session {child_id} recorded no conversation items"
    interrupted = [i for i in items if _INTERRUPTED_MARKER in json.dumps(i)]
    assert interrupted == [], (
        f"child session {child_id} turn 1 contains {len(interrupted)} synthetic "
        f"{_INTERRUPTED_MARKER!r} item(s) — an internal model-switch respawn "
        f"must not inject the user-abandonment marker:\n"
        + "\n".join(json.dumps(i)[:300] for i in interrupted)
    )
