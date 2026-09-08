"""E2E: codex-native must honor the top rungs (max/ultra) of its effort ladder.

A native Codex session legitimately runs at ``max`` / ``ultra`` reasoning
effort — the codex app-server advertises those rungs per model and the native
executor's own vocabulary (``CODEX_NATIVE_EFFORTS``) accepts them — but the
harness capability table declares the shorter Responses-wire ladder
(``EffortFamily.OPENAI``, capped at ``xhigh``) for ``codex-native``. Every
ladder consumer that trusts ``efforts_for_harness("codex-native")`` therefore
mishandles the top rungs:

* **Dispatch** — ``sys_session_send`` with ``args.reasoning_effort: "max"``
  targeting a codex-native sub-agent runs the runner's dispatch gate
  (``_validate_subagent_reasoning_effort``), which validates against the short
  ladder. The deprecated-alias fold coerces the "unsupported" ``max`` down to
  ``xhigh``, so the child session is silently created one rung below what the
  caller asked for. (Before the alias fold existed, the same gap rejected the
  dispatch outright with ``ValueError`` — either way the caller's ``max``
  never reaches the child.)
* **Per-turn delivery** — a session whose persisted effort is ``max`` hits the
  runner's reasoning guard in ``_run_turn_bg``, which checks membership in the
  same short ladder and silently drops the effort for the turn (a runner-log
  warning is the only trace; the turn runs without the requested effort).

Both tests drive the real product path — a real ``omnigent server`` and a real
``omnigent.runner._entry`` runner from the conftest rig — and are red while
the ladder gap lives:

* ``test_dispatched_max_effort_survives_to_codex_native_child`` fails because
  the child session row persists ``xhigh`` instead of ``max``.
* ``test_persisted_max_effort_is_delivered_on_codex_native_turn`` fails
  because the runner log carries the ``dropping reasoning effort`` warning
  for the session's turn.

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke
with::

    pytest tests/e2e/test_codex_native_effort_ladder_e2e.py -v
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

# CI forces an egress proxy via HTTP(S)_PROXY env vars that must not intercept
# loopback requests to the spawned server / runner / mock-LLM trio. Exclude
# loopback at import time so the shared conftest's ambient httpx health checks
# (and every spawned subprocess, which inherits os.environ) bypass the proxy.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))

from tests.e2e.conftest import (  # noqa: E402
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S  # noqa: E402

pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
]

# How long the child session may take to appear after the parent's dispatch
# turn completes (creation is synchronous inside the tool call, so this is
# pure polling slack).
_CHILD_APPEAR_TIMEOUT_S = 120.0

# How long the codex-native turn may take to reach the runner's delivery
# path. The buggy path logs the drop warning within seconds of the send; the
# fixed path proceeds into the codex TUI launch, which on a credential-less
# box burns the ~30s thread-start timeout before erroring — so leave headroom
# for a full launch-and-fail cycle.
_TURN_DELIVERY_TIMEOUT_S = 180.0


# ─── Facet 1: dispatch-time effort override ────────────────────────────────


@pytest.fixture(scope="module")
def codex_dispatch_parent(
    http_client: httpx.Client,
    mock_llm_server_url: str,
) -> tuple[str, str]:
    """Register a mock-LLM parent that exposes a codex-native sub-agent.

    The parent runs on the openai-agents harness against the mock LLM (the
    standard hermetic-orchestrator pattern, see test_sub_agent_phase3_e2e).
    Its single inline sub-agent tool pins ``harness: codex-native`` — the
    polly codex-worker shape — so a ``sys_session_send`` dispatch resolves
    the child harness this bug is about.

    :param http_client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: ``(parent_agent_name, parent_model)``.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-effort-parent-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"
    parent_name = register_inline_agent(
        http_client,
        name=f"codex-effort-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are the codex-native effort-ladder e2e fixture parent. "
            "Dispatch the codex_worker sub-agent when asked."
        ),
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "codex_worker": {
                    "type": "agent",
                    "description": "Native Codex worker (codex-native harness).",
                    "executor": {"harness": "codex-native"},
                    "prompt": "You are the native Codex worker sub-agent.",
                },
            },
        },
    )
    return parent_name, parent_model


