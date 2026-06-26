"""
Tests for :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`.

End-to-end through real subprocesses spawned via the same
:class:`HarnessProcessManager` used in production. Uses
:class:`omnigent.inner.executor.MockExecutor` as the inner
executor — no real LLM SDK required. The adapter's per-event
translation contract is what's under test; per-harness
configuration (Claude SDK CLI discovery, Codex subprocess setup,
Databricks credential resolution) is the per-wrap concern tested
elsewhere.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

_TEST_HARNESS_NAME = "executor_adapter_fixture"
_TEST_HARNESS_MODULE = "tests.runtime.harnesses._test_executor_adapter_harness"


def _start_turn_body() -> dict[str, Any]:
    """Return a minimal session-keyed ``message`` event body that starts a turn.

    Used by adapter tests that just want a fresh turn to drive
    the inner mock executor — no pre-existing conversation
    history needed. Returns a fresh dict per call so a test that
    mutates the body doesn't bleed into the next test.

    :returns: A discriminated ``message`` event body suitable for
        ``POST /v1/sessions/{conversation_id}/events``.
    """
    return {"type": "message", "role": "user", "model": "test-agent", "content": []}


@dataclass
class _ParsedSSEEvent:
    """
    Single parsed SSE event captured from a streaming response.

    :param event: The SSE event name.
    :param data: The JSON-decoded payload.
    """

    event: str
    data: dict[str, Any]


async def _stream_iter(
    response: httpx.Response,
) -> AsyncIterator[_ParsedSSEEvent]:
    """
    Yield parsed SSE events from an open streaming response.

    :param response: An open streaming response.
    :yields: Parsed events one by one.
    """
    buffer = ""
    async for chunk in response.aiter_text():
        buffer += chunk
        while "\n\n" in buffer:
            frame, _, buffer = buffer.partition("\n\n")
            event_line = next(
                (line for line in frame.splitlines() if line.startswith("event:")),
                None,
            )
            data_line = next(
                (line for line in frame.splitlines() if line.startswith("data:")),
                None,
            )
            if event_line is None or data_line is None:
                continue
            event_name = event_line[len("event:") :].strip()
            data_payload = json.loads(data_line[len("data:") :].strip())
            yield _ParsedSSEEvent(event=event_name, data=data_payload)


@pytest.fixture
def register_fixture_harness() -> Iterator[None]:
    """Register the inner-adapter fixture harness for the test."""
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = _TEST_HARNESS_MODULE
    try:
        yield
    finally:
        _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)


@pytest.fixture
def short_tmp_parent() -> Iterator[Path]:
    """Per-test parent directory under /tmp with a short path."""
    parent = Path("/tmp") / f"omni-ia-{uuid.uuid4().hex[:8]}"
    parent.mkdir(mode=0o700)
    try:
        yield parent
    finally:
        shutil.rmtree(parent, ignore_errors=True)


@pytest.fixture
async def manager(
    short_tmp_parent: Path,
    register_fixture_harness: None,
) -> AsyncIterator[HarnessProcessManager]:
    """A started manager rooted in a short tmp dir."""
    mgr = HarnessProcessManager(
        idle_timeout_s=60.0,
        reaper_interval_s=60.0,
        tmp_parent=short_tmp_parent,
    )
    await mgr.start()
    try:
        yield mgr
    finally:
        await mgr.shutdown()


# ── Per-mock-script selectors ──────────────────────────────────
#
# Each fixture below sets the ``MOCK_EXECUTOR_SCRIPT`` env var
# the runner subprocess reads in
# ``tests/runtime/harnesses/_test_executor_adapter_harness.py:create_app``
# to populate the MockExecutor with a particular script.


@pytest.fixture
def use_text_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """MockExecutor that responds with a single text turn."""
    monkeypatch.setenv("MOCK_EXECUTOR_SCRIPT", "text_only")


@pytest.fixture
def use_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """MockExecutor that yields a tool-call observation then completes."""
    monkeypatch.setenv("MOCK_EXECUTOR_SCRIPT", "tool_call")


@pytest.fixture
def use_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """MockExecutor that yields an ExecutorError."""
    monkeypatch.setenv("MOCK_EXECUTOR_SCRIPT", "error")


@pytest.fixture
def use_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """MockExecutor that yields a provider-side TurnCancelled."""
    monkeypatch.setenv("MOCK_EXECUTOR_SCRIPT", "cancelled")


@pytest.fixture
def use_capture_messages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Capturing executor: records the messages it received as JSON.

    :returns: The path the executor will write to. Test reads
        this file after the request completes to inspect the
        messages list the inner executor received.
    """
    capture_path = tmp_path / "captured_messages.json"
    monkeypatch.setenv("MOCK_EXECUTOR_SCRIPT", "capture_messages")
    monkeypatch.setenv("MOCK_EXECUTOR_CAPTURE_PATH", str(capture_path))
    return capture_path


# ── Tests ──────────────────────────────────────────────────────


async def test_text_chunk_translates_to_output_text_delta(
    use_text_only: None,
    manager: HarnessProcessManager,
) -> None:
    """A TextChunk from the inner executor → response.output_text.delta.

    Verifies the basic text-streaming translation path. The mock
    script yields a single TurnComplete with response="hello from
    mock" — the adapter should surface this as a single
    text-delta event followed by response.completed.
    """
    conv_id = "conv_text"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    events: list[_ParsedSSEEvent] = []
    async with client.stream(
        "POST", f"/v1/sessions/{conv_id}/events", json=_start_turn_body()
    ) as response:
        async for event in _stream_iter(response):
            events.append(event)

    # Expected sequence: created + in_progress envelope + the
    # turn body + terminal response.completed.
    event_names = [e.event for e in events]
    # Initial envelope is constant across all adapter tests.
    assert event_names[:2] == ["response.created", "response.in_progress"]
    # Stream completes.
    assert event_names[-1] == "response.completed"
    # The mock executor's text content shows up SOMEWHERE in the
    # stream — either as TextChunk → output_text.delta (if we
    # script TextChunks) or via the TurnComplete.response path
    # (if the script uses TurnComplete with text). The current
    # text_only script uses TurnComplete; the adapter doesn't
    # emit deltas for that today (see _translate_event), so we
    # verify the stream completes cleanly without that text.
    # If a future change makes TurnComplete.response emit a
    # delta, this test should be updated.
    assert "response.completed" in event_names


async def test_tool_call_translates_to_paired_function_call_items(
    use_tool_call: None,
    manager: HarnessProcessManager,
) -> None:
    """ToolCallRequest+Complete → paired function_call + function_call_output.

    Verifies the v1 native-tool emission pattern: tools the
    inner SDK already executed surface as paired observed items
    (status=completed), NOT as action_required (which is
    server-dispatched).
    """
    conv_id = "conv_tool"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    events: list[_ParsedSSEEvent] = []
    async with client.stream(
        "POST", f"/v1/sessions/{conv_id}/events", json=_start_turn_body()
    ) as response:
        async for event in _stream_iter(response):
            events.append(event)

    # Expected: created, in_progress, function_call (observed),
    # function_call_output (observed), completed.
    output_items = [e for e in events if e.event == "response.output_item.done"]
    # The mock yields ToolCallRequest + ToolCallComplete +
    # TurnComplete. The ToolCallComplete carries NO call_id in its
    # metadata — it models a ``handles_tools_internally`` executor
    # (e.g. antigravity) whose tool was run ENTIRELY inside the
    # SDK and never round-tripped through ``_stable_tool_executor`` /
    # ctx.dispatch_tool. The adapter must therefore emit BOTH the
    # observed function_call AND its paired function_call_output —
    # the inner completion is the ONLY output source for these tools.
    # (Suppression is scoped to call ids in ``_dispatched_call_ids``,
    # populated only by ``_stable_tool_executor``; an internal tool's
    # id is never added, so its completion is not suppressed. The
    # prior blanket ``_current_ctx is not None`` rule wrongly dropped
    # it, leaving such calls as perpetual in_progress with no output.)
    fc_items = [e for e in output_items if e.data["item"].get("type") == "function_call"]
    assert len(fc_items) >= 1, (
        f"expected at least 1 function_call; got {len(fc_items)}: "
        f"{[e.data['item'].get('type') for e in output_items]}"
    )
    fc = fc_items[0].data["item"]
    assert fc["type"] == "function_call"
    # The adapter emits tool calls as in_progress initially; the
    # scaffold upgrades to completed when the tool resolves.
    assert fc["status"] in ("completed", "in_progress")
    assert fc["name"] == "echo_tool"
    # The paired function_call_output is emitted for the internally-run
    # tool (this is the FIX-1b behavior — without it the tool renders
    # as a perpetual in_progress card).
    fco_items = [e for e in output_items if e.data["item"].get("type") == "function_call_output"]
    assert len(fco_items) >= 1, (
        "expected the inner ToolCallComplete to surface a "
        "function_call_output for a tool run internally by the SDK "
        f"(no dispatch round-trip); got item types "
        f"{[e.data['item'].get('type') for e in output_items]}"
    )
    assert "tool result" in str(fco_items[0].data["item"].get("output", ""))
    # Stream completes cleanly.
    assert events[-1].event == "response.completed"


