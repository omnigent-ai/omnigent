"""E2E: built-in write policies must cover Claude Code's NotebookEdit.

Two built-in policies promise complete coverage of file-mutating tools and are
user-attachable from the Policies UI:

- ``read_only_os`` ("Report-Only (Deny File Writes)") — "Denies every
  file-mutating tool … so a report-only agent can read and run shell but
  never change code".
- ``worktree_guard`` ("Restrict Writes to Git Worktree") — blocks file writes
  outside the worker's worktree subtree.

Claude Code mutates ``.ipynb`` files with its native ``NotebookEdit`` tool,
which addresses the target file via a ``notebook_path`` argument (not
``file_path``). On the buggy build neither policy gates it: ``NotebookEdit``
is missing from both tool-name sets, and ``worktree_guard``'s path extraction
never reads ``notebook_path``, so a report-only or worktree-confined Claude
session silently rewrites notebooks anywhere on disk.

Journey (per policy): attach the built-in policy to a live session → Claude
Code's ``PreToolUse`` hook posts the tool call to
``POST /v1/sessions/{id}/policies/evaluate`` (the exact wire path the
claude-native harness takes for every native tool call; driven here with the
same request shape ``omnigent.claude_native_hook._main_evaluate_policy``
sends) → the verdict must be ``POLICY_ACTION_DENY``. Control legs prove the
same journey with ``Write`` (and ``MultiEdit`` for read_only_os) DENYs today,
so a failure is specifically the ``NotebookEdit`` coverage gap.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

_READ_ONLY_HANDLER = "omnigent.policies.builtins.orchestration.read_only_os"
_WORKTREE_GUARD_HANDLER = "omnigent.policies.builtins.orchestration.worktree_guard"


def _create_bare_session(http_client: httpx.Client) -> str:
    """Create a session with a minimal inline agent bundle.

    Policy evaluation needs only a session row (no runner, no LLM turn) —
    the evaluate endpoint is the sole enforcement point the claude-native
    ``PreToolUse`` hook calls, so the journey is fully exercised without
    booting the CLI.
    """
    import io
    import tarfile
    import uuid

    import yaml

    name = f"write-policy-coverage-{uuid.uuid4().hex[:8]}"
    config: dict[str, Any] = {
        "name": name,
        "prompt": "You are a test agent.",
        "executor": {"harness": "openai-agents", "model": "gpt-4o-mini"},
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        bundle = buf.getvalue()
    resp = http_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    assert resp.status_code in (200, 201), f"session create failed: {resp.text[:500]}"
    return str(resp.json()["session_id"])


def _attach_policy(
    http_client: httpx.Client,
    session_id: str,
    *,
    name: str,
    handler: str,
    factory_params: dict[str, Any] | None = None,
) -> None:
    """Attach a registered built-in policy to *session_id*."""
    body: dict[str, Any] = {"name": name, "type": "python", "handler": handler}
    if factory_params is not None:
        body["factory_params"] = factory_params
    resp = http_client.post(f"/v1/sessions/{session_id}/policies", json=body)
    assert resp.status_code == 200, f"policy attach failed: {resp.status_code} {resp.text[:500]}"


def _evaluate_tool_call(
    http_client: httpx.Client,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """POST a ``PHASE_TOOL_CALL`` evaluate request; return the verdict string.

    Same body shape the claude-native ``evaluate-policy`` hook sends for a
    ``PreToolUse`` event (see ``omnigent.claude_native_hook``).
    """
    resp = http_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {"name": tool_name, "arguments": arguments},
                "context": {"harness": "claude-native"},
            },
        },
    )
    assert resp.status_code == 200, f"evaluate failed: {resp.status_code} {resp.text[:500]}"
    result = resp.json().get("result", "")
    assert isinstance(result, str)
    return result


@pytest.mark.timeout(120)
def test_report_only_policy_denies_notebook_edit(
    live_server: str,
    http_client: httpx.Client,
) -> None:
    """ "Report-Only (Deny File Writes)" must DENY a NotebookEdit call.

    Control legs first: Write and MultiEdit are denied today, proving the
    policy is attached and firing on this session. Then the same journey
    with NotebookEdit — a file-mutating tool on a report-only agent — must
    also DENY. On the buggy build it returns ALLOW and the ``.ipynb`` is
    silently rewritten.
    """
    session_id = _create_bare_session(http_client)
    _attach_policy(
        http_client,
        session_id,
        name="report-only-deny-writes",
        handler=_READ_ONLY_HANDLER,
    )

    # Controls: the policy demonstrably gates writes on this session.
    assert (
        _evaluate_tool_call(
            http_client, session_id, "Write", {"file_path": "a.py", "content": "x"}
        )
        == "POLICY_ACTION_DENY"
    )
    assert (
        _evaluate_tool_call(
            http_client,
            session_id,
            "MultiEdit",
            {"file_path": "a.py", "edits": [{"old_string": "x", "new_string": "y"}]},
        )
        == "POLICY_ACTION_DENY"
    )

    # The bug: NotebookEdit mutates a file, so a report-only agent must be
    # denied. Fails on the buggy build (returns POLICY_ACTION_ALLOW).
    verdict = _evaluate_tool_call(
        http_client,
        session_id,
        "NotebookEdit",
        {"notebook_path": "analysis.ipynb", "new_source": "print(1)"},
    )
    assert verdict == "POLICY_ACTION_DENY", (
        f"read_only_os allowed NotebookEdit (verdict={verdict!r}): a report-only "
        "agent can silently mutate .ipynb files"
    )


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"notebook_path": "/etc/x.ipynb", "new_source": "x"}, id="notebook_path"),
        pytest.param({"file_path": "/etc/x.ipynb", "new_source": "x"}, id="file_path"),
    ],
)
def test_worktree_guard_denies_escaping_notebook_edit(
    live_server: str,
    http_client: httpx.Client,
    arguments: dict[str, Any],
) -> None:
    """ "Restrict Writes to Git Worktree" must DENY an escaping NotebookEdit.

    Control leg: an absolute-path Write is denied today. Then the same
    journey with NotebookEdit targeting an absolute path — via its real
    ``notebook_path`` argument and via a ``file_path`` spelling — must also
    DENY. On the buggy build both return ALLOW: the tool name is missing
    from ``_write_tools`` and the path extraction never reads
    ``notebook_path``.
    """
    session_id = _create_bare_session(http_client)
    _attach_policy(
        http_client,
        session_id,
        name="restrict-writes-to-worktree",
        handler=_WORKTREE_GUARD_HANDLER,
        factory_params={
            "allowed_root": "workspace",
            "deny_reason": "Writes must stay inside the workspace.",
        },
    )

    # Control: the guard demonstrably fires on this session for Write.
    assert (
        _evaluate_tool_call(
            http_client, session_id, "Write", {"file_path": "/etc/x.py", "content": "x"}
        )
        == "POLICY_ACTION_DENY"
    )

    # The bug: an out-of-tree NotebookEdit must be denied like any other
    # escaping write. Fails on the buggy build (returns POLICY_ACTION_ALLOW).
    verdict = _evaluate_tool_call(http_client, session_id, "NotebookEdit", arguments)
    assert verdict == "POLICY_ACTION_DENY", (
        f"worktree_guard allowed NotebookEdit with {arguments!r} (verdict={verdict!r}): "
        "notebook writes escape the worktree confinement"
    )

    # Guard for the fix: an in-tree relative notebook edit stays allowed —
    # the fix must add coverage, not blanket-deny notebooks.
    in_tree = _evaluate_tool_call(
        http_client,
        session_id,
        "NotebookEdit",
        {"notebook_path": "notebooks/a.ipynb", "new_source": "x"},
    )
    assert in_tree == "POLICY_ACTION_ALLOW", in_tree
