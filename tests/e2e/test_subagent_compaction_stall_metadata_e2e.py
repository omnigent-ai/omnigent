"""An orchestrator must be able to tell a compaction-stalled sub-agent from a healthy one.

A mock ``openai-agents`` orchestrator dispatches a real ``claude-native`` child
through ``sys_session_send`` (purpose ``implement``) with a large read-phase
prompt. The child's Claude Code reads files until the mock model reports a
near-full context, auto-compacts, reads again, auto-compacts a second time, and
then its next model reply is held on the mock gate so the turn never advances
(no further tool calls, no writes). The orchestrator then runs two status
checks ten seconds apart through ``sys_session_get_info`` and ``sys_read_inbox``.

Claude Code only enforces a context window it knows; a third-party base URL
leaves the window unenforced, so the child's HOME carries
``CLAUDE_CODE_AUTO_COMPACT_WINDOW`` (also exported to the runner) and the mock
reports ``input_tokens`` above the window minus Claude's 13k buffer.

The model APIs are mocked; the server, runner, native Claude CLI, hooks, tmux
pane and the runner-dispatched ``sys_session_*`` tools are real::

    uv run --no-sync pytest -o addopts='' \\
        tests/e2e/test_subagent_compaction_stall_metadata_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import _mock_llm_server_process

pytestmark = pytest.mark.timeout(900, method="signal")

_PARENT_MODEL = "mock-compaction-stall-parent"
_CHILD_MODEL = "claude-sonnet-4-20250514"
# Claude Code compacts once the last reply's prompt usage reaches the enforced
# window minus its 13k buffer.
_AUTO_COMPACT_WINDOW = 100_000
_NEAR_FULL_USAGE = {
    "input_tokens": 95_000,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
_SMALL_USAGE = {
    "input_tokens": 1_200,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}
_DOC_COUNT = 6
_HELD_REPLY = "HELD-REPLY: never delivered while the gate is closed."
_POLL_GAP_S = 10.0
_STATUS_TOOLS = ("sys_session_get_info", "sys_read_inbox")


def _write_docs(workspace: Path) -> None:
    docs = workspace / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for i in range(1, _DOC_COUNT + 1):
        lines = [f"# Guide {i}", ""]
        lines += [
            f"Section {n}: guide {i} explains step {n} of the workflow in detail."
            for n in range(300)
        ]
        (docs / f"guide-{i}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=repro@example.com", "-c", "user.name=repro", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass
class Rig:
    client: httpx.Client
    stack: ServerRunner
    mock_url: str
    workspace: Path


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Rig]:
    """Run an isolated server/runner; the child uses the real Claude CLI + mock API."""
    for binary in ("claude", "tmux", "git"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} binary")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip("machine-managed Claude settings override mock auth; run in a clean container")
    root = tmp_path_factory.mktemp("compaction-stall")
    (root / "mock-logs").mkdir()
    mock_iter = _mock_llm_server_process(root / "mock-logs")
    mock_url = next(mock_iter)
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "repro-claude": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {"default": _CHILD_MODEL},
                        },
                    },
                    "repro-openai": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _PARENT_MODEL},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    try:
        with server_runner(
            root / "stack", server_env={"OMNIGENT_CONFIG_HOME": str(config_dir)}
        ) as stack:
            _write_docs(stack.workspace)
            _git(stack.workspace, "init", "-q", "-b", "main")
            _git(stack.workspace, "add", ".")
            _git(stack.workspace, "commit", "-q", "-m", "docs baseline")
            claude_dir = stack.runner_home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            (claude_dir / "settings.json").write_text(
                json.dumps(
                    {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(_AUTO_COMPACT_WINDOW)}}
                ),
                encoding="utf-8",
            )
            (stack.runner_home / ".claude.json").write_text(
                json.dumps(
                    {
                        "hasCompletedOnboarding": True,
                        "theme": "dark",
                        "projects": {
                            str(stack.workspace.resolve()): {"hasTrustDialogAccepted": True}
                        },
                    }
                ),
                encoding="utf-8",
            )
            stack.start_runner(
                env={
                    "OMNIGENT_CONFIG_HOME": str(config_dir),
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(_AUTO_COMPACT_WINDOW),
                    "OMNIGENT_SKIP_ONBOARD": "1",
                    "OMNIGENT_NO_UPDATE_CHECK": "1",
                }
            )
            with httpx.Client(
                base_url=stack.base_url,
                timeout=30,
                trust_env=False,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    "x-omnigent-background-session-titles": "off",
                },
            ) as client:
                yield Rig(client=client, stack=stack, mock_url=mock_url, workspace=stack.workspace)
    finally:
        with contextlib.suppress(StopIteration):
            next(mock_iter)


def _mock_post(mock_url: str, path: str, body: dict[str, Any]) -> None:
    httpx.post(f"{mock_url}{path}", json=body, timeout=10, trust_env=False).raise_for_status()


def _read_call(workspace: Path, index: int, usage: dict[str, int]) -> dict[str, Any]:
    return {
        "tool_calls": [
            {
                "call_id": f"toolu_read_guide_{index}",
                "name": "Read",
                "arguments": json.dumps(
                    {"file_path": str(workspace / "docs" / f"guide-{index}.md")}
                ),
            }
        ],
        "usage": usage,
    }


def _configure_child_model(mock_url: str, workspace: Path) -> int:
    """Script the read phase: reads, compaction, read, compaction, then a held reply.

    :returns: Number of scripted replies; tool-bearing requests beyond it would fall
        through to the non-blocking fallback and end the park.
    """
    responses = [_read_call(workspace, i, _SMALL_USAGE) for i in range(1, _DOC_COUNT - 1)]
    responses += [
        _read_call(workspace, _DOC_COUNT - 1, _NEAR_FULL_USAGE),
        {"text": "Summary after the first compaction: guides 1-5 were read verbatim."},
        _read_call(workspace, _DOC_COUNT, _NEAR_FULL_USAGE),
        {"text": "Summary after the second compaction: guides 1-6 were read verbatim."},
    ]
    # A long blocking tail keeps the park alive even if the CLI retries the held call.
    responses += [{"text": _HELD_REPLY, "block": True}] * 30
    # Claude's title/background requests advertise no tools; keep them off this queue.
    _mock_post(
        mock_url,
        "/mock/configure",
        {"key": _CHILD_MODEL, "required_tools": ["Read"], "responses": responses},
    )
    _mock_post(mock_url, "/mock/set_fallback", {"key": _CHILD_MODEL, "text": _HELD_REPLY})
    _mock_post(mock_url, "/mock/set_fallback", {"key": "default", "text": "ok"})
    return len(responses)


def _dispatch_prompt() -> str:
    files = ", ".join(f"docs/guide-{i}.md" for i in range(1, _DOC_COUNT + 1))
    return (
        "Restructure the docs information architecture. Before writing anything, read "
        f"each of these files verbatim and in full: {files}. Then move their content "
        "into docs/new/ preserving every sentence, update cross references, and "
        "commit when done."
    )


def _configure_parent_dispatch(mock_url: str) -> None:
    _mock_post(
        mock_url,
        "/mock/configure",
        {
            "key": _PARENT_MODEL,
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_dispatch_implementer",
                            "name": "sys_session_send",
                            "arguments": json.dumps(
                                {
                                    "agent": "implementer",
                                    "title": "docs-ia",
                                    "args": {"input": _dispatch_prompt(), "purpose": "implement"},
                                }
                            ),
                        }
                    ]
                },
                {"text": "Implementer dispatched; waiting for its result."},
            ],
        },
    )
    _mock_post(mock_url, "/mock/set_fallback", {"key": _PARENT_MODEL, "text": "Acknowledged."})


def _configure_parent_status_check(mock_url: str, child_id: str, n: int) -> None:
    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool_calls": [{"call_id": f"{name}_{n}", "name": name, "arguments": json.dumps(args)}]
        }

    _mock_post(
        mock_url,
        "/mock/configure",
        {
            "key": _PARENT_MODEL,
            "responses": [
                call("sys_session_get_info", {"session_id": child_id}),
                call("sys_read_inbox", {}),
                {"text": f"Status check {n} recorded."},
            ],
        },
    )


def _register_parent(client: httpx.Client, mock_url: str) -> str:
    name = f"compaction-stall-orchestrator-{uuid.uuid4().hex[:8]}"
    spec = {
        "name": name,
        "prompt": (
            "You are an orchestrator. Dispatch the implementer sub-agent via "
            "sys_session_send when asked, and check on it with sys_session_get_info "
            "and sys_read_inbox when asked for a status check."
        ),
        "executor": {
            "harness": "openai-agents",
            "model": _PARENT_MODEL,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock_url}/v1"},
        },
        "tools": {
            "implementer": {
                "type": "agent",
                "description": "Claude Code implementer sub-agent for multi-file docs work.",
                "executor": {"harness": "claude-native"},
                "prompt": (
                    "You are a coding sub-agent. Read the referenced files verbatim "
                    "before writing."
                ),
            }
        },
    }
    bundle_bytes = bundle_files({f"{name}.yaml": yaml.safe_dump(spec).encode()})
    resp = post_session_bundle(client.post, "/v1/sessions", bundle_bytes)
    assert resp.status_code in (200, 201, 409), f"{resp.status_code} {resp.text[:400]}"
    listing = client.get(
        "/v1/sessions", params={"visibility": "all", "agent_name": name, "limit": 1}
    )
    listing.raise_for_status()
    return str(listing.json()["data"][0]["agent_id"])


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    resp = client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}, timeout=10
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _item_type(item: dict[str, Any]) -> str | None:
    return item.get("type") or (item.get("data") or {}).get("type")


def _tool_outputs(items: list[dict[str, Any]]) -> dict[str, str]:
    """Map ``call_id`` to the persisted output of every function call in *items*."""
    out: dict[str, str] = {}
    for item in items:
        data = item.get("data") or {}
        if _item_type(item) == "function_call_output":
            cid = item.get("call_id") or data.get("call_id")
            text = item.get("output") or data.get("output")
            if cid and text:
                out[str(cid)] = str(text)
    return out


def _compaction_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    return [item for item in _items(client, session_id) if _item_type(item) == "compaction"]


def _child_tool_requests(mock_url: str) -> int:
    """Count the child's model calls that advertised tools, i.e. consumed the scripted queue."""
    resp = httpx.get(
        f"{mock_url}/mock/requests", params={"key": _CHILD_MODEL}, timeout=10, trust_env=False
    )
    resp.raise_for_status()
    return sum(1 for r in resp.json()["requests"] if isinstance(r, dict) and r.get("tools"))