async def test_full_history_roundtrips_to_inner_executor(
    use_capture_messages: Path,
    manager: HarnessProcessManager,
) -> None:
    """End-to-end proof of the resume-history fix.

    The user's reported regression was: ``--resume`` follow-up
    turns ("list those backwards") came back as "What?" because
    the harness boundary was stripping conversation history
    down to the latest user message — the inner SDK started
    a fresh session with no prior context.

    This test sends a request body whose ``input`` carries
    THREE turns of role-keyed message items (the wire shape
    :func:`_translate_messages_to_input` now produces from
    AP's full Layer 2 history). The harness's
    :class:`ExecutorAdapter` decodes them via
    :func:`_translate_input_to_messages` and forwards to the
    inner executor. The :class:`_CapturingExecutor` writes
    every received message to disk, and we assert the captured
    file shows all three turns.

    If this test ever fails with ``len(captured) == 1``, the
    AP→harness→inner-executor pipeline has regressed to
    "latest user only" and the user-facing ``--resume``
    follow-up bug is back.
    """
    conv_id = "conv_history"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    # ``MessageEvent.content`` carries the full role-keyed item
    # list verbatim (it's renamed to ``input`` by
    # :meth:`MessageEvent.to_create_request` before reaching the
    # adapter); the outer envelope's ``role`` is the discriminator
    # for the downward direction and is unrelated to per-item
    # roles inside ``content``.
    body = {
        "type": "message",
        "role": "user",
        "model": "test-agent",
        "content": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "what tools you got?"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Bash, Edit, Read."}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "list those backwards"}],
            },
        ],
    }
    events: list[_ParsedSSEEvent] = []
    async with client.stream("POST", f"/v1/sessions/{conv_id}/events", json=body) as response:
        async for event in _stream_iter(response):
            events.append(event)
    # Sanity: stream completes cleanly. If this fails, the
    # adapter raised on the new role-keyed input shape and
    # the captured-file check below would be moot.
    assert events[-1].event == "response.completed"

    # The capture file is written synchronously inside the
    # inner executor's run_turn before it yields TurnComplete,
    # so once response.completed arrives it must exist.
    assert use_capture_messages.exists(), (
        f"Capture file {use_capture_messages} was not created — "
        f"the inner executor's run_turn never reached the "
        f"json.dump call. Likely the adapter raised on the "
        f"input shape before forwarding to the executor."
    )
    captured = json.loads(use_capture_messages.read_text())

    # Three messages survived end-to-end. If 1, the harness
    # boundary stripped history again and ``--resume``
    # follow-ups are broken.
    assert len(captured) == 3, (
        f"Expected 3 messages reconstructed from the "
        f"role-keyed input shape, got {len(captured)}: "
        f"{captured!r}. If 1, the AP→harness→inner-executor "
        f"pipeline has regressed to 'latest user only' and "
        f"--resume follow-up turns will lose prior context."
    )
    assert [m["role"] for m in captured] == ["user", "assistant", "user"]
    assert captured[0]["content"] == "what tools you got?"
    assert captured[1]["content"] == "Bash, Edit, Read."
    assert captured[2]["content"] == "list those backwards"


async def test_executor_error_terminates_with_response_failed(
    use_error: None,
    manager: HarnessProcessManager,
) -> None:
    """ExecutorError → response.failed terminal event.

    Verifies the adapter raises on ExecutorError so the
    scaffold's terminal-event path produces response.failed
    (instead of response.completed). The error message surfaces
    on the ResponseObject's ``error`` field.
    """
    conv_id = "conv_err"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    events: list[_ParsedSSEEvent] = []
    async with client.stream(
        "POST", f"/v1/sessions/{conv_id}/events", json=_start_turn_body()
    ) as response:
        async for event in _stream_iter(response):
            events.append(event)

    # Terminal event is response.failed (NOT completed/cancelled).
    # If the adapter swallowed the error, this assertion catches
    # it — the stream would terminate cleanly with completed.
    assert events[-1].event == "response.failed"
    error_detail = events[-1].data["response"]["error"]
    assert error_detail is not None
    # The mock script's error message ("mock error") propagates
    # via the RuntimeError wrap in the adapter; the scaffold
    # builds an ErrorDetail with the exception's str().
    assert "mock error" in error_detail["message"]


async def test_turn_cancelled_terminates_with_response_cancelled(
    use_cancelled: None,
    manager: HarnessProcessManager,
) -> None:
    """TurnCancelled -> response.cancelled terminal event.

    Provider-side cancellation is not driven by an inbound interrupt event, so
    the adapter must mark the turn context cancelled itself. Otherwise the
    scaffold sees a clean return and emits response.completed.
    """
    conv_id = "conv_cancelled"
    client = await manager.get_client(conv_id, _TEST_HARNESS_NAME)
    events: list[_ParsedSSEEvent] = []
    async with client.stream(
        "POST", f"/v1/sessions/{conv_id}/events", json=_start_turn_body()
    ) as response:
        async for event in _stream_iter(response):
            events.append(event)

    assert events[-1].event == "response.cancelled"
    terminal_response = events[-1].data["response"]
    assert terminal_response["status"] == "cancelled"
    assert terminal_response.get("error") is None


# ── Error-code classification ──────────────────────────────────


def test_build_error_detail_uses_omnigent_error_code() -> None:
    """
    :class:`OmnigentError` (and its
    :class:`RetryableLLMError` / :class:`PermanentLLMError`
    subclasses) carry a semantic ``code`` field; the adapter's
    override uses it verbatim instead of the exception class
    name.

    What breaks if this fails: a ``RetryableLLMError(code="timeout")``
    raised by the inner executor would surface as
    ``code="RetryableLLMError"``, AP's allowlist wouldn't
    match, and the workflow's retry policy would treat the
    timeout as permanent. The whole point of step 5j is the
    structured ``code + retryable`` flowing through.
    """
    from omnigent.llms.errors import LLMErrorDetail, RetryableLLMError
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import HarnessApp

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    error = RetryableLLMError(
        "rate-limited by gateway",
        code="rate_limit_exceeded",
        detail=LLMErrorDetail(provider="anthropic", status_code=429),
    )
    detail = adapter._build_error_detail(error)

    # Code preserved verbatim — the Omnigent allowlist matches this and
    # marks the failure retryable. Class-name fallback (which the
    # base HarnessApp implementation would have used) gives
    # ``"RetryableLLMError"`` instead, which Omnigent would NOT match.
    assert detail.code == "rate_limit_exceeded"
    assert "rate-limited by gateway" in detail.message
    # Sanity: the base class fallback would NOT have produced this
    # code. If the override stops calling .code and falls through,
    # this assertion would fail.
    base_detail = HarnessApp._build_error_detail(adapter, RuntimeError("oops"))
    assert base_detail.code == "RuntimeError"


def test_classify_openai_exception_maps_known_types() -> None:
    """
    The OpenAI SDK classifier maps each recognized exception
    type onto its allowlist code. Unknown OpenAI exceptions
    return ``None`` so the caller falls through to the base
    implementation (preserves the class name in logs).

    What breaks if this fails: a known retryable failure like
    ``openai.RateLimitError`` would not be classified
    retryable; the workflow would not retry through what is in
    fact a transient gateway hiccup. ``openai`` is in the venv
    — the openai-agents / open-responses inner executors depend
    on it directly.
    """
    import openai

    from omnigent.runtime.harnesses._executor_adapter import (
        _classify_openai_exception,
    )

    # Construct each exception with a minimal body matching the
    # SDK's __init__ signature. The OpenAI SDK's APIError
    # subclasses take ``(message, request, body)``; we build a
    # minimal ``httpx.Request`` so the class doesn't reject the
    # args.
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")

    rate = openai.RateLimitError(
        "rate limit",
        response=httpx.Response(429, request=request),
        body=None,
    )
    timeout = openai.APITimeoutError(request=request)
    connect = openai.APIConnectionError(message="conn", request=request)
    server = openai.InternalServerError(
        "server",
        response=httpx.Response(500, request=request),
        body=None,
    )

    # Each known type maps onto AP's allowlist verbatim.
    assert _classify_openai_exception(rate) == "rate_limit_exceeded"
    assert _classify_openai_exception(timeout) == "timeout"
    assert _classify_openai_exception(connect) == "connection_error"
    assert _classify_openai_exception(server) == "server_error"

    # An unrelated exception type returns None — the classifier
    # only handles OpenAI SDK exceptions, so callers know to
    # fall through.
    assert _classify_openai_exception(RuntimeError("nope")) is None


