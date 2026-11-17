"""A real Codex child enforces its parent's session budget while the parent is idle.

The server, runner, Codex CLIs, delegation, usage forwarding, and native tool
hooks are real. Only the model Responses API is scripted. Synthetic token
usage and a test provider priced at $1/token cross a $100 cap without charges.
This tests the child's next tool gate, not interruption between gate events.

Run without credentials::

    uv run --no-sync pytest -o addopts='' \
        tests/e2e/test_codex_subagent_session_budget_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._helpers.server_runner import server_runner
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    release_mock_gate,
    upload_agent,
)

pytestmark = pytest.mark.timeout(300, method="signal")
_REPO = Path(__file__).resolve().parents[2]
_PARENT_MODEL = "mock-budget-parent"
_CHILD_MODEL = "mock-budget-child"


@pytest.fixture
def codex_budget_rig(
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[tuple[httpx.Client, Path, str, str]]:
    """Start an isolated native Codex stack using only a local mock provider."""
    for binary in ("codex", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} binary")
    mock_url = isolated_mock_llm_server_url
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    native_home = tmp_path / "home"
    codex_home = native_home / ".codex"
    for directory in (workspace, config_dir, codex_home):
        directory.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "budget-test": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _PARENT_MODEL, "worker": _CHILD_MODEL},
                            "pricing": {
                                "input_per_million": 1_000_000,
                                "output_per_million": 1_000_000,
                            },
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "CODEX_HOME": str(codex_home),
        "OMNIGENT_CONFIG_HOME": str(config_dir),
        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_SKIP_WEB_UI": "true",
        "OMNIGENT_CODEX_PATH": str(shutil.which("codex")),
    }
    with (
        server_runner(
            tmp_path,
            workspace=workspace,
            server_cwd=_REPO,
            base_env=base_env,
            server_env=env,
            health_timeout=60,
            poll_interval=0.5,
            wait_ready=False,
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            timeout=15,
            trust_env=False,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        assert stack.runner_home == native_home, "native configuration must match runner HOME"
        stack.start_runner(cwd=_REPO, env=env)
        yield client, workspace, stack.runner_id, mock_url


def _snapshot(
    client: httpx.Client, session_id: str, *, evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    snapshot = response.json()
    if evidence is not None:
        evidence.update(_budget_state_summary(snapshot))
    return snapshot


def _wait_for(probe: Callable[[], Any], *, description: str, deadline: float) -> Any:
    """Share one deadline across the journey so failures leave time for cleanup."""
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {description}")


def _shell_response(call_id: str, command: str, **options: Any) -> dict[str, Any]:
    return {
        "native_items": [
            {
                "type": "function_call",
                "id": f"fc-{call_id}",
                "call_id": call_id,
                "name": "exec_command",
                "arguments": json.dumps({"cmd": command}),
            }
        ],
        **options,
    }


def _budget_state_summary(state: dict[str, Any]) -> dict[str, Any]:
    """Keep only fixed state labels and bounded elicitation counts."""
    statuses = {
        "idle",
        "running",
        "waiting",
        "queued",
        "in_progress",
        "completed",
        "failed",
        "cancelled",
    }
    summary: dict[str, Any] = {}
    for key in ("status", "current_task_status"):
        value = state.get(key)
        summary[key] = value if isinstance(value, str) and value in statuses else "other"
    busy = state.get("busy")
    summary["busy"] = busy if isinstance(busy, bool) else "other"
    pending = state.get("pending_elicitations")
    count = len(pending) if isinstance(pending, list) else state.get("pending_elicitations_count")
    summary["pending_elicitations_count"] = (
        count if type(count) is int and 0 <= count <= 999_999 else "unavailable"
    )
    return summary


def _budget_request_evidence(requests: list[dict[str, Any]], states: dict[str, Any]) -> str:
    """Bound request structure to four requests, four items each and 4096 ASCII bytes."""
    labels = {
        "role": {"system", "developer", "user", "assistant", "tool"},
        "type": {
            "message",
            "function_call",
            "function_call_output",
            "reasoning",
            "item_reference",
        },
        "name": {"sys_session_send", "mcp__omnigent.sys_session_send", "exec_command"},
    }
    summary: dict[str, Any] = {
        "request_count": len(requests),
        "requests_omitted": max(0, len(requests) - 4),
        "parent_at_cost_check": _budget_state_summary(states.get("parent", {})),
        "child_at_discovery": _budget_state_summary(states.get("child", {})),
        "requests": [],
    }
    for request_index in range(max(0, len(requests) - 4), len(requests)):
        items = requests[request_index].get("input")
        detail: dict[str, Any] = {
            "request_index": request_index,
            "item_count": "other",
            "items": [],
        }
        if isinstance(items, list):
            detail["item_count"] = len(items)
            detail["items_omitted"] = max(0, len(items) - 4)
            for item_index in range(max(0, len(items) - 4), len(items)):
                item = items[item_index]
                entry: dict[str, Any] = {"item_index": item_index}
                for key, allowed in labels.items():
                    value = item.get(key) if isinstance(item, dict) else None
                    entry[key] = value if isinstance(value, str) and value in allowed else "other"
                detail["items"].append(entry)
        summary["requests"].append(detail)
    encoded = json.dumps(summary, ensure_ascii=True, separators=(",", ":"))
    if len(encoded) > 4096:
        encoded = json.dumps({"request_count": len(requests), "structure_omitted": True})
    return encoded


def _work_requests(
    mock_url: str, model: str, *, evidence: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Exclude the CLI's auxiliary title-generation requests from turn counts."""
    requests = [body for body in get_mock_requests(mock_url, key=model) if body.get("tools")]
    if evidence is not None:
        evidence["detail"] = _budget_request_evidence(requests, evidence)
    return requests