def _gate_pending(mock_url: str) -> bool:
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _send_user_message(client: httpx.Client, session_id: str, text: str) -> None:
    send = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    assert send.status_code == 202, f"{send.status_code} {send.text}"


def _status_check(
    client: httpx.Client, mock_url: str, parent_id: str, child_id: str, n: int
) -> dict[str, str]:
    """Run status check *n* on the orchestrator; return each status tool's raw output."""
    _configure_parent_status_check(mock_url, child_id, n)
    _send_user_message(client, parent_id, f"Status check {n}: how is the implementer doing?")
    assert _wait_until(
        lambda: f"sys_read_inbox_{n}" in _tool_outputs(_items(client, parent_id)), timeout=120
    ), f"orchestrator status check {n} did not complete"
    outputs = _tool_outputs(_items(client, parent_id))
    missing = [name for name in _STATUS_TOOLS if f"{name}_{n}" not in outputs]
    assert not missing, f"status check {n} outputs missing {missing}: {sorted(outputs)}"
    return {name: outputs[f"{name}_{n}"] for name in _STATUS_TOOLS}


def _stop(client: httpx.Client, *session_ids: str) -> None:
    for sid in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            client.post(f"/v1/sessions/{sid}/events", json={"type": "stop_session"}, timeout=5)