def test_classify_openai_exception_context_length_exceeded_direct() -> None:
    """
    A direct ``BadRequestError`` with ``code='context_length_exceeded'``
    is classified so the harness wire carries the code and the workflow's
    reactive compaction fires.

    What breaks if this fails: context overflow from the openai-agents
    executor is misclassified as a generic permanent error and compaction
    never triggers.
    """
    import openai

    from omnigent.runtime.harnesses._executor_adapter import (
        _classify_openai_exception,
    )

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    exc = openai.BadRequestError(
        "context length exceeded",
        response=httpx.Response(400, request=request),
        body={"error": {"code": "context_length_exceeded", "message": "too long"}},
    )
    assert _classify_openai_exception(exc) == "context_length_exceeded"


def test_classify_openai_exception_context_length_exceeded_wrapped() -> None:
    """
    When the openai-agents SDK wraps a ``BadRequestError`` as
    ``__cause__`` of a generic ``Exception``, the classifier walks
    the cause chain and still returns ``"context_length_exceeded"``.

    What breaks if this fails: the common case where the agents SDK
    wraps provider errors is not classified and compaction never fires.
    """
    import openai

    from omnigent.runtime.harnesses._executor_adapter import (
        _classify_openai_exception,
    )

    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    inner = openai.BadRequestError(
        "context length exceeded",
        response=httpx.Response(400, request=request),
        body={"error": {"code": "context_length_exceeded", "message": "too long"}},
    )
    wrapper = RuntimeError("agents SDK error")
    wrapper.__cause__ = inner
    assert _classify_openai_exception(wrapper) == "context_length_exceeded"


def test_classify_claude_sdk_exception_maps_connection_error() -> None:
    """
    The :mod:`claude_agent_sdk` classifier maps
    ``CLIConnectionError`` onto ``"connection_error"`` so the
    AP-side retry loop treats subprocess connection drops as
    transient. Other claude_agent_sdk exceptions
    (``CLINotFoundError``, ``CLIJSONDecodeError``,
    ``ProcessError``) are non-retryable and fall through.

    What breaks if this fails: a transient subprocess hiccup
    (e.g. CLI restarting) would surface as a permanent failure
    and the workflow would never retry. ``claude_agent_sdk`` is
    in the venv — the claude-sdk harness depends on it.
    """
    import claude_agent_sdk

    from omnigent.runtime.harnesses._executor_adapter import (
        _classify_claude_sdk_exception,
    )

    conn_err = claude_agent_sdk.CLIConnectionError("subprocess gone")
    not_found = claude_agent_sdk.CLINotFoundError("CLI not on PATH")

    # Connection error → retryable.
    assert _classify_claude_sdk_exception(conn_err) == "connection_error"
    # Not-found is non-retryable; the classifier returns None
    # so the base implementation surfaces the class name.
    assert _classify_claude_sdk_exception(not_found) is None
    # Non-claude-sdk exception → None.
    assert _classify_claude_sdk_exception(RuntimeError("nope")) is None


def test_classify_httpx_exception_maps_timeout_and_connect() -> None:
    """
    The httpx classifier handles raw transport-layer exceptions
    that some inner executors surface unwrapped (notably
    litellm-backed paths for non-Anthropic providers).

    What breaks if this fails: a turn that times out at the
    httpx layer (rather than inside the SDK's wrapper) would
    not be retryable.
    """
    from omnigent.runtime.harnesses._executor_adapter import (
        _classify_httpx_exception,
    )

    timeout = httpx.ConnectTimeout("timed out")
    connect = httpx.ConnectError("conn refused")
    other = httpx.ProtocolError("bad chunk")

    assert _classify_httpx_exception(timeout) == "timeout"
    assert _classify_httpx_exception(connect) == "connection_error"
    # Other httpx exceptions fall through to the caller — we only
    # claim retryability for the two we explicitly mapped.
    assert _classify_httpx_exception(other) is None
    # Non-httpx exception → None.
    assert _classify_httpx_exception(RuntimeError("nope")) is None


def test_classify_anthropic_exception_returns_none_when_sdk_not_installed() -> None:
    """
    Without :mod:`anthropic` installed, the classifier returns
    ``None`` rather than raising.

    Regression: a hard-import of :mod:`anthropic` would crash
    the ``ExecutorAdapter._build_error_detail`` path on every
    error in environments that don't ship the SDK (e.g. the
    openai-agents wrap deployment).
    """
    from omnigent.runtime.harnesses._executor_adapter import (
        _classify_anthropic_exception,
    )

    # ``anthropic`` is not in the venv; the lazy ImportError
    # path returns None silently.
    assert _classify_anthropic_exception(RuntimeError("nope")) is None

    # Even an exception that LOOKS Anthropic-shaped (a
    # ``RateLimitError`` named identically from a different
    # module) returns None — we only claim ours.
    class _FakeRateLimitError(Exception):
        pass

    assert _classify_anthropic_exception(_FakeRateLimitError()) is None


def test_classify_anthropic_exception_maps_known_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With :mod:`anthropic` available, the classifier maps each
    recognized exception type onto its allowlist code.

    The :mod:`anthropic` SDK isn't a hard dep of the venv, so
    we synthesize a stub module with the exception classes the
    classifier checks against. ``isinstance`` against a stub
    class works as long as the classifier's lazy import
    resolves to the same module — which is what the
    monkeypatch arranges.

    Regression: Phase 1d/1e wires
    ``ANTHROPIC_MAX_RETRIES`` into the Claude CLI subprocess,
    which can still surface raw :class:`anthropic.RateLimitError`
    upward when the SDK's framing layer fails. Without this
    classifier, those would render as ``[llm] RateLimitError``
    and AP's retry allowlist (which uses semantic codes, not
    class names) wouldn't match — silent demotion of retryable
    failures to permanent.
    """
    import sys
    import types

    # Synthesize a stub ``anthropic`` module with the four
    # exception classes the classifier checks. Each stub class
    # subclasses ``Exception`` so ``isinstance(stub_inst, cls)``
    # works the way the SDK's classes would in production.
    fake_anthropic = types.ModuleType("anthropic")

    class _RateLimit(Exception):
        pass

    class _Timeout(Exception):
        pass

    class _Connection(Exception):
        pass

    class _InternalServer(Exception):
        pass

    fake_anthropic.RateLimitError = _RateLimit  # type: ignore[attr-defined]
    fake_anthropic.APITimeoutError = _Timeout  # type: ignore[attr-defined]
    fake_anthropic.APIConnectionError = _Connection  # type: ignore[attr-defined]
    fake_anthropic.InternalServerError = _InternalServer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    from omnigent.runtime.harnesses._executor_adapter import (
        _classify_anthropic_exception,
    )

    assert _classify_anthropic_exception(_RateLimit("limit")) == "rate_limit_exceeded"
    assert _classify_anthropic_exception(_Timeout("slow")) == "timeout"
    assert _classify_anthropic_exception(_Connection("conn")) == "connection_error"
    assert _classify_anthropic_exception(_InternalServer("500")) == "server_error"

    # Unrelated exception → None even with anthropic available.
    assert _classify_anthropic_exception(RuntimeError("nope")) is None


def test_classify_inner_exception_dispatches_across_sdks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The consolidated :func:`classify_inner_exception` entry
    point fans out across all per-SDK classifiers and returns
    the first match.

    Pin: Phase 3 of ``designs/RETRY_ACROSS_HARNESSES.md`` —
    callers (``ExecutorAdapter._build_error_detail``) used to
    inline three separate classifier calls; this test ensures
    the consolidated function preserves that semantics
    (first-match-wins) and adds the Anthropic path.

    Verifies via real exceptions where possible (openai +
    httpx are in the venv) and a synthesized stub for
    anthropic. ``RuntimeError`` confirms the fall-through to
    ``None``.
    """
    import sys
    import types

    import openai

    from omnigent.runtime.harnesses._executor_adapter import (
        classify_inner_exception,
    )

    # OpenAI SDK path — real exceptions.
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    rate = openai.RateLimitError(
        "rate limit",
        response=httpx.Response(429, request=request),
        body=None,
    )
    assert classify_inner_exception(rate) == "rate_limit_exceeded"

    # httpx path — real exceptions.
    connect = httpx.ConnectError("conn refused")
    assert classify_inner_exception(connect) == "connection_error"

    # claude_agent_sdk path — real exception.
    import claude_agent_sdk

    cli_err = claude_agent_sdk.CLIConnectionError("subprocess gone")
    assert classify_inner_exception(cli_err) == "connection_error"

    # Anthropic path — synthesized stub since the SDK isn't a
    # hard venv dep.
    fake_anthropic = types.ModuleType("anthropic")

    class _RateLimit(Exception):
        pass

    class _Timeout(Exception):
        pass

    class _Connection(Exception):
        pass

    class _InternalServer(Exception):
        pass

    fake_anthropic.RateLimitError = _RateLimit  # type: ignore[attr-defined]
    fake_anthropic.APITimeoutError = _Timeout  # type: ignore[attr-defined]
    fake_anthropic.APIConnectionError = _Connection  # type: ignore[attr-defined]
    fake_anthropic.InternalServerError = _InternalServer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    assert classify_inner_exception(_Timeout("slow")) == "timeout"

    # Fall-through: an exception no classifier recognizes
    # returns None. Caller is expected to use the class name.
    assert classify_inner_exception(RuntimeError("unknown")) is None


