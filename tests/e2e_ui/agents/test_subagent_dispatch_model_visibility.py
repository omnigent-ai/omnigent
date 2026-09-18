"""UI e2e: sub-agent dispatch records must expose each child's routed model.

Journey: an orchestrator fans out three worker sub-agents in one turn,
dispatching two on an Opus-class model and one on a Sonnet-class model. The
child sessions really run on those models (each child's page shows it, and
``model_override`` persists it), but the ``sys_session_send`` tool results the
orchestrator receives back carry no model attribution at all — so when the
user asks which model each sub-agent used, the orchestrator has nothing to
ground its answer in and misreports its own model for the whole fan-out.

The regression contract asserted here is that boundary: each
``sys_session_send`` tool result exposes the launched child's routed
``model``, matching that child's persisted ``model_override``.
"""

from __future__ import annotations

import ast
import io
import json
import re
import tarfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_ORCHESTRATOR_NAME = "model_reporter"
_OPUS_MODEL = "databricks-claude-opus-4-6"
_SONNET_MODEL = "databricks-claude-sonnet-4-6"
_WORKER_MODELS: dict[str, str] = {
    "worker_one": _OPUS_MODEL,
    "worker_two": _OPUS_MODEL,
    "worker_three": _SONNET_MODEL,
}
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_RELAY_TIMEOUT_MS = 240_000
_API_TIMEOUT_S = 120.0


def _orchestrator_yaml() -> str:
    workers = "\n".join(
        f"""\
  {name}:
    type: agent
    description: Worker sub-agent. Completes one small task when asked.
    executor:
      model: gpt-4o-mini
      harness: openai-agents
    prompt: |
      You are a worker. When asked to do a task, reply with a one-line
      completion report and nothing else.
"""
        for name in _WORKER_MODELS
    )
    return f"""\
name: {_ORCHESTRATOR_NAME}
prompt: |
  You coordinate three worker sub-agents: `worker_one`, `worker_two` and
  `worker_three`. When the user asks you to fan out work, dispatch one task
  to EACH worker via `sys_session_send`, picking a model per dispatch, then
  end your turn and wait for their results to arrive in your inbox. When the
  user asks which model each worker used, answer from your dispatch records.

executor:
  model: gpt-4o-mini
  harness: openai-agents

tools:
{workers}
"""


@dataclass(frozen=True)
class ModelFanoutSession:
    """Handle for the three-worker mixed-model fan-out session fixture.

    :param base_url: Spawned server base URL.
    :param session_id: The runner-bound parent session id.
    :param routing_token: Per-run token selecting the parent's mock queue.
    :param synth_code: Nonce in the parent's post-wake synthesis reply.
    :param call_ids: Dispatch tool-call id per worker name.
    """

    base_url: str
    session_id: str
    routing_token: str
    synth_code: str
    call_ids: dict[str, str]


@pytest.fixture
def model_fanout_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ModelFanoutSession]:
    """Create a runner-bound session for the mixed-model fan-out orchestrator.

    Same runner-respawn + bind contract as ``joke_subagents_session``; the
    parent's scripted queue dispatches each worker with an explicit
    per-dispatch ``args.model`` (two Opus, one Sonnet), and each worker
    answers from its own content-routed queue.

    :param live_server: Spawned server fixture from the parent conftest.
    :param mock_llm_server_url: Mock LLM server used by credential-free runs.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: A :class:`ModelFanoutSession` handle.
    """
    suffix = uuid.uuid4().hex[:10]
    routing_token = f"fanout-parent-{suffix}"
    worker_tokens = {name: f"fanout-{name}-{suffix}" for name in _WORKER_MODELS}
    synth_code = f"synth-{suffix}"
    call_ids = {name: f"call-{name}-{suffix}" for name in _WORKER_MODELS}

    dispatch_calls = [
        {
            "call_id": call_ids[name],
            "name": "sys_session_send",
            "arguments": json.dumps(
                {
                    "agent": name,
                    "title": f"{name}-task",
                    "args": {
                        "input": f"Do one small task. Routing marker: {worker_tokens[name]}",
                        "model": model,
                    },
                }
            ),
        }
        for name, model in _WORKER_MODELS.items()
    ]
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": dispatch_calls},
            {"text": "Dispatched all three workers; waiting for their results."},
            # Inbox wakes may arrive coalesced or split, so the synthesis
            # reply is queued once per child.
            {"text": f"All three workers reported back. Synthesis code: {synth_code}."},
            {"text": f"All three workers reported back. Synthesis code: {synth_code}."},
            {"text": f"All three workers reported back. Synthesis code: {synth_code}."},
        ],
        key=routing_token,
        match=routing_token,
    )
    for name in _WORKER_MODELS:
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": f"Task complete: {name} reporting in."}],
            key=worker_tokens[name],
            match=worker_tokens[name],
        )

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = _orchestrator_yaml().encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Non-config.yaml arcname routes the bundle through the omnigent
        # compat adapter, whose loader parses the inline `type: agent` tools.
        info = tarfile.TarInfo(name=f"{_ORCHESTRATOR_NAME}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield ModelFanoutSession(
            base_url=live_server,
            session_id=session_id,
            routing_token=routing_token,
            synth_code=synth_code,
            call_ids=call_ids,
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except Exception:
                respawned_runner.kill()


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _children_by_worker(base_url: str, parent_id: str) -> dict[str, str]:
    """Poll the parent's child sessions until all three workers exist.

    :returns: Mapping of worker name to child session id.
    """
    deadline = time.monotonic() + _API_TIMEOUT_S
    children: dict[str, str] = {}
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{parent_id}/child_sessions", timeout=10.0)
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        children = {
            str(row.get("tool")): str(row.get("session_id") or row.get("id"))
            for row in rows
            if row.get("tool") in _WORKER_MODELS
        }
        if len(children) == len(_WORKER_MODELS):
            return children
        time.sleep(1.0)
    raise AssertionError(
        f"expected {len(_WORKER_MODELS)} worker child sessions, got {sorted(children)}"
    )


def _persisted_model(base_url: str, child_id: str) -> str:
    resp = httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
    resp.raise_for_status()
    return str(resp.json().get("model_override") or "")


def _dispatch_result_texts(mock_url: str, call_ids: dict[str, str]) -> dict[str, str]:
    """Collect the ``sys_session_send`` tool-result text sent back to the brain.

    Scans the mock server's captured request bodies for
    ``function_call_output`` items matching the fixture's dispatch call ids —
    i.e. exactly the dispatch records the orchestrator model can see.

    :returns: Mapping of worker name to raw tool-result text.
    """
    wanted = {call_id: worker for worker, call_id in call_ids.items()}
    deadline = time.monotonic() + _API_TIMEOUT_S
    found: dict[str, str] = {}
    while time.monotonic() < deadline:
        resp = httpx.get(f"{mock_url}/mock/requests", timeout=10.0)
        resp.raise_for_status()
        for request in resp.json()["requests"]:
            items = request.get("input")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "function_call_output":
                    continue
                worker = wanted.get(str(item.get("call_id")))
                if worker is None:
                    continue
                output = item.get("output")
                if isinstance(output, list):
                    output = "".join(
                        str(part.get("text", "")) for part in output if isinstance(part, dict)
                    )
                found[worker] = str(output)
        if len(found) == len(call_ids):
            return found
        time.sleep(1.0)
    raise AssertionError(
        f"dispatch tool results never reached the orchestrator model; "
        f"saw results for {sorted(found)} of {sorted(call_ids)}"
    )


def _parse_handle(worker: str, raw: str) -> dict:
    """Parse a dispatch tool-result as a handle mapping.

    The harness may relay the handle as JSON or as a repr'd dict, so both
    are accepted.
    """
    for parse in (json.loads, ast.literal_eval):
        try:
            value = parse(raw)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, dict):
            return value
    pytest.fail(f"dispatch result for {worker} is not a handle mapping: {raw[:300]!r}")