def _wait_for_child_sessions(
    client: httpx.Client,
    parent_id: str,
    *,
    timeout_s: float = _CHILD_APPEAR_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Poll until *parent_id* has at least one child session.

    :param client: HTTP client pointed at the live server.
    :param parent_id: The dispatching (parent) session id.
    :returns: The child-session rows.
    :raises AssertionError: If no child ever appears — including the older
        shape of this same ladder gap, where the dispatch gate *rejected*
        ``reasoning_effort: max`` outright and no child was created; the
        parent transcript is included so that rejection is visible.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{parent_id}/child_sessions")
        if resp.status_code == 200:
            kids = resp.json().get("data", [])
            if kids:
                return kids
        time.sleep(POLL_INTERVAL_S)
    snap = client.get(f"/v1/sessions/{parent_id}")
    items_blob = (
        json.dumps(snap.json().get("items", []))[:1500] if snap.status_code == 200 else "<gone>"
    )
    raise AssertionError(
        "no codex_worker child session appeared after the dispatch turn: "
        "either the dispatch gate rejected reasoning_effort='max' (the "
        "reject shape of the codex-native effort-ladder gap) or the "
        f"dispatch never ran. Parent items: {items_blob}"
    )


def test_dispatched_max_effort_survives_to_codex_native_child(
    http_client: httpx.Client,
    codex_dispatch_parent: tuple[str, str],
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A ``reasoning_effort: max`` dispatch must persist ``max`` on the child.

    Journey: the orchestrator's brain emits ``sys_session_send`` targeting
    the codex-native worker with ``args.reasoning_effort: "max"`` (a rung the
    native codex ladder supports). The child session must be created with
    exactly that effort.

    Red while the ladder gap lives: the dispatch gate validates ``max``
    against the short OPENAI ladder, the deprecated-alias fold silently
    rewrites it to ``xhigh``, and the child session row persists the clamped
    value — the user's pick never reaches the native Codex worker.
    """
    parent_name, parent_model = codex_dispatch_parent
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-effort-max-{uuid.uuid4().hex[:6]}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "codex_worker",
                                "title": "effort-probe",
                                "args": {
                                    "input": "Reply with the word OK.",
                                    "reasoning_effort": "max",
                                },
                            }
                        ),
                    }
                ],
            },
            {"text": "Dispatched codex_worker with reasoning_effort=max."},
            # Absorb a possible auto-wake continuation when the child turn
            # later reaches a terminal state (it may error on a box with no
            # Codex credential; that is irrelevant to this assertion).
            {"text": "ack"},
            {"text": "ack"},
        ],
        key=parent_model,
    )

    parent_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    child_id: str | None = None
    try:
        response_id = send_user_message_to_session(
            http_client,
            session_id=parent_id,
            content="Dispatch codex_worker with reasoning effort max.",
        )
        body = poll_session_until_terminal(
            http_client,
            session_id=parent_id,
            response_id=response_id,
            timeout=240.0,
        )
        assert body["status"] == "completed", (
            f"dispatch turn did not complete: status={body.get('status')!r}, "
            f"error={body.get('error')!r}"
        )

        kids = _wait_for_child_sessions(http_client, parent_id)
        assert len(kids) == 1, f"expected exactly one dispatched child, got {kids!r}"
        child_id = str(kids[0].get("session_id") or kids[0].get("id"))
        child = http_client.get(f"/v1/sessions/{child_id}")
        child.raise_for_status()
        child_effort = child.json().get("reasoning_effort")
        assert child_effort == "max", (
            "sys_session_send dispatched the codex-native worker with "
            "reasoning_effort='max' (a rung the native codex ladder "
            "supports), but the child session was created with "
            f"reasoning_effort={child_effort!r} — the dispatch gate "
            "validates against the short OPENAI ladder and silently clamps "
            "the caller's pick instead of passing it through"
        )
    finally:
        if child_id is not None:
            http_client.delete(f"/v1/sessions/{child_id}", timeout=30.0)
        http_client.delete(f"/v1/sessions/{parent_id}", timeout=30.0)


# ─── Facet 2: per-turn delivery of a persisted effort ──────────────────────


# The polly codex sub-agent shape reduced to the fields that pick the
# codex-native launch path (mirrors test_codex_native_headless_subagent_e2e).
_CODEX_NATIVE_SPEC_YAML = """\
spec_version: 1
name: codex-effort-turn
description: Native Codex session for the max-effort per-turn e2e.

executor:
  type: omnigent
  config:
    harness: codex-native
    yolo: true

prompt: |
  You are Codex, driven end-to-end for the reasoning-effort ladder test.

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _codex_spec_bundle() -> bytes:
    """Gzip the codex-native spec as a session bundle (strict parser path)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _CODEX_NATIVE_SPEC_YAML.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _runner_process_log_text() -> str:
    """Concatenated contents of the rig runner's process logs.

    The runner subprocess writes its records to per-process files under
    ``<data-dir>/logs/runner`` (its stdout stays empty), so log-keyed
    assertions must read those files. The pytest rig points the data dir at
    a per-session temp root, so only this run's runner files are here; a
    unique conversation id keys every search anyway.
    """
    from omnigent.process_logging import process_log_dir

    runner_dir = process_log_dir("runner")
    if not runner_dir.exists():
        return ""
    return "\n".join(
        log_file.read_text(errors="replace") for log_file in sorted(runner_dir.glob("*.log"))
    )


def _wait_for_runner_log_marker(marker: str, *, timeout_s: float) -> bool:
    """Poll the runner process logs until *marker* appears."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if marker in _runner_process_log_text():
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


def test_persisted_max_effort_is_delivered_on_codex_native_turn(
    http_client: httpx.Client,
    live_runner_id: str,
    live_server: str,
    tmp_path: Path,
) -> None:
    """A codex-native turn must carry the session's ``max`` reasoning effort.

    Journey: create a codex-native session bound to a runner (the native
    terminal launches), set its reasoning effort to ``max`` (the web effort
    picker's PATCH — accepted, since the persisted value is validated
    against the union vocabulary, and live-forwarded to the runner as an
    ``effort_change``), then send a message. The runner resolves the
    remembered effort for the turn and must deliver it with the turn.

    Red while the ladder gap lives: the reasoning guard in the runner's turn
    delivery checks ``max`` against the short OPENAI ladder, drops it, and
    logs ``dropping reasoning effort 'max' — harness codex-native accepts
    ...`` — the turn silently runs without the user's requested effort.

    Non-vacuous by construction: the test first requires the runner to have
    *received* the ``effort_change`` (so the remembered effort demonstrably
    exists), then requires the turn to demonstrably reach the runner's
    delivery path (drop warning, codex thread start, or a terminal item —
    on a credential-less box the launch errors, which still proves delivery
    ran), and only then asserts that no drop warning was ever logged for
    this session.

    :param live_server: Rig fixture; unused directly, referenced so the
        runner whose process log this test reads is this rig's.
    """
    del live_server
    create = http_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(tmp_path)})},
        files={"bundle": ("codex.tar.gz", _codex_spec_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    try:
        bind = http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": live_runner_id},
            timeout=60.0,
        )
        bind.raise_for_status()

        # The user picks Max in the session's effort control once the
        # session is live. The server persists it and live-forwards an
        # ``effort_change`` to the runner, which remembers it for the next
        # turn.
        patched = http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"reasoning_effort": "max"},
            timeout=120.0,
        )
        patched.raise_for_status()
        snap = http_client.get(f"/v1/sessions/{session_id}")
        snap.raise_for_status()
        persisted = snap.json().get("reasoning_effort")
        # Precondition, not the bug under test: the union vocabulary accepts
        # ``max`` at the session layer.
        assert persisted == "max", (
            f"session PATCH did not persist reasoning_effort='max' (got {persisted!r}); "
            "cannot exercise the per-turn delivery path"
        )
        # Precondition: the runner received the effort change, so the
        # remembered per-turn effort demonstrably exists before the send —
        # without this the delivery assertion below could pass vacuously.
        effort_received = _wait_for_runner_log_marker(
            f"conv={session_id} type=effort_change",
            timeout_s=60.0,
        )
        assert effort_received, (
            "the runner never logged receiving the effort_change for this "
            "session — rig failure (live-forward did not arrive), not the "
            "ladder gap under test"
        )

        # Raw event POST: a codex-native session may park the user message as
        # ``pending_...`` until the native terminal attaches (no ``item_id``
        # in the ack), so the conftest helper's item_id contract is too
        # strict here. Queued acceptance is all this journey needs — the
        # runner delivers the turn once the launch settles.
        send = http_client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Say OK."}],
                },
            },
            timeout=30.0,
        )
        assert send.status_code in (200, 202), f"send rejected: {send.status_code} {send.text}"

        drop_marker = f"conv={session_id} dropping reasoning effort"
        delivery_seen = ""
        deadline = time.monotonic() + _TURN_DELIVERY_TIMEOUT_S
        while time.monotonic() < deadline:
            if drop_marker in _runner_process_log_text():
                delivery_seen = "reasoning dropped"
                break
            items = http_client.get(f"/v1/sessions/{session_id}/items", params={"limit": 50})
            if items.status_code == 200:
                data = items.json().get("data", [])
                if any(item.get("type") == "error" for item in data) or any(
                    item.get("type") == "message" and item.get("role") == "assistant"
                    for item in data
                ):
                    delivery_seen = "turn reached a terminal item"
                    break
            session = http_client.get(f"/v1/sessions/{session_id}")
            if session.status_code == 200 and session.json().get("external_session_id"):
                delivery_seen = "codex thread started"
                break
            time.sleep(POLL_INTERVAL_S)

        # Give the runner's log handler a beat, then take the final read the
        # assertion judges — the drop (when it happens) is logged before the
        # harness call, so it is on disk by any of the outcomes above.
        time.sleep(1.0)
        log_text = _runner_process_log_text()
        drop_lines = [line for line in log_text.splitlines() if drop_marker in line]
        assert not drop_lines, (
            "the runner silently dropped the session's persisted "
            "reasoning_effort='max' on the codex-native turn (the per-turn "
            "half of the codex-native effort-ladder gap):\n  " + "\n  ".join(drop_lines[:3])
        )
        assert delivery_seen, (
            "the codex-native turn never demonstrably reached the runner's "
            f"delivery path within {_TURN_DELIVERY_TIMEOUT_S:.0f}s (no drop "
            "warning, no thread, no terminal item) — rig failure, not the "
            "ladder gap under test"
        )
    finally:
        http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
