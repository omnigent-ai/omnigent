"""A codex-native parent is woken when its independent child session finishes.

A codex-native orchestrator dispatches an independent Omnigent sub-agent via
``sys_session_send`` (not a codex ``/side`` thread) and goes idle. When the
child finishes and its result reaches the parent's inbox, the runner must post
the ``[System: ... waiting in inbox]`` wake notice so the idle parent takes a
continuation turn and surfaces the result -- the contract a claude-native
parent already honors. Suppression is keyed on the child's ``omnigent.wrapper``
label, not the parent's harness. Both agents are real Codex CLIs; only the
model Responses API is scripted.

Run without credentials::

    uv run --no-sync pytest -o addopts='' \
        tests/e2e/test_codex_native_parent_independent_child_wake_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
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
    upload_agent,
)

pytestmark = pytest.mark.timeout(300, method="signal")
_REPO = Path(__file__).resolve().parents[2]
_PARENT_MODEL = "mock-wake-parent"
_CHILD_MODEL = "mock-wake-child"

# Emitted ONLY by the runner's auto-wake path (_format_subagent_wake_notice);
# the sys_read_inbox drain message does not contain it, so its presence is an
# auto-wake-specific signal.
_WAKE_NOTICE_SIGNATURE = "waiting in inbox"
_CHILD_MARKER = "CODEX_CHILD_DONE_8842"
# Reachable only via the auto-wake continuation turn (the third parent reply).
_CONTINUATION_MARKER = "PARENT_CONTINUATION_8842"


@pytest.fixture
def codex_wake_rig(
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
                    "wake-test": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _PARENT_MODEL, "worker": _CHILD_MODEL},
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


def _snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json()


def _items_blob(client: httpx.Client, session_id: str) -> str:
    return json.dumps(_snapshot(client, session_id).get("items", []))


def _wait_for(probe: Callable[[], Any], *, description: str, deadline: float) -> Any:
    """Share one deadline across the journey so failures leave time for cleanup."""
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"Timed out waiting for {description}")


def test_codex_native_parent_woken_by_independent_child(
    codex_wake_rig: tuple[httpx.Client, Path, str, str],
    tmp_path: Path,
) -> None:
    """A finished independent child must auto-wake its idle codex-native parent."""
    client, _workspace, runner_id, mock_url = codex_wake_rig
    deadline = time.monotonic() + 240
    common = {
        "spec_version": 1,
        "executor": {
            "type": "omnigent",
            "auth": {"type": "provider", "name": "wake-test"},
            "config": {"harness": "codex-native", "yolo": True},
        },
        "os_env": {
            "type": "caller_process",
            "cwd": str(_workspace),
            "sandbox": {"type": "none"},
        },
    }
    parent = {
        **common,
        "name": "orchestrator",
        "llm": {"model": _PARENT_MODEL},
        "prompt": (
            "You are an orchestrator. When asked, dispatch the worker sub-agent "
            "with sys_session_send, then end your turn and wait. When woken, "
            "report the worker's result."
        ),
        "tools": {"agents": ["worker"]},
    }
    child = {
        **common,
        "name": "worker",
        "llm": {"model": _CHILD_MODEL},
        "prompt": "You are the worker. Complete the task and reply with the result.",
    }
    bundle = tmp_path / "bundle"
    child_dir = bundle / "agents" / "worker"
    child_dir.mkdir(parents=True)
    (bundle / "config.yaml").write_text(yaml.safe_dump(parent), encoding="utf-8")
    (child_dir / "config.yaml").write_text(yaml.safe_dump(child), encoding="utf-8")

    # Codex's auxiliary title/summary requests must not drain the scripted turns.
    for marker in ("<session>", "<user_message>", "Generate a concise, single-line task title"):
        configure_mock_llm(mock_url, [{"text": "Working."}], match=marker)
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
                            {
                                "agent": "worker",
                                "title": "worker-task",
                                "args": "Produce the completion marker.",
                            }
                        ),
                    }
                ]
            },
            {"text": "Dispatched the worker; waiting for its result."},
            # Only reachable via the auto-wake continuation turn.
            {"text": f"{_CONTINUATION_MARKER}: the worker returned {_CHILD_MARKER}"},
        ],
        key=_PARENT_MODEL,
    )
    configure_mock_llm(
        mock_url,
        [{"text": f"Task complete. {_CHILD_MARKER}"}] * 6,
        key=_CHILD_MODEL,
    )

    agent_name = upload_agent(client, bundle)
    parent_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    try:
        send = client.post(
            f"/v1/sessions/{parent_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Dispatch the worker and wait."}],
                },
            },
        )
        send.raise_for_status()

        def find_child() -> str | None:
            rows = client.get(f"/v1/sessions/{parent_id}/child_sessions").json()["data"]
            return rows[0]["id"] if rows else None

        child_id = _wait_for(find_child, description="worker child dispatch", deadline=deadline)

        _wait_for(
            lambda: _CHILD_MARKER in _items_blob(client, child_id),
            description="the worker persisting its completion marker",
            deadline=deadline,
        )
        _wait_for(
            lambda: _snapshot(client, parent_id).get("status") == "idle",
            description="the parent orchestrator going idle after dispatch",
            deadline=deadline,
        )
        assert _snapshot(client, child_id)["harness"] == "codex-native"

        wake_seen = False
        continuation_seen = False
        wake_deadline = time.monotonic() + 120
        while time.monotonic() < wake_deadline:
            blob = _items_blob(client, parent_id)
            wake_seen = wake_seen or (_WAKE_NOTICE_SIGNATURE in blob)
            continuation_seen = continuation_seen or (_CONTINUATION_MARKER in blob)
            if wake_seen and continuation_seen:
                break
            time.sleep(2.0)

        assert wake_seen, (
            f"the worker child {child_id} completed its turn (its result "
            f"{_CHILD_MARKER!r} is persisted in the child transcript) but the "
            f"codex-native parent {parent_id} never received the auto-wake notice "
            f"({_WAKE_NOTICE_SIGNATURE!r}) within 120s -- the completion never woke "
            "the orchestrator. A claude-native parent wakes on the same journey."
        )
        assert continuation_seen, (
            "the wake notice arrived but the parent never took the continuation turn "
            f"surfacing {_CONTINUATION_MARKER!r}."
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            client.post(
                f"/v1/sessions/{parent_id}/events", json={"type": "stop_session"}, timeout=5
            )