def test_codex_child_enforces_root_cost_budget_without_parent_activity(
    codex_budget_rig: tuple[httpx.Client, Path, str, str],
    tmp_path: Path,
) -> None:
    """An idle parent's $100 policy must deny its running child's next tool."""
    client, workspace, runner_id, mock_url = codex_budget_rig
    deadline = time.monotonic() + 180
    name = f"budget-parent-{uuid.uuid4().hex[:8]}"
    common = {
        "spec_version": 1,
        "executor": {
            "type": "omnigent",
            "auth": {"type": "provider", "name": "budget-test"},
            "config": {"harness": "codex-native", "yolo": True},
        },
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
    }
    parent = {
        **common,
        "name": name,
        "llm": {"model": _PARENT_MODEL},
        "prompt": "Dispatch the worker, then wait without doing more work.",
        "tools": {"agents": ["worker"]},
    }
    child = {
        **common,
        "name": "worker",
        "llm": {"model": _CHILD_MODEL},
        "prompt": "Perform the requested sequential shell calls. Respect policy denials.",
    }
    bundle = tmp_path / "bundle"
    child_dir = bundle / "agents" / "worker"
    child_dir.mkdir(parents=True)
    (bundle / "config.yaml").write_text(yaml.safe_dump(parent), encoding="utf-8")
    (child_dir / "config.yaml").write_text(yaml.safe_dump(child), encoding="utf-8")
    for marker in ("<session>", "<user_message>", "Generate a concise, single-line task title"):
        configure_mock_llm(mock_url, [{"text": "Budget test"}], match=marker)
    configure_mock_llm(
        mock_url,
        [
            {
                "native_items": [
                    {
                        "type": "function_call",
                        "id": "fc-dispatch-worker",
                        "call_id": "dispatch-worker",
                        "namespace": "mcp__omnigent",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "worker", "title": "budget-check", "args": "Run the steps."}
                        ),
                    }
                ]
            },
            {"text": "Waiting for worker."},
        ],
        key=_PARENT_MODEL,
    )
    configure_mock_llm(
        mock_url,
        [
            _shell_response("under-budget", "printf allowed > under-budget.txt"),
            _shell_response(
                "accrue-cost",
                "true",
                usage={"input_tokens": 10, "output_tokens": 90},
                block=True,
            ),
            _shell_response("over-budget", "printf forbidden > over-budget.txt", block=True),
            {"text": "CHILD_BUDGET_DENIED"},
        ],
        key=_CHILD_MODEL,
    )
    agent_name = upload_agent(client, bundle)
    parent_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    attached = client.post(
        f"/v1/sessions/{parent_id}/policies",
        json={
            "name": "session-cost-budget",
            "type": "python",
            "handler": "omnigent.policies.builtins.cost.cost_budget",
            "factory_params": {"max_cost_usd": 100},
        },
    )
    attached.raise_for_status()
    try:
        response = client.post(
            f"/v1/sessions/{parent_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Dispatch worker and wait."}],
                },
            },
        )
        response.raise_for_status()

        evidence: dict[str, Any] = {"parent": {}, "child": {}}

        def find_child() -> str | None:
            response = client.get(f"/v1/sessions/{parent_id}/child_sessions")
            response.raise_for_status()
            rows = response.json()["data"]
            if rows:
                evidence["child"] = _budget_state_summary(rows[0])
            return rows[0]["id"] if rows else None

        child_id = _wait_for(find_child, description="native child dispatch", deadline=deadline)
        _wait_for(
            lambda: len(_work_requests(mock_url, _CHILD_MODEL)) == 2,
            description="child's second model request at the mock gate",
            deadline=deadline,
        )
        _wait_for(
            lambda: _snapshot(client, parent_id).get("status") == "idle",
            description="parent becoming idle before child exhausts the budget",
            deadline=deadline,
        )
        assert (workspace / "under-budget.txt").read_text() == "allowed"
        assert (
            _snapshot(client, parent_id, evidence=evidence["parent"]).get("total_cost_usd") or 0
        ) < 100
        parent_requests = len(_work_requests(mock_url, _PARENT_MODEL, evidence=evidence))
        assert parent_requests == 2, evidence["detail"]
        release_mock_gate(mock_url)
        _wait_for(
            lambda: len(_work_requests(mock_url, _CHILD_MODEL)) == 3,
            description="child's third model request at the mock gate",
            deadline=deadline,
        )
        _wait_for(
            lambda: (_snapshot(client, child_id).get("total_cost_usd") or 0) >= 100,
            description="native child usage crossing the root's $100 cap",
            deadline=deadline,
        )
        assert not (workspace / "over-budget.txt").exists()
        assert _snapshot(client, child_id)["harness"] == "codex-native"
        policies = client.get(f"/v1/sessions/{child_id}/policies")
        policies.raise_for_status()
        assert policies.json()["data"] == [], policies.text
        assert len(_work_requests(mock_url, _PARENT_MODEL)) == parent_requests

        release_mock_gate(mock_url)

        def denied_tool_result() -> bool:
            requests = _work_requests(mock_url, _CHILD_MODEL)
            for request in requests:
                for item in request.get("input", []):
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "function_call_output"
                        and item.get("call_id") == "over-budget"
                    ):
                        output = json.dumps(item.get("output", ""))
                        assert "$100.00" in output and "budget" in output.lower(), (
                            f"Child's over-budget tool was not denied by the cost policy: {output}"
                        )
                        return True
            return False

        _wait_for(
            denied_tool_result,
            description="the real Codex tool result carrying the inherited budget denial",
            deadline=deadline,
        )
        assert not (workspace / "over-budget.txt").exists(), "over-budget tool actually executed"
        assert len(_work_requests(mock_url, _PARENT_MODEL)) == parent_requests
    finally:
        with contextlib.suppress(httpx.HTTPError):
            release_mock_gate(mock_url)
        with contextlib.suppress(httpx.HTTPError):
            client.post(
                f"/v1/sessions/{parent_id}/events", json={"type": "stop_session"}, timeout=5
            )