@dataclass
class StallObservation:
    parent_id: str
    child_id: str
    check_1: dict[str, str]
    check_2: dict[str, str]
    compaction_items: list[dict[str, Any]]
    git_status_before: str
    git_status_after: str
    head_before: str
    head_after: str
    gate_pending: bool
    scripted_child_replies: int
    child_tool_requests: int

    def get_info(self, n: int) -> dict[str, Any]:
        return json.loads((self.check_1, self.check_2)[n - 1]["sys_session_get_info"])

    def inbox(self, n: int) -> str:
        return (self.check_1, self.check_2)[n - 1]["sys_read_inbox"]


@pytest.fixture(scope="module")
def stalled_subagent(rig: Rig) -> Iterator[StallObservation]:
    """Drive the journey once and keep the orchestrator's observations."""
    client, mock_url, workspace = rig.client, rig.mock_url, rig.workspace
    scripted_child_replies = _configure_child_model(mock_url, workspace)
    _configure_parent_dispatch(mock_url)
    agent_id = _register_parent(client, mock_url)
    create = client.post("/v1/sessions", json={"agent_id": agent_id})
    create.raise_for_status()
    parent_id = str(create.json()["id"])
    client.patch(
        f"/v1/sessions/{parent_id}", json={"runner_id": rig.stack.runner_id}
    ).raise_for_status()
    git_status_before = _git(workspace, "status", "--porcelain")
    head_before = _git(workspace, "rev-parse", "HEAD")

    _send_user_message(
        client, parent_id, "Dispatch the implementer sub-agent to restructure the docs."
    )

    child_id: str | None = None

    def child_seen() -> bool:
        nonlocal child_id
        resp = client.get(f"/v1/sessions/{parent_id}/child_sessions")
        if resp.status_code == 200 and resp.json().get("data"):
            row = resp.json()["data"][0]
            raw_id = row.get("session_id") or row.get("id")
            if raw_id is None:
                return False
            child_id = str(raw_id)
            return True
        return False

    try:
        assert _wait_until(child_seen, timeout=120), "orchestrator never dispatched the child"
        assert child_id is not None

        def compacted_twice_and_parked() -> bool:
            return len(_compaction_items(client, child_id)) >= 2 and _gate_pending(mock_url)

        assert _wait_until(compacted_twice_and_parked, timeout=240, interval=2.0), (
            f"child did not compact twice and park; compaction items: "
            f"{len(_compaction_items(client, child_id))}, gate pending: {_gate_pending(mock_url)}"
        )
        # Let the forwarder persist anything still in flight before polling.
        time.sleep(5)
        check_1 = _status_check(client, mock_url, parent_id, child_id, 1)
        time.sleep(_POLL_GAP_S)
        check_2 = _status_check(client, mock_url, parent_id, child_id, 2)
        observation = StallObservation(
            parent_id=parent_id,
            child_id=child_id,
            check_1=check_1,
            check_2=check_2,
            compaction_items=_compaction_items(client, child_id),
            git_status_before=git_status_before,
            git_status_after=_git(workspace, "status", "--porcelain"),
            head_before=head_before,
            head_after=_git(workspace, "rev-parse", "HEAD"),
            gate_pending=_gate_pending(mock_url),
            scripted_child_replies=scripted_child_replies,
            child_tool_requests=_child_tool_requests(mock_url),
        )
        print("STALL_OBSERVATION " + json.dumps(asdict(observation), default=str)[:40000])
        yield observation
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_url}/gate/release", timeout=5, trust_env=False)
        if child_id is not None:
            _stop(client, child_id)
        _stop(client, parent_id)