class _StubExecutor(Executor):
    """
    Minimal :class:`Executor` stub — just enough for ExecutorAdapter
    construction in unit tests that don't actually drive a turn.
    The override-method tests above never call ``run_turn``; they
    just need a constructed adapter to invoke
    ``_build_error_detail`` on.

    Subclasses :class:`Executor` so ``executor_factory=lambda: _StubExecutor()``
    typechecks (the factory expects ``Callable[[], Executor]``); the base
    provides the no-op ``run_turn`` / capability defaults these tests rely on.
    """


def test_translate_input_to_messages_reconstructs_full_history() -> None:
    """
    Role-keyed message items in ``input`` must round-trip back
    into the inner Message list.

    Mirror of
    ``test_translate_messages_to_input_passes_full_history``
    in test_client_executor.py — together they pin the
    AP→harness→inner-executor history pipeline. If this test
    only sees the latest message, the resume regression is
    back: the inner SDK gets a fresh turn with no prior
    context and answers "What?" the way the user reported
    against ``--resume``.
    """
    from omnigent.runtime.harnesses._executor_adapter import (
        _translate_input_to_messages,
    )

    input_value = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "What tools you got?"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Bash, Edit, Read."}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "List those backwards."}],
        },
    ]

    messages = _translate_input_to_messages(input_value)

    # Three messages survive, in order — the inner SDK's
    # ``_build_prompt`` will serialize them as "Conversation so
    # far:" when starting a fresh session, giving the LLM the
    # context it needs to answer the latest user turn.
    assert len(messages) == 3, (
        f"Expected 3 messages reconstructed from role-keyed input "
        f"items, got {len(messages)}: {messages!r}. If 1, the "
        f"adapter has regressed to its old single-user-message "
        f"flatten and resume context will be lost."
    )
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "What tools you got?"
    assert messages[1]["content"] == "Bash, Edit, Read."
    assert messages[2]["content"] == "List those backwards."


def test_translate_input_to_messages_string_input_fallback() -> None:
    """
    Plain-string ``input`` → single user message.

    Backwards-compat fallback for any caller that still sends
    the original shape (a bare string is the Omnigent API's shorthand
    for a single ``input_text`` block from the user). One
    user-role :class:`Message` so the inner executor's
    single-turn path keeps working.
    """
    from omnigent.runtime.harnesses._executor_adapter import (
        _translate_input_to_messages,
    )

    messages = _translate_input_to_messages("hello")

    assert messages == [{"role": "user", "content": "hello"}]


def test_translate_input_to_messages_legacy_content_blocks_fallback() -> None:
    """
    Bare content-block list (no role wrappers) → single user message.

    The pre-history-fix wire format. Omnigent clients that haven't
    been migrated still send this, and the harness must keep
    handling it the same way (concat all ``text`` fields into
    a single user message). This test pins the fallback so a
    future cleanup doesn't accidentally drop bare-block
    callers.
    """
    from omnigent.runtime.harnesses._executor_adapter import (
        _translate_input_to_messages,
    )

    input_value = [
        {"type": "input_text", "text": "Hello"},
        {"type": "input_text", "text": "world"},
    ]

    messages = _translate_input_to_messages(input_value)

    # Single user message with the texts concatenated by newline
    # — same shape the harness produced before history-shape
    # support was added.
    assert messages == [{"role": "user", "content": "Hello\nworld"}]


def test_translate_input_to_messages_drops_empty_message_blocks() -> None:
    """
    A message item whose content has no text (e.g. an
    assistant turn that produced only tool calls) is dropped
    rather than emitted as ``{"role": "assistant", "content": ""}``.

    Empty messages confuse the inner SDK's prompt builder and
    don't carry useful context for the LLM (the prior user
    turn already encodes the question, the next user turn
    encodes the follow-up). Skipping them keeps the
    serialized "Conversation so far:" prefix focused on the
    parts the LLM actually benefits from.
    """
    from omnigent.runtime.harnesses._executor_adapter import (
        _translate_input_to_messages,
    )

    input_value = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "do the thing"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [],  # Tool-only assistant turn — no text.
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "did it work?"}],
        },
    ]

    messages = _translate_input_to_messages(input_value)

    # Two messages, not three: the empty assistant turn is
    # dropped. If three messages come back, the inner SDK
    # would see a confusing empty assistant entry between two
    # user turns.
    assert len(messages) == 2, (
        f"Expected 2 messages (empty assistant skipped), got {len(messages)}: {messages!r}."
    )
    assert [m["role"] for m in messages] == ["user", "user"]


# ── MCP tool-call observed/dispatch correlation ──────────
#
# These unit tests pin the queue mechanic that fixes the tool-call
# correlation gap.
# The adapter's ``_translate_event`` queues each MCP-prefixed
# ToolCallRequest's ``tool_use_id``; ``_stable_tool_executor``
# pops in FIFO order so the eventual dispatch_tool call_id
# matches the inline observed event's call_id. Without that
# correlation, the SDK client's BlockStream can't dedupe and
# the REPL renders ``⏵ tool_name`` twice.


class _RecordingTurnContext:
    """
    Stand-in for :class:`TurnContext` that records every emit.

    Why a real stub class instead of MagicMock: per the
    project's testing rules, MagicMock would silently return
    MagicMock for any attribute access — if the adapter
    started calling a non-existent method on ctx, the test
    would still pass. A typed stub fails loud.

    Only the surface the adapter touches is implemented.
    """

    def __init__(self, response_id: str = "resp_xyz") -> None:
        """Initialize recording state.

        :param response_id: Identifier exposed via the matching
            attribute, e.g. ``"resp_xyz"``. Mirrors the
            real ``TurnContext.response_id``; the adapter uses
            it as the ``agent`` field on emitted function_call
            items.
        """
        self.response_id = response_id
        self.emitted: list[Any] = []

    def emit(self, event: Any) -> None:
        """Record an emitted event.

        :param event: The event the adapter produced (e.g.
            :class:`OutputItemDoneEvent`).
        """
        self.emitted.append(event)


def test_translate_event_mcp_tool_call_request_emits_observed_with_bare_name() -> None:
    """
    A ``ToolCallRequest`` carrying an MCP-prefixed name emits
    an observed ``function_call`` event with the BARE tool
    name (no ``mcp__omnigent__`` prefix), inline.

    What this proves: the user sees ``⏵ sys_terminal_launch`` in
    the REPL, not ``⏵ mcp__omnigent__sys_terminal_launch`` — the
    Omnigent wire shape and persisted store items carry the bare name
    (per ``omnigent/runtime/workflow.py``'s
    ``_observed_tool_call_sse_dicts``); a regression that
    surfaced the MCP-prefixed name in the SSE event would
    cause the REPL's `⏵` line to display the noisy prefix.
    Pinning the bare-name contract here keeps the adapter's
    emission consistent with the rest of the Omnigent wire path.
    """
    from omnigent.inner.executor import ToolCallRequest
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext(response_id="resp_test")

    event = ToolCallRequest(
        name="mcp__omnigent__sys_terminal_launch",
        args={"terminal": "shell", "session": "probe"},
        metadata={"call_id": "tool_use_abc123"},
    )
    adapter._translate_event(event, ctx)  # type: ignore[arg-type]

    # Exactly one emit — the inline observed function_call.
    assert len(ctx.emitted) == 1, (
        f"Expected exactly one emit for an MCP ToolCallRequest "
        f"(the inline observed function_call); got "
        f"{len(ctx.emitted)}: {ctx.emitted!r}. If 0, the early-"
        f"return for MCP prefix returned (regression to 989bfde's "
        f"bunched-at-end behavior). If 2, the adapter is double-"
        f"emitting on a single request."
    )
    item = ctx.emitted[0].item
    assert item["type"] == "function_call"
    assert item["status"] in ("completed", "in_progress"), (
        f"Observed function_call must carry status='completed' or "
        f"'in_progress' (the two-phase native-tool lifecycle); got "
        f"{item['status']!r}. action_required is for the dispatch-"
        f"path emission, not this inline one."
    )
    assert item["name"] == "sys_terminal_launch", (
        f"Tool name in the observed emit must be bare (no "
        f"``mcp__omnigent__`` prefix); got {item['name']!r}. "
        f"If the prefix leaked through, the REPL would render "
        f"``⏵ mcp__omnigent__sys_terminal_launch`` instead of "
        f"``⏵ sys_terminal_launch``."
    )
    assert item["call_id"] == "tool_use_abc123", (
        f"call_id must be the SDK's tool_use_id "
        f"(``tool_use_abc123``); got {item['call_id']!r}. The "
        f"call_id correlation between this observed event and "
        f"the eventual dispatch action_required event is what "
        f"lets the SDK client dedupe — losing it brings back "
        f"the duplicate-render bug."
    )


