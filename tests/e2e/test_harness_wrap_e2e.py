"""
End-to-end smoke test for the per-harness wraps with a real LLM
behind each, against the session-keyed harness API surface.

Parametrized across every harness wrap registered in
:data:`omnigent.harnesses.runtime._HARNESS_MODULES`. For each:
spawn a real harness subprocess via
:class:`HarnessProcessManager`, drive a turn via the session-keyed
``POST /v1/sessions/{conversation_id}/events`` endpoint with a
deterministic prompt, and verify the full round-trip works:

- The runner subprocess loads the wrap module
  (e.g. :mod:`omnigent.harnesses.claude_sdk.harness` or
  :mod:`omnigent.harnesses.codex.harness`).
- Reads its ``HARNESS_<HARNESS>_*`` env vars (per-spawn, per
  harness contract step 5a).
- Constructs a real inner Executor configured for the Databricks
  gateway against the user's profile.
- Routes the turn through the wrapped SDK + Databricks gateway.
- Streams ``response.output_text.delta`` events back.
- Closes with ``response.completed``.

This test scopes to the harness wrap (subprocess + scaffold)
directly — not the AP-level session lifecycle. The
``HarnessProcessManager`` spawns a per-conversation subprocess
whose scaffold owns exactly one conversation_id, validated via
``app.state.conversation_id``. No prior AP-level session
creation is needed, and the scaffold's
``POST /v1/sessions/{id}/events`` endpoint returns the SSE
stream directly as the HTTP response (no separate subscribe
hop, unlike the AP-level :mod:`omnigent.runtime.session_stream`
pub-sub).

Gated on ``--profile`` (the existing tests/conftest.py option).
Without it, the tests skip. Run with::

    .venv/bin/python -m pytest \\
        tests/e2e/test_harness_wrap_e2e.py \\
        --profile test-profile -v

Each harness's params are parametrized so the test ID surfaces
the harness in the test name (``[claude-sdk]`` / ``[codex]``)
and a per-harness assertion failure is visible without
re-reading the parametrize tuple.
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.runtime.process_manager import HarnessProcessManager
from tests.e2e._harness_probes import (
    HARNESS_IDS,
    HARNESS_PROBES,
    HarnessProbe,
    skip_if_harness_cli_missing,
)


@pytest.fixture
def databricks_profile(request: pytest.FixtureRequest) -> str:
    """
    Return the ``--profile`` CLI arg, or skip if not provided.

    Each harness wrap needs a real Databricks profile to route
    its inner executor through the gateway. Without one, there's
    no LLM backend, so the test is meaningless.
    """
    profile: str = request.config.getoption("--profile")
    if not profile:
        pytest.skip("harness wrap e2e requires --profile <name> (e.g. --profile test-profile)")
    return profile


@pytest.fixture
def short_tmp_parent() -> Iterator[Path]:
    """
    Per-test parent directory under /tmp with a short path.

    macOS ``AF_UNIX`` socket path limit is ~104 chars; pytest's
    default tmp_path exceeds that. Same pattern as the other
    HarnessProcessManager tests.
    """
    parent = Path("/tmp") / f"omni-cs-{uuid.uuid4().hex[:8]}"
    parent.mkdir(mode=0o700)
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


async def _consume_sse(
    response: httpx.Response,
    *,
    client: httpx.AsyncClient,
    conversation_id: str,
) -> list[dict[str, Any]]:
    """
    Drain an SSE streaming response into a list of decoded events.

    Each frame's ``data:`` line is JSON whose ``"type"`` mirrors the
    SSE event name, so callers match on the parsed dict alone.

    Also answers the harness policy round-trip: harness-backed
    executors park each turn on a ``policy_evaluation.requested`` event
    awaiting a ``policy_verdict`` (normally posted by the runner). This
    test talks to the scaffold directly with no runner, so we POST an
    ALLOW verdict ourselves; otherwise the turn hangs forever.

    :param response: An open streaming response.
    :param client: Harness client, used to POST policy verdicts.
    :param conversation_id: Conversation the scaffold is bound to.
    :returns: One decoded event dict per SSE frame, in order.
    """
    events: list[dict[str, Any]] = []
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")),
                None,
            )
            if data_line is None:
                continue
            try:
                event = json.loads(data_line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
            events.append(event)
            if event.get("type") == "policy_evaluation.requested":
                await client.post(
                    f"/v1/sessions/{conversation_id}/events",
                    json={
                        "type": "policy_verdict",
                        "evaluation_id": event["evaluation_id"],
                        "action": "POLICY_ACTION_ALLOW",
                    },
                )
    return events


@pytest.mark.parametrize("probe", HARNESS_PROBES, ids=HARNESS_IDS)
async def test_harness_wrap_real_llm_smoke(
    probe: HarnessProbe,
    databricks_profile: str,
    short_tmp_parent: Path,
) -> None:
    """End-to-end: real LLM via each harness wrap returns text.

    Verifies the full path for the parametrized harness:
    - HarnessProcessManager spawns the runner subprocess with
      per-spawn env (``HARNESS_<HARNESS>_GATEWAY=true``,
      ``HARNESS_<HARNESS>_DATABRICKS_PROFILE=<profile>``,
      ``HARNESS_<HARNESS>_MODEL=<model>``).
    - The runner imports the wrap module and calls ``create_app``.
    - ``create_app`` constructs a real inner Executor configured
      for the Databricks gateway.
    - ``POST /v1/sessions/{conv}/events`` with a ``message`` event
      streams ``response.output_text.delta`` events back.
    - The stream closes with ``response.completed``.

    The prompt asks the model to reply with an exact marker so
    we can deterministically detect a successful round-trip. If
    the marker is missing, either: the model responded but
    didn't follow the instruction (stochastic) or the wire path
    dropped/garbled the response (regression). Either way, the
    test surfaces the failure with the actual output in the
    assertion message.
    """
    skip_if_harness_cli_missing(probe.harness)
    conv_id = "conv_e2e"
    pm = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await pm.start()
    try:
        client = await pm.get_client(
            conv_id,
            probe.harness,
            env={
                # Enable-flag for the gateway transport. claude-sdk / codex /
                # pi read it; openai-agents and the supervisor have no flag and
                # ignore it (they route via the Databricks profile alone).
                f"{probe.env_prefix}GATEWAY": "true",
                f"{probe.env_prefix}DATABRICKS_PROFILE": databricks_profile,
                f"{probe.env_prefix}MODEL": probe.model,
            },
        )

        # Deterministic-ish prompt: asks the model to include an
        # exact marker. If the marker comes back, the full
        # round-trip works; if it doesn't, surface the actual
        # output for debugging.
        #
        # Wire shape: session-keyed ``MessageEvent`` body per
        # ``omnigent/harnesses/runtime/_scaffold.py``. The
        # outer ``type``/``role`` discriminate this as a fresh
        # downward user-side ``message`` event; ``content`` is
        # a list of input blocks the scaffold forwards to the
        # synthesized :class:`CreateResponseRequest`.
        body = {
            "type": "message",
            "role": "user",
            "model": f"{probe.harness}-e2e-agent",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        f"Reply with exactly the literal string {probe.marker} and nothing else."
                    ),
                }
            ],
        }

        async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
            assert response.status_code == 200, (
                f"[{probe.harness}] /v1/sessions/{conv_id}/events returned "
                f"{response.status_code}, expected 200. The scaffold either "
                f"rejected the MessageEvent body shape (422) or the "
                f"conversation_id binding mismatched (404)."
            )
            events = await _consume_sse(response, client=client, conversation_id=conv_id)

        # Must see the canonical envelope events.
        event_types = [e.get("type") for e in events]
        # ``response.created`` is always first per the scaffold
        # contract (``_initial_envelope_events``). If absent,
        # the scaffold's start-of-turn envelope is broken on
        # this harness.
        assert "response.created" in event_types, (
            f"[{probe.harness}] missing response.created; saw: {event_types}"
        )
        # ``response.completed`` is the terminal event emitted
        # by ``_build_terminal_event``. Its absence means the
        # turn either crashed (response.failed) or the stream
        # was truncated before completion.
        assert "response.completed" in event_types, (
            f"[{probe.harness}] missing response.completed; saw: {event_types}"
        )

        # Must see at least one text delta. The delta carries
        # the model output through the scaffold's
        # output_text.delta translation; absence means the
        # adapter never received text events from the inner
        # executor (likely a SDK-side regression in the wrap).
        text_deltas = [
            e.get("delta", "") for e in events if e.get("type") == "response.output_text.delta"
        ]
        full_text = "".join(text_deltas)
        assert full_text, (
            f"[{probe.harness}] no response.output_text.delta events with "
            f"non-empty delta; the harness wrap never streamed text. "
            f"All event types: {event_types}"
        )
        # The marker proves the full path: client → harness
        # subprocess → inner Executor → Databricks gateway →
        # model → SDK events → adapter → scaffold SSE → us. If
        # the marker is missing, surface the full text so a
        # flake is debuggable.
        assert probe.marker in full_text, (
            f"[{probe.harness}] marker {probe.marker!r} not found in "
            f"response; full text: {full_text!r}"
        )
    finally:
        await pm.shutdown()