def test_parked_subagent_metadata_reports_compactions(stalled_subagent: StallObservation) -> None:
    """``sys_session_get_info`` must expose the two compactions next to the heartbeat."""
    obs = stalled_subagent
    # Parked state: two compactions, the model reply still held, no writes, no inbox notice.
    assert len(obs.compaction_items) == 2, obs.compaction_items
    assert obs.gate_pending, "child turn should still be held open"
    assert obs.child_tool_requests <= obs.scripted_child_replies, (
        "scripted child replies exhausted; the held reply fell through to the fallback"
    )
    # The runner leaves its own startup probe dir in the workspace; only new paths count.
    new_paths = set(obs.git_status_after.splitlines()) - set(obs.git_status_before.splitlines())
    assert not new_paths, f"sub-agent wrote to the workspace: {sorted(new_paths)}"
    assert obs.head_before == obs.head_after
    for n in (1, 2):
        assert "docs-ia" not in obs.inbox(n) and obs.child_id not in obs.inbox(n), obs.inbox(n)
    info = obs.get_info(1)
    assert info.get("status") == "running", info
    assert info.get("runner_online") is True, info
    assert info.get("pending_elicitation_count") == 0, info
    last_activity_at = info.get("last_activity_at")
    assert isinstance(last_activity_at, int), info
    # get_info metadata must expose the compaction aggregate without transcript scraping.
    assert info.get("compaction_count") == 2, info
    last_compaction_at = info.get("last_compaction_at")
    assert isinstance(last_compaction_at, int) and last_activity_at >= last_compaction_at, info
    # A second reading while the reply is still held must show the same aggregate
    # and an unchanged heartbeat: the parked session has produced nothing since.
    again = obs.get_info(2)
    assert again.get("compaction_count") == 2, again
    assert again.get("last_compaction_at") == last_compaction_at, again
    assert again.get("last_activity_at") == last_activity_at, again