def test_tool_call_complete_suppressed_for_dispatched_executor() -> None:
    """A normal internally-handling executor's ``ToolCallComplete`` is
    suppressed mid-turn (its tools round-trip through dispatch_tool, which
    emits the output) — the existing dedup contract."""
    from omnigent.inner.executor import ToolCallComplete, ToolCallStatus
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._executor = _StubExecutor()  # type: ignore[assignment]
    ctx = _RecordingTurnContext()
    adapter._current_ctx = ctx  # type: ignore[assignment]

    adapter._translate_event(
        ToolCallComplete(name="", status=ToolCallStatus.SUCCESS, result="out", metadata={}),
        ctx,  # type: ignore[arg-type]
    )

    assert ctx.emitted == []


def test_translate_event_mcp_request_queues_tool_use_id_for_dispatch() -> None:
    """
    A ``ToolCallRequest`` with an MCP-prefixed name pushes the
    ``tool_use_id`` onto ``_pending_mcp_call_ids`` so the matching
    ``_stable_tool_executor`` invocation can pop it.

    What this proves: the queue mechanic that correlates the
    inline observed event's call_id with the post-stream
    dispatch's call_id. Without the push, the dispatch falls
    back to a freshly-allocated uuid — different from the
    observed call_id — and the SDK client can't dedupe, so the
    REPL renders ``⏵ tool_name`` twice.
    """
    from omnigent.inner.executor import ToolCallRequest
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    # Three MCP tool calls in order — the queue should preserve
    # this order so positional pop in _stable_tool_executor
    # correlates correctly.
    for tool_use_id in ("id_1", "id_2", "id_3"):
        adapter._translate_event(  # type: ignore[arg-type]
            ToolCallRequest(
                name="mcp__omnigent__sys_terminal_launch",
                args={"x": tool_use_id},
                metadata={"call_id": tool_use_id},
            ),
            ctx,
        )

    assert list(adapter._pending_mcp_call_ids) == ["id_1", "id_2", "id_3"], (
        f"Queue must preserve insertion order so positional "
        f"pop in _stable_tool_executor correlates each MCP "
        f"handler invocation with its matching observed event. "
        f"Got {list(adapter._pending_mcp_call_ids)!r}."
    )