# Nightly: full three-child dispatch + auto-wake UI journey; scripted LLM
# queues keep it deterministic while staying off the PR gate.
@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_subagent_dispatch_results_expose_routed_models(
    page: Page,
    model_fanout_session: ModelFanoutSession,
    mock_llm_server_url: str,
) -> None:
    chat = model_fanout_session
    page.goto(f"{chat.base_url}/c/{chat.session_id}")

    _send(
        page,
        "Fan out one small task to each of your three workers, choosing an "
        "appropriate model per dispatch, then report back once they all "
        f"finish. Routing marker: {chat.routing_token}",
    )
    expect(page.locator(_ASSISTANT, has_text="Dispatched all three workers").first).to_be_visible(
        timeout=_RELAY_TIMEOUT_MS
    )

    # The routing truth: each child session persists the model it was
    # dispatched on — two Opus workers, one Sonnet worker.
    children = _children_by_worker(chat.base_url, chat.session_id)
    persisted = {
        worker: _persisted_model(chat.base_url, child) for worker, child in children.items()
    }
    for worker, dispatched in _WORKER_MODELS.items():
        family = "opus" if dispatched == _OPUS_MODEL else "sonnet"
        assert family in persisted[worker].lower(), (
            f"{worker} was dispatched on {dispatched!r} but persisted "
            f"model_override={persisted[worker]!r}"
        )

    # The fan-out completes and wakes the orchestrator.
    expect(page.locator(_ASSISTANT, has_text=chat.synth_code).first).to_be_visible(
        timeout=_RELAY_TIMEOUT_MS
    )

    # The user can see a child's real model in the product: an Opus worker's
    # own session page shows an Opus model on its composer controls.
    opus_child = children["worker_one"]
    page.goto(f"{chat.base_url}/c/{opus_child}")
    model_value = page.locator('[data-testid$="-agent-model-value"]').first
    expect(model_value).to_be_visible(timeout=30_000)
    expect(model_value).to_contain_text(re.compile("opus", re.IGNORECASE), timeout=30_000)
    page.goto(f"{chat.base_url}/c/{chat.session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    # Expanding a dispatch card shows the user the record the orchestrator
    # got back for that worker.
    dispatch_cards = page.get_by_role("button").filter(has_text="session_send")
    if dispatch_cards.count() > 0:
        dispatch_cards.first.scroll_into_view_if_needed()
        dispatch_cards.first.click()
        expect(page.get_by_text("launching as task").first).to_be_visible(timeout=10_000)

    # The contract under test: the dispatch records the orchestrator model
    # received expose each child's routed model. Without this, the
    # orchestrator has no visibility into the fan-out's models and misreports
    # them when asked.
    results = _dispatch_result_texts(mock_llm_server_url, chat.call_ids)
    for worker, raw in results.items():
        handle = _parse_handle(worker, raw)
        assert handle.get("model") == persisted[worker], (
            f"dispatch record for {worker} does not expose its routed model "
            f"(child persisted model_override={persisted[worker]!r}); handle "
            f"fields={sorted(handle)}; handle={raw[:400]}"
        )