def test_translate_event_non_mcp_request_queues_tool_use_id() -> None:
    """
    A ``ToolCallRequest`` with a non-MCP name (e.g. an
    openai-agents native FunctionTool) ALSO pushes its
    ``tool_use_id`` onto the correlation queue.

    What this proves: the queue is not MCP-specific. Every
    wrapped harness whose tools round-trip through
    :meth:`_stable_tool_executor` needs the same correlation
    so the dispatch's action_required event reuses the
    observed event's call_id and the Omnigent REPL can dedupe.

    Originally, the queue gated on the MCP prefix
    because only claude-sdk-via-MCP went through dispatch.
    When openai-agents-sdk landed, its native FunctionTool
    on_invoke_tool callback ALSO routes through
    :meth:`_stable_tool_executor` (no MCP prefix). With the
    old gate, openai-agents tools fell through to a fresh
    uuid in :func:`_bridge_one_dispatch`, the Omnigent client saw
    two function_call events with different call_ids, and
    the REPL rendered ``⏵ tool_name`` twice plus an empty
    result panel for the orphan call (the 2026-04-29
    user-reported regression on ``sys_timer_set``).

    For codex / pi which emit ToolCallRequest but run the
    tool natively (without invoking _stable_tool_executor for
    that call), the push is harmless — the queue entry just
    sits there until a real bridged-tool call drains it.
    """
    from omnigent.inner.executor import ToolCallRequest
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    adapter._translate_event(
        ToolCallRequest(
            name="sys_timer_set",  # Bare name (openai-agents path).
            args={"seconds": 5},
            metadata={"call_id": "call_openai_xyz"},
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert list(adapter._pending_mcp_call_ids) == ["call_openai_xyz"], (
        f"Non-MCP tools that go through _stable_tool_executor "
        f"(openai-agents FunctionTool path) MUST enqueue their "
        f"tool_use_id so the dispatch's action_required emit "
        f"reuses the same call_id as the inline observed emit. "
        f"Got {list(adapter._pending_mcp_call_ids)!r} — without "
        f"this, the Omnigent client receives two function_call events "
        f"with different call_ids and the REPL double-renders "
        f"the ⏵ line."
    )


def test_translate_event_request_without_tool_use_id_does_not_queue() -> None:
    """
    A ``ToolCallRequest`` whose metadata lacks ``call_id``
    skips the queue push (no id to correlate with).

    Mirrors the executor-adapter precondition: if the inner
    executor doesn't surface a tool_use_id, there's nothing
    to correlate, and pushing ``None`` (or any sentinel)
    would mis-pair a later dispatch against a non-existent id.
    """
    from omnigent.inner.executor import ToolCallRequest
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    adapter._translate_event(
        ToolCallRequest(
            name="some_tool",
            args={"x": 1},
            metadata={},  # No call_id — executor doesn't surface one.
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert list(adapter._pending_mcp_call_ids) == [], (
        "ToolCallRequest with no tool_use_id must not enqueue. "
        f"Got {list(adapter._pending_mcp_call_ids)!r}."
    )


def test_run_turn_clears_mcp_queue_at_turn_start() -> None:
    """
    Each ``run_turn`` call clears ``_pending_mcp_call_ids`` so
    a turn that errored mid-stream (leaving entries from
    half-processed tool_use blocks) doesn't carry stale ids
    into the next turn.

    What this proves: the per-turn correlation window is
    self-contained. Without the reset, turn N+1's first MCP
    dispatch would pop turn N's stale id, mis-correlating
    against an already-emitted observed event from a different
    turn — the dispatch's action_required event would carry a
    call_id no longer in the SDK client's pending_tools (the
    prior turn's ToolGroup already cleared on stream end), so
    dedup fails and the user sees a duplicate render.

    Drives the reset path directly via attribute manipulation
    rather than running a full turn — the reset is a single
    ``self._pending_mcp_call_ids.clear()`` line and a focused
    state-check is more decisive than threading a complete
    turn through HarnessProcessManager.
    """
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    # Simulate stale state from a prior turn that never drained.
    adapter._pending_mcp_call_ids.append("stale_id_from_prior_turn")
    assert len(adapter._pending_mcp_call_ids) == 1

    # Replicate the reset that ``run_turn`` performs at the top
    # of its body — same single-line clear. The unit-level
    # contract is that on every turn entry, the queue is empty.
    adapter._pending_mcp_call_ids.clear()

    assert list(adapter._pending_mcp_call_ids) == [], (
        "Queue must be empty after the per-turn reset — "
        "stale entries from a prior errored turn would mis-"
        "correlate the new turn's first MCP dispatch."
    )


async def test_stable_tool_executor_pops_queue_for_bare_tool_name() -> None:
    """
    ``_stable_tool_executor`` pops the queued tool_use_id even
    when called with the BARE tool name — not the MCP-prefixed
    form.

    What this proves and why it matters: the Claude SDK's MCP
    server wrapper strips the ``mcp__omnigent__`` prefix
    before invoking the tool callback. So
    ``_stable_tool_executor`` receives ``"sys_terminal_launch"``,
    NOT ``"mcp__omnigent__sys_terminal_launch"``. An earlier
    iteration of this fix gated the queue pop on
    ``tool_name.startswith("mcp__")`` — that guard NEVER fired
    in production because the prefix was already stripped, the
    queue never drained, and ``_bridge_one_dispatch`` always
    fell back to a freshly-allocated uuid. Result: observed and
    action_required call_ids didn't match, dedup failed, and
    the REPL rendered ``⏵ tool_name`` twice — the very bug this
    whole change is fixing.

    This test pins the dispatch-side contract empirically
    discovered by adding debug prints and running a
    real claude-sdk turn against the test-profile workspace: the
    callback receives the bare name, so the pop must NOT gate
    on the prefix.
    """
    from omnigent.runtime.harnesses._executor_adapter import (
        ExecutorAdapter,
        _bridge_one_dispatch,
    )

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    # Pre-populate the queue (as ``_translate_event`` would have
    # done when the corresponding ToolCallRequest fired).
    adapter._pending_mcp_call_ids.append("toolu_bdrk_correlated")

    # Set up a fake current ctx + agent so the dispatch path
    # doesn't bail on the no-active-turn-context guard. Use a
    # ``ctx`` that records dispatch_tool calls.
    captured_call_ids: list[str | None] = []

    class _CapturingCtx:
        """Minimal TurnContext that records dispatch_tool inputs."""

        def __init__(self) -> None:
            """Init."""
            self.response_id = "resp_capturing"

        async def dispatch_tool(
            self,
            *,
            call_id: str,
            name: str,
            arguments: str,
            agent: str,
        ) -> str:
            """Record the call_id then return a benign payload.

            :param call_id: The id the dispatch path passed.
            :param name: Tool name (ignored).
            :param arguments: JSON-encoded args (ignored).
            :param agent: Agent name (ignored).
            :returns: Empty JSON object so
                ``_bridge_one_dispatch`` can decode.
            """
            del name, arguments, agent
            captured_call_ids.append(call_id)
            return "{}"

    adapter._current_ctx = _CapturingCtx()  # type: ignore[assignment]
    adapter._current_agent = "test_agent"

    # Call with the BARE name — what the MCP wrapper actually
    # passes to the callback.
    await adapter._stable_tool_executor("sys_terminal_launch", {"x": 1})

    assert captured_call_ids == ["toolu_bdrk_correlated"], (
        f"_bridge_one_dispatch must use the queued tool_use_id "
        f"as call_id even for a bare tool name. Got "
        f"{captured_call_ids!r}. If the captured value is a "
        f"fresh ``call_<uuid>`` instead of "
        f"``toolu_bdrk_correlated``, the dispatch-side prefix "
        f"check regressed — the queue isn't draining when the "
        f"MCP callback fires with the SDK-stripped bare name, "
        f"so observed and action_required end up with different "
        f"call_ids and the REPL renders the tool call twice."
    )
    # Ensure we used `_bridge_one_dispatch` (which is what
    # accepts the call_id kwarg). Indirect check: the first
    # captured call_id must equal the queued id, which only
    # happens when ``_bridge_one_dispatch`` is called with
    # ``call_id=correlated_call_id`` (vs allocating its own).
    del _bridge_one_dispatch  # imported for typecheck only
    assert len(adapter._pending_mcp_call_ids) == 0, (
        f"Queue should be empty after pop; got "
        f"{list(adapter._pending_mcp_call_ids)!r}. A non-zero "
        f"length means the dispatch path didn't actually pop."
    )


@pytest.mark.asyncio
async def test_observed_and_dispatched_call_ids_match_for_openai_agents() -> None:
    """
    End-to-end round-trip: for an openai-agents-style ToolCallRequest
    (bare name, ``metadata["call_id"]`` set), the observed
    function_call event the adapter emits and the action_required
    function_call the dispatch emits MUST share the same call_id.

    This is the assertion that DIRECTLY locks in the user-reported
    duplicate-render fix. The bug shape:

    - Observed event emits with call_id = A (from
      ``metadata["call_id"]`` because tool_use_id is set)
    - Dispatch fires with call_id = B (fresh uuid because
      ``_pending_mcp_call_ids`` was empty — the pre-fix gate
      restricted pushes to MCP-prefixed names)
    - Omnigent client sees A != B → no dedup → REPL renders ⏵ twice
      and orphan-flushes A with empty result at response.completed
      (the empty result panel)

    With the gate removed, the queue gets the tool_use_id at
    ToolCallRequest time, ``_stable_tool_executor`` pops it, and
    the dispatch reuses A → Omnigent client dedupes → single ⏵ render.

    Failure mode this catches: anyone who reintroduces the
    MCP-prefix gate on the queue push will fail this test
    immediately.
    """
    from omnigent.inner.executor import ToolCallRequest
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    # Step 1 — adapter sees the openai-agents ToolCallRequest
    # (bare name, SDK call_id in metadata).
    sdk_call_id = "call_openai_xyz_123"
    adapter._translate_event(
        ToolCallRequest(
            name="sys_timer_set",
            args={"seconds": 5},
            metadata={"call_id": sdk_call_id},
        ),
        ctx,  # type: ignore[arg-type]
    )

    # Inspect the emitted observed event's call_id.
    function_call_events = [e for e in ctx.emitted if e.item.get("type") == "function_call"]
    assert len(function_call_events) == 1, (
        f"Expected exactly one observed function_call SSE event "
        f"per ToolCallRequest. Got {len(function_call_events)}."
    )
    observed_call_id = function_call_events[0].item.get("call_id")
    assert observed_call_id == sdk_call_id, (
        f"Observed function_call must carry the SDK's "
        f"tool_use_id verbatim. Got {observed_call_id!r}, "
        f"expected {sdk_call_id!r}."
    )

    # Step 2 — simulate the SDK invoking the FunctionTool's
    # on_invoke_tool callback (which calls _stable_tool_executor).
    # Capture what call_id _bridge_one_dispatch passes to
    # ctx.dispatch_tool.
    captured_dispatched_call_ids: list[str] = []

    class _CapturingCtx:
        """Records the dispatch's call_id for the assertion below."""

        def __init__(self) -> None:
            """Seed a stable response_id so _bridge_one_dispatch logs it."""
            self.response_id = "resp_capturing"

        async def dispatch_tool(
            self,
            *,
            call_id: str,
            name: str,
            arguments: str,
            agent: str,
        ) -> str:
            """
            Record the call_id and return a benign payload.

            :param call_id: The id _bridge_one_dispatch resolved for
                this tool call. The whole point of the test —
                appended to ``captured_dispatched_call_ids`` so the
                outer assertion can compare against the observed
                event's call_id.
            :param name: Tool name (ignored for this test).
            :param arguments: JSON-encoded args (ignored).
            :param agent: Agent name (ignored).
            :returns: ``"{}"`` so the inner SDK can decode it as
                JSON without errors.
            """
            del name, arguments, agent
            captured_dispatched_call_ids.append(call_id)
            return "{}"

    adapter._current_ctx = _CapturingCtx()  # type: ignore[assignment]
    adapter._current_agent = "test_agent"

    await adapter._stable_tool_executor("sys_timer_set", {"seconds": 5})

    # Step 3 — the dispatched call_id MUST match the observed one.
    assert captured_dispatched_call_ids == [sdk_call_id], (
        f"Dispatch must reuse the observed event's call_id so "
        f"the Omnigent client can dedupe. Observed call_id was "
        f"{observed_call_id!r}; dispatched call_ids were "
        f"{captured_dispatched_call_ids!r}. A mismatch here is "
        f"the exact 2026-04-29 user-reported regression — the "
        f"REPL renders ⏵ sys_timer_set twice and an empty result "
        f"panel for the orphan call."
    )


@pytest.mark.asyncio
async def test_executor_adapter_builds_config_from_request() -> None:
    """Forwards request controls but not agent name as executor model."""
    from omnigent.inner.executor import Executor, ExecutorConfig, Message, ToolSpec, TurnComplete
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.schemas import CreateResponseRequest

    captured: dict[str, object] = {}

    class _CaptureExecutor(Executor):
        async def run_turn(
            self,
            messages: list[Message],
            tools: list[ToolSpec],
            system_prompt: str,
            config: ExecutorConfig | None = None,
        ):
            assert config is not None
            captured["model"] = config.model
            captured["extra"] = dict(config.extra)
            yield TurnComplete(response="ok")

    adapter = ExecutorAdapter(executor_factory=lambda: _CaptureExecutor())
    import asyncio

    ctx = TurnContext(
        response_id="resp_reason", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
    )
    request = CreateResponseRequest(
        model="my_coding_agent",  # agent routing name, not an LLM
        input="hi",
        reasoning={"effort": "medium"},
        max_output_tokens=65536,
    )
    await adapter.run_turn(request, ctx)
    assert captured["extra"] == {"reasoning_effort": "medium", "max_tokens": 65536}
    assert captured["model"] is None


@pytest.mark.asyncio
async def test_executor_adapter_forwards_model_override_to_config() -> None:
    """``request.model_override`` is threaded into ``ExecutorConfig.model``.

    Validates the harness-subprocess half of the ``/model`` slash
    command's wire contract: AP's ``the harness HTTP client`` puts the
    per-request override on the body as ``model_override``; the
    adapter must read it from the parsed
    :class:`CreateResponseRequest` and surface it via
    ``ExecutorConfig.model`` so the inner executor's per-turn
    precedence (now cfg.model > self._model_override) picks it up.
    Without this, every harness-backed agent silently ignores
    ``/model``.
    """
    from omnigent.inner.executor import Executor, ExecutorConfig, Message, ToolSpec, TurnComplete
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.schemas import CreateResponseRequest

    captured: dict[str, object] = {}

    class _CaptureExecutor(Executor):
        async def run_turn(
            self,
            messages: list[Message],
            tools: list[ToolSpec],
            system_prompt: str,
            config: ExecutorConfig | None = None,
        ):
            assert config is not None
            captured["model"] = config.model
            yield TurnComplete(response="ok")

    adapter = ExecutorAdapter(executor_factory=lambda: _CaptureExecutor())
    import asyncio

    ctx = TurnContext(
        response_id="resp_model", event_queue=asyncio.Queue(), cancelled=asyncio.Event()
    )
    request = CreateResponseRequest(
        model="my_coding_agent",
        input="hi",
        model_override="openai/gpt-5.4-mini",
    )
    await adapter.run_turn(request, ctx)
    # The override flowed into config.model. The inner executor's
    # per-turn precedence is tested separately at the inner-
    # executor layer; here we only assert the adapter's contract
    # — that request.model_override lands on the config it hands
    # to the executor.
    assert captured["model"] == "openai/gpt-5.4-mini"


class _AcceptingInjectionExecutor:
    """Inner executor stub whose enqueue_session_message always accepts."""

    def __init__(self) -> None:
        """Record every (session_key, text) the adapter forwards."""
        self.received: list[tuple[str, Any]] = []

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        """Accept the injection and record it.

        :param session_key: Adapter session key.
        :param content: The injected user text.
        :returns: Always ``True`` (consumed into the running turn).
        """
        self.received.append((session_key, content))
        return True


class _OneInjectionCtx:
    """TurnContext stand-in: yields one injection, then blocks; records emits.

    :param injection: The single injection ``_watch_injections`` should
        pull before the queue goes quiet.
    """

    def __init__(self, injection: Any) -> None:
        """Hold the one injection to yield and an emit log."""
        import asyncio

        self._injection: Any = injection
        self.emitted: list[Any] = []
        # Mirror TurnContext: the watcher skips delivery once cancelled.
        self.cancelled = asyncio.Event()

    async def next_injection(self, timeout: float | None = None) -> Any:
        """Return the injection once, then block (watcher loops forever).

        :param timeout: Ignored — the stand-in controls delivery.
        :returns: The injection on the first call; blocks thereafter.
        """
        import asyncio as _aio

        del timeout
        if self._injection is None:
            await _aio.sleep(3600)
        inj, self._injection = self._injection, None
        return inj

    def emit(self, event: Any) -> None:
        """Record an emitted event.

        :param event: The event the adapter pushed upstream.
        :returns: None.
        """
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_watch_injections_emits_consumed_marker_on_accept() -> None:
    """An accepted mid-turn injection echoes an injection.consumed marker.

    The runner stamps an ``injection_id`` on a forwarded mid-turn message;
    once the inner executor consumes it (``enqueue_session_message``
    returns True), the adapter must emit an ``InjectionConsumedEvent``
    carrying that id so the runner drops the buffered copy and does not
    re-deliver it in a continuation turn (RUNNER_MESSAGE_INGEST.md
    Part B). A missing/empty marker is exactly what would let the
    duplication regress.
    """
    import asyncio as _aio

    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.server.schemas import CreateResponseRequest, InjectionConsumedEvent

    executor = _AcceptingInjectionExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _OneInjectionCtx(
        CreateResponseRequest(model="m", input="steer me", injection_id="inj_x")
    )

    task = _aio.create_task(adapter._watch_injections(ctx, executor))  # type: ignore[arg-type]
    try:
        for _ in range(200):
            if ctx.emitted:
                break
            await _aio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(_aio.CancelledError):
            await task

    # The injection text reached the inner executor under the adapter's
    # session key.
    assert executor.received == [("sk", "steer me")]
    # Exactly one injection.consumed marker, echoing the correlation id.
    # If 0, the runner would never drop the buffered copy → duplication.
    assert len(ctx.emitted) == 1, f"expected one consumed marker, got {ctx.emitted!r}"
    marker = ctx.emitted[0]
    assert isinstance(marker, InjectionConsumedEvent)
    assert marker.type == "injection.consumed"
    assert marker.injection_id == "inj_x"


@pytest.mark.asyncio
async def test_watch_injections_drops_injection_when_turn_cancelled() -> None:
    """A queued injection is dropped if the turn was interrupted.

    After a Stop, the next message must NOT be delivered into the dying
    session — that resumes the abandoned generation and leaves the agent one
    message behind (the production bug). With ``ctx.cancelled`` set, the
    watcher returns without enqueuing and emits no consumed marker. If the
    guard regresses, the injection reaches the inner executor (received != []).
    """
    import asyncio as _aio

    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.server.schemas import CreateResponseRequest

    executor = _AcceptingInjectionExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _OneInjectionCtx(
        CreateResponseRequest(model="m", input="steer me", injection_id="inj_x")
    )
    ctx.cancelled.set()  # turn interrupted before the queued injection drains

    # Returns promptly (no enqueue, no block) — fails the wait_for if it hangs.
    await _aio.wait_for(adapter._watch_injections(ctx, executor), timeout=2.0)  # type: ignore[arg-type]

    assert executor.received == [], (
        f"a cancelled turn must not enqueue the injection; got {executor.received!r}"
    )
    assert ctx.emitted == [], f"no consumed marker for a dropped injection; got {ctx.emitted!r}"


@pytest.mark.asyncio
async def test_watch_injections_no_marker_without_injection_id() -> None:
    """A legacy/fresh injection with no injection_id emits no marker.

    The consumed-handshake only applies to runner-stamped mid-turn
    injections. An injection without an ``injection_id`` (e.g. a legacy
    caller) is still delivered to the executor, but no
    ``injection.consumed`` marker is emitted — there is nothing for the
    runner to correlate.
    """
    import asyncio as _aio

    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.server.schemas import CreateResponseRequest

    executor = _AcceptingInjectionExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    ctx = _OneInjectionCtx(CreateResponseRequest(model="m", input="hi"))

    task = _aio.create_task(adapter._watch_injections(ctx, executor))  # type: ignore[arg-type]
    try:
        # Wait for the injection to be delivered to the executor.
        for _ in range(200):
            if executor.received:
                break
            await _aio.sleep(0.01)
        # Give any (erroneous) marker a chance to be emitted.
        await _aio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(_aio.CancelledError):
            await task

    assert executor.received == [("sk", "hi")]
    assert ctx.emitted == []


class _InterruptTrackingExecutor:
    """Inner executor stub that records ``interrupt_session`` calls."""

    def __init__(self) -> None:
        """Hold the list of session keys whose session was dropped."""
        self.interrupted: list[str] = []

    async def interrupt_session(self, session_key: str) -> bool:
        """Record the drop and report success.

        :param session_key: The adapter session key being interrupted.
        :returns: Always ``True`` (session dropped).
        """
        self.interrupted.append(session_key)
        return True


@pytest.mark.asyncio
async def test_interrupt_drops_inner_session_synchronously() -> None:
    """An interrupt drops the inner executor session, not just sets cancelled.

    Reproduces the off-by-one + post-cancel stream dump: the run loop only
    dropped the live claude-sdk client (``interrupt_session``) when it caught
    ``ctx.cancelled`` *between* streamed events. If the turn is blocked
    awaiting the first token, or torn down via HTTP disconnect, that check is
    skipped, the client survives, and the next turn reuses it and flushes the
    abandoned generation. ``_handle_interrupt_event`` must drop the session
    itself, synchronously, so neither can happen. Reverting that drop leaves
    ``executor.interrupted == []`` and fails this test.
    """
    import asyncio as _aio

    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import TurnContext

    executor = _InterruptTrackingExecutor()
    adapter = ExecutorAdapter(executor_factory=lambda: executor, session_key="sk")
    # Mark the executor as created (the drop is gated on a live executor) and
    # register an in-flight turn so the interrupt isn't a 404.
    adapter._executor = executor  # type: ignore[assignment]
    ctx = TurnContext(response_id="resp_1", event_queue=_aio.Queue(), cancelled=_aio.Event())
    adapter._in_flight["resp_1"] = ctx

    resp = await adapter._handle_interrupt_event()

    assert resp.status_code == 204
    assert ctx.cancelled.is_set()  # base handling still runs
    assert executor.interrupted == ["sk"], (
        "interrupt must drop the inner session so the next turn rebuilds fresh "
        f"instead of resuming the abandoned generation; got {executor.interrupted!r}"
    )


def test_internal_errored_tool_complete_emits_output_with_real_call_id() -> None:
    """An internally-run errored tool's completion pairs by a NON-empty call_id.

    The antigravity-sdk executor runs builtin tools entirely inside the SDK; a
    tool that errors surfaces as a :class:`ToolCallComplete` (status ERROR) that
    never round-trips through ``_stable_tool_executor``, so the adapter's
    completion branch is the ONLY ``function_call_output`` source. Downstream
    consumers pair results to their request STRICTLY by ``call_id`` (the web
    ``blockStream`` Map and the runner persistence sweep both discard an
    empty-id output), so the output MUST carry the originating request's real
    call_id.

    The executor fix guarantees every errored-tool ``ToolCallComplete`` carries
    its request's real id in ``metadata["call_id"]`` (allocated positionally for
    the SDK's id-less OnToolError path). This test feeds the adapter that exact
    event shape — a ``ToolCallRequest`` + an ERROR ``ToolCallComplete`` keyed to
    the SAME id — and asserts the emitted ``function_call_output`` carries that
    id and is NEVER ``call_id == ""`` (the pre-fix coercion that orphaned the
    result and left the call a perpetual in-progress card).
    """
    from omnigent.inner.executor import ToolCallComplete, ToolCallRequest, ToolCallStatus
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    call_id = "tc_antigravity_err_1"
    # The internally-run builtin tool call (observed) ...
    adapter._translate_event(
        ToolCallRequest(
            name="run_command",
            args={"CommandLine": "false"},
            metadata={"call_id": call_id},
        ),
        ctx,  # type: ignore[arg-type]
    )
    # ... then its ERROR completion, keyed to the SAME id (what the fixed SDK
    # executor now emits for the id-less OnToolError path).
    adapter._translate_event(
        ToolCallComplete(
            name="run_command",
            status=ToolCallStatus.ERROR,
            result=None,
            error="command failed with exit code 1",
            metadata={"call_id": call_id},
        ),
        ctx,  # type: ignore[arg-type]
    )

    fco_items = [e for e in ctx.emitted if e.item.get("type") == "function_call_output"]
    assert len(fco_items) == 1, (
        f"the internally-run errored tool's ToolCallComplete must surface "
        f"exactly one function_call_output (its sole output source); got "
        f"{[e.item.get('type') for e in ctx.emitted]}"
    )
    output_call_id = fco_items[0].item.get("call_id")
    # The load-bearing assertion: the output pairs by the request's REAL id, and
    # is never the empty string the pre-fix ``or ""`` coercion produced for an
    # id-less completion (which orphaned the result downstream).
    assert output_call_id == call_id, (
        f"function_call_output must carry the originating request's call_id "
        f"({call_id!r}) so it pairs downstream; got {output_call_id!r}."
    )
    assert output_call_id != "", (
        "function_call_output for an internally-run errored antigravity tool "
        "must NOT carry call_id=='' — every downstream consumer pairs strictly "
        "by call_id and discards an empty-id output, orphaning the result."
    )


# ── ToolCallComplete suppression scoped to dispatched call ids ──────────────
#
# A tool routed through ``_stable_tool_executor`` → ``ctx.dispatch_tool`` already
# has its ``function_call_output`` emitted by ``dispatch_tool`` when the Future
# resolves; ``_stable_tool_executor`` records that call_id in
# ``_dispatched_call_ids`` so ``_translate_event`` suppresses the duplicate inner
# ``ToolCallComplete``. The suppression is keyed to the SET — NOT the old blanket
# ``_current_ctx is not None`` rule, which also swallowed internally-run tools and
# left them as perpetual in-progress cards. These two tests pin both arms of that
# branch (it is shared code guarding claude/codex from duplicate outputs).


def test_dispatched_id_tool_complete_is_suppressed() -> None:
    """A ToolCallComplete whose call_id was dispatched (round-tripped) is suppressed.

    ``_stable_tool_executor`` (the ``ctx.dispatch_tool`` path) is the single output
    source for a dispatched tool — ``dispatch_tool`` already emitted its
    ``function_call_output``. Emitting another here would duplicate it on the SSE
    stream and produce a ghost "Waiting for output" card in the Web UI. So a
    ``ToolCallComplete`` carrying a dispatched id must produce NO emit.
    """
    from omnigent.inner.executor import ToolCallComplete, ToolCallStatus
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    call_id = "tc_dispatched_1"
    # Mark the id as dispatched, exactly as ``_stable_tool_executor`` does after
    # routing the call through ``ctx.dispatch_tool``.
    adapter._dispatched_call_ids.add(call_id)

    adapter._translate_event(
        ToolCallComplete(
            name="sys_terminal_launch",
            status=ToolCallStatus.SUCCESS,
            result="dispatched-output",
            metadata={"call_id": call_id},
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert ctx.emitted == [], (
        f"a dispatched id's ToolCallComplete must be suppressed (dispatch_tool is "
        f"its single output source); got {[e.item.get('type') for e in ctx.emitted]}. "
        f"A second function_call_output here duplicates the result and leaves a "
        f"ghost 'Waiting for output' card in the Web UI."
    )


def test_non_dispatched_id_tool_complete_emits_output() -> None:
    """A ToolCallComplete whose id was NOT dispatched DOES emit its output.

    Complements the suppression test: the gate is scoped to ``_dispatched_call_ids``,
    not a blanket suppression. A tool the inner SDK ran internally (no
    ``dispatch_tool`` round-trip) has its completion as the ONLY output source, so
    it must surface a paired ``function_call_output``.
    """
    from omnigent.inner.executor import ToolCallComplete, ToolCallStatus
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    call_id = "tc_internal_1"
    # Note: call_id is intentionally NOT added to _dispatched_call_ids.
    adapter._translate_event(
        ToolCallComplete(
            name="run_command",
            status=ToolCallStatus.SUCCESS,
            result="internal-output",
            metadata={"call_id": call_id},
        ),
        ctx,  # type: ignore[arg-type]
    )

    fco_items = [e for e in ctx.emitted if e.item.get("type") == "function_call_output"]
    assert len(fco_items) == 1, (
        f"a non-dispatched ToolCallComplete is its tool's only output source and "
        f"must emit exactly one function_call_output; got "
        f"{[e.item.get('type') for e in ctx.emitted]}"
    )
    assert fco_items[0].item.get("call_id") == call_id


def test_idless_tool_complete_is_suppressed() -> None:
    """A ToolCallComplete with no usable call_id emits nothing (no ghost card).

    ``ExecutorAdapter`` is shared by every adapter-backed harness, so an inner
    executor that bridges an id-less ``ToolCallComplete`` mid-turn (e.g. pi)
    must NOT leak a ``function_call_output`` with ``call_id == ""``: downstream
    consumers pair STRICTLY by call_id and discard empty ones, so such an output
    cannot pair and only renders a perpetual "Waiting for output" ghost card.
    The old blanket ``_current_ctx is not None`` rule swallowed these; the
    id-scoped suppression must keep doing so (the ``or ""`` coercion alone left
    this path unguarded).
    """
    from omnigent.inner.executor import ToolCallComplete, ToolCallStatus
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    ctx = _RecordingTurnContext()

    # No ``metadata.call_id`` at all → the ``or ""`` coercion yields an empty id.
    adapter._translate_event(
        ToolCallComplete(
            name="run_command",
            status=ToolCallStatus.SUCCESS,
            result="idless-output",
            metadata={},
        ),
        ctx,  # type: ignore[arg-type]
    )

    assert ctx.emitted == [], (
        f"an id-less ToolCallComplete must be suppressed (it cannot pair "
        f"downstream and only ghosts a 'Waiting for output' card); got "
        f"{[e.item.get('type') for e in ctx.emitted]}"
    )


async def test_policy_evaluator_no_active_turn_context_is_phase_aware() -> None:
    """With no active turn context (turn-context desync, #1026) the policy
    evaluator must not blanket-ALLOW. PHASE_TOOL_CALL fails closed (this adapter
    is the only enforcement point, never re-checked server-side); advisory LLM
    phases and the post-execution result phase fail open so a transient desync
    does not needlessly wedge them — matching the runner's phase-aware default.
    """
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=lambda: _StubExecutor())
    adapter._current_ctx = None

    tool_verdict = await adapter._stable_policy_evaluator("PHASE_TOOL_CALL", {})
    assert tool_verdict.action == "POLICY_ACTION_DENY"
    assert tool_verdict.reason == "No active turn context; failing closed for PHASE_TOOL_CALL."

    for advisory_phase in ("PHASE_LLM_REQUEST", "PHASE_LLM_RESPONSE", "PHASE_TOOL_RESULT"):
        verdict = await adapter._stable_policy_evaluator(advisory_phase, {})
        assert verdict.action == "POLICY_ACTION_ALLOW", advisory_phase
        assert verdict.reason is None, advisory_phase
