"""
Tests for the ``executor.type: opencode`` harness wrap.

Mirror of ``tests/inner/test_databricks_supervisor_harness.py`` — verifies
the wrap module has the same shape (registry entry, FastAPI app routes,
env-var-driven configuration). Does NOT spawn a real ``opencode`` CLI;
:func:`asyncio.create_subprocess_exec` is replaced with a stub that
streams a scripted sequence of JSON events so the event-translation path
is exercised end-to-end without an external dependency.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from omnigent.inner import opencode_executor, opencode_harness
from omnigent.inner.executor import (
    ExecutorError,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnComplete,
)
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES


def test_harness_module_registered_in_module_registry() -> None:
    """``"opencode"`` resolves to the wrap module path.

    Without this entry, the runner subprocess cannot dispatch a
    ``executor.config.harness: opencode`` spec to the right
    ``create_app()``.
    """
    assert _HARNESS_MODULES.get("opencode") == "omnigent.inner.opencode_harness"


def test_harness_accepted_by_spec_validator() -> None:
    """``"opencode"`` is in the spec-side allowlist.

    Without this entry, the Omnigent spec validator would reject
    every YAML that declares ``executor.config.harness: opencode``
    before the runner ever got a chance to spawn the harness.
    """
    assert "opencode" in OMNIGENT_HARNESSES


def test_create_app_returns_fastapi_with_required_routes() -> None:
    """``create_app()`` returns a FastAPI app exposing the harness API.

    The OpenCode CLI is spawned lazily on the first turn (not at
    app build time), so this test passes without ``opencode``
    installed.
    """
    app = opencode_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


# ── _parse_truthy: env-var convention ────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        (None, False),
        ("   ", False),
    ],
)
def test_parse_truthy_matches_other_harness_wraps(raw: str | None, expected: bool) -> None:
    """Truthy-env-var parsing matches the other harness wraps verbatim.

    A regression here would split convention from ``pi`` / ``codex``
    so an operator who set ``HARNESS_PI_GATEWAY=1`` and reused the
    same setting for OpenCode would get different behavior.
    """
    assert opencode_executor._parse_truthy(raw) is expected


# ── _resolve_opencode_binary: PATH lookup vs explicit override ──────


def test_resolve_binary_prefers_explicit_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``HARNESS_OPENCODE_PATH`` wins over ``shutil.which("opencode")``.

    The explicit override lets operators pin a specific install
    (e.g. in CI sandboxes) without relying on ``PATH`` mangling.
    """
    monkeypatch.setenv("HARNESS_OPENCODE_PATH", "/custom/opencode")
    # If PATH lookup were tried first this would still pass, so
    # also stub ``which`` to return a different path to prove the
    # env var takes precedence.
    monkeypatch.setattr(opencode_executor.shutil, "which", lambda _: "/usr/local/bin/opencode")

    assert opencode_executor._resolve_opencode_binary() == "/custom/opencode"


def test_resolve_binary_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var → ``shutil.which("opencode")`` is consulted."""
    monkeypatch.delenv("HARNESS_OPENCODE_PATH", raising=False)
    monkeypatch.setattr(opencode_executor.shutil, "which", lambda name: f"/found/{name}")

    assert opencode_executor._resolve_opencode_binary() == "/found/opencode"


def test_resolve_binary_raises_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var and not on PATH → loud :class:`FileNotFoundError`.

    The error message must name both the env var and the install
    URL so a fresh operator can fix the misconfiguration without
    reading code.
    """
    monkeypatch.delenv("HARNESS_OPENCODE_PATH", raising=False)
    monkeypatch.setattr(opencode_executor.shutil, "which", lambda _: None)

    with pytest.raises(FileNotFoundError) as excinfo:
        opencode_executor._resolve_opencode_binary()

    msg = str(excinfo.value)
    assert "HARNESS_OPENCODE_PATH" in msg
    assert "opencode.ai" in msg


# ── _latest_user_text: message-shape robustness ─────────────────────


def test_latest_user_text_returns_string_content_verbatim() -> None:
    """Plain string content surfaces unchanged."""
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "world"},
    ]
    assert opencode_executor._latest_user_text(messages) == "world"


def test_latest_user_text_joins_input_text_blocks() -> None:
    """``input_text`` blocks join with newlines into a single prompt."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "first"},
                {"type": "input_text", "text": "second"},
            ],
        }
    ]
    assert opencode_executor._latest_user_text(messages) == "first\nsecond"


def test_latest_user_text_drops_multimodal_blocks_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Image / file blocks are dropped in v0 with a warning.

    Documents the v0 limitation explicitly — a future commit that
    plumbs multimodal through must update this test alongside the
    behavior change.
    """
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "look at this"},
                {"type": "input_image", "image_url": "data:..."},
            ],
        }
    ]
    with caplog.at_level("WARNING"):
        result = opencode_executor._latest_user_text(messages)
    assert result == "look at this"
    assert any("multimodal" in rec.getMessage() for rec in caplog.records)


def test_latest_user_text_returns_empty_when_no_user_message() -> None:
    """An assistant-only history yields empty — caller raises ExecutorError."""
    messages = [{"role": "assistant", "content": "hi"}]
    assert opencode_executor._latest_user_text(messages) == ""


# ── _build_argv: flag composition ───────────────────────────────────


def test_build_argv_minimal_arguments() -> None:
    """A minimal invocation: just ``opencode run --format json <prompt>``."""
    argv = opencode_executor._build_argv(
        "/usr/local/bin/opencode",
        session_id=None,
        model=None,
        cwd_flag=None,
        agent=None,
        variant=None,
        thinking=False,
        skip_permissions=False,
        prompt="hello",
    )
    assert argv == ["/usr/local/bin/opencode", "run", "--format", "json", "hello"]


def test_build_argv_all_flags_set() -> None:
    """Every optional flag flows into argv in order.

    Order matters less than presence here — what we're really
    asserting is that no flag silently drops on the way through
    (a regression that, say, swallowed ``--variant``).
    """
    argv = opencode_executor._build_argv(
        "opencode",
        session_id="sess_abc",
        model="anthropic/claude-sonnet-4-5",
        cwd_flag="/tmp/work",
        agent="build",
        variant="high",
        thinking=True,
        skip_permissions=True,
        prompt="ship it",
    )
    assert argv[0] == "opencode"
    assert argv[1] == "run"
    assert "--session" in argv and argv[argv.index("--session") + 1] == "sess_abc"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    assert "--dir" in argv and argv[argv.index("--dir") + 1] == "/tmp/work"
    assert "--agent" in argv and argv[argv.index("--agent") + 1] == "build"
    assert "--variant" in argv and argv[argv.index("--variant") + 1] == "high"
    assert "--thinking" in argv
    assert "--dangerously-skip-permissions" in argv
    assert argv[-1] == "ship it"


# ── _translate_event: per-event-type translation ────────────────────


def test_translate_event_text_emits_text_chunk() -> None:
    out = opencode_executor._translate_event(
        {"type": "text", "part": {"text": "hello"}}, emit_reasoning=False
    )
    assert len(out) == 1
    assert isinstance(out[0], TextChunk)
    assert out[0].text == "hello"


def test_translate_event_empty_text_drops() -> None:
    """Empty text deltas are dropped so renderers don't waste frames."""
    out = opencode_executor._translate_event(
        {"type": "text", "part": {"text": ""}}, emit_reasoning=False
    )
    assert out == []


def test_translate_event_reasoning_dropped_when_disabled() -> None:
    """Reasoning events drop unless ``emit_reasoning=True``.

    Mirrors the CLI: ``--thinking`` is opt-in, so the harness env
    var ``HARNESS_OPENCODE_THINKING`` gates this path.
    """
    out = opencode_executor._translate_event(
        {"type": "reasoning", "part": {"text": "thinking..."}},
        emit_reasoning=False,
    )
    assert out == []


def test_translate_event_reasoning_emits_chunk_when_enabled() -> None:
    out = opencode_executor._translate_event(
        {"type": "reasoning", "part": {"text": "thinking..."}},
        emit_reasoning=True,
    )
    assert len(out) == 1
    assert isinstance(out[0], ReasoningChunk)
    assert out[0].delta == "thinking..."
    assert out[0].event_type == "reasoning_text"


def test_translate_event_tool_use_fans_out_to_request_and_complete() -> None:
    """A ``tool_use`` event becomes a paired Request + Complete.

    Both events MUST carry the same ``call_id`` so the AP-side
    correlator can re-pair them into a single observed tool call.
    """
    out = opencode_executor._translate_event(
        {
            "type": "tool_use",
            "part": {
                "id": "tool_xyz",
                "tool": "bash",
                "input": {"command": "ls"},
                "output": "a\nb\n",
                "status": "success",
            },
        },
        emit_reasoning=False,
    )
    assert len(out) == 2
    request, complete = out
    assert isinstance(request, ToolCallRequest)
    assert request.name == "bash"
    assert request.args == {"command": "ls"}
    assert request.metadata == {"call_id": "tool_xyz"}
    assert isinstance(complete, ToolCallComplete)
    assert complete.status == ToolCallStatus.SUCCESS
    assert complete.result == "a\nb\n"
    assert complete.metadata == {"call_id": "tool_xyz"}


def test_translate_event_tool_use_error_status_maps_to_error() -> None:
    out = opencode_executor._translate_event(
        {
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "input": {},
                "status": "error",
                "error": "command failed",
            },
        },
        emit_reasoning=False,
    )
    complete = out[1]
    assert isinstance(complete, ToolCallComplete)
    assert complete.status == ToolCallStatus.ERROR
    assert complete.error == "command failed"


def test_translate_event_error_event_emits_executor_error() -> None:
    out = opencode_executor._translate_event(
        {"type": "error", "error": "boom"}, emit_reasoning=False
    )
    assert len(out) == 1
    assert isinstance(out[0], ExecutorError)
    assert "opencode" in out[0].message
    assert "boom" in out[0].message


@pytest.mark.parametrize("event_type", ["step_start", "step_finish"])
def test_translate_event_step_events_drop(event_type: str) -> None:
    """Step-boundary events are informational — drop without warning."""
    out = opencode_executor._translate_event(
        {"type": event_type, "part": {}}, emit_reasoning=False
    )
    assert out == []


def test_translate_event_unknown_type_logs_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Future OpenCode event kinds must drop, not crash.

    Without this guard, an upstream that ships a new event type
    would tear down every running turn until the harness catches up.
    """
    with caplog.at_level("WARNING"):
        out = opencode_executor._translate_event(
            {"type": "some_future_event", "part": {}}, emit_reasoning=False
        )
    assert out == []
    assert any("unknown event" in rec.getMessage().lower() for rec in caplog.records)


# ── run_turn: end-to-end with a stubbed subprocess ───────────────────


class _FakeStdout:
    """Async readline-iterator over a fixed list of byte lines."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeStderr:
    async def read(self, _: int) -> bytes:
        return b""


class _FakeProcess:
    """Minimal :class:`asyncio.subprocess.Process` stand-in.

    Exposes stdout/stderr the executor reads off, captures the
    argv the test recorded, and reports a configurable exit code.
    """

    def __init__(self, lines: list[bytes], return_code: int = 0) -> None:
        self.stdout = _FakeStdout(lines)
        self.stderr = _FakeStderr()
        self.returncode: int | None = None
        self._final_return_code = return_code

    async def wait(self) -> int:
        self.returncode = self._final_return_code
        return self._final_return_code

    def terminate(self) -> None:  # pragma: no cover — only on cancel path
        self.returncode = -15

    def kill(self) -> None:  # pragma: no cover — only on cancel path
        self.returncode = -9


async def _collect(executor: opencode_executor.OpenCodeExecutor, prompt: str) -> list:
    """Drain one ``run_turn`` into a list for assertion."""
    events = []
    async for event in executor.run_turn(
        messages=[{"role": "user", "content": prompt, "session_id": "omni_sess_1"}],
        tools=[],
        system_prompt="",
        config=None,
    ):
        events.append(event)
    return events


def test_run_turn_streams_text_and_emits_turn_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: stub subprocess streams text events → TextChunks + TurnComplete.

    Locks in:
    - ``--format json`` JSONL parsing.
    - Empty stdout line termination → graceful exit.
    - Final assembled text is reflected in ``TurnComplete.response``.
    """
    monkeypatch.setenv("HARNESS_OPENCODE_PATH", "/fake/opencode")

    scripted_lines = [
        json.dumps(
            {
                "type": "text",
                "sessionID": "opencode_sess_1",
                "part": {"text": "hello "},
            }
        ).encode()
        + b"\n",
        json.dumps(
            {
                "type": "text",
                "sessionID": "opencode_sess_1",
                "part": {"text": "world"},
            }
        ).encode()
        + b"\n",
        b"",  # EOF
    ]

    recorded_argv: list[list[str]] = []

    async def _fake_subprocess(*argv: str, **_: object) -> _FakeProcess:
        recorded_argv.append(list(argv))
        return _FakeProcess(scripted_lines, return_code=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess)

    executor = opencode_executor.OpenCodeExecutor()
    events = asyncio.run(_collect(executor, "hi"))

    # argv shape: ``opencode run --format json
    # --dangerously-skip-permissions hi`` (no --session on the first
    # turn since the cache was empty; permissions are skipped by
    # default because a headless meta-harness has nowhere to surface
    # interactive prompts — see OpenCodeExecutor.__init__).
    assert recorded_argv == [
        [
            "/fake/opencode",
            "run",
            "--format",
            "json",
            "--dangerously-skip-permissions",
            "hi",
        ]
    ]

    text_events = [e for e in events if isinstance(e, TextChunk)]
    assert [e.text for e in text_events] == ["hello ", "world"]

    turn_completes = [e for e in events if isinstance(e, TurnComplete)]
    assert len(turn_completes) == 1
    assert turn_completes[0].response == "hello world"

    # The captured opencode session ID is cached against the
    # Omnigent session_key so the next turn can reattach.
    assert executor._session_ids["omni_sess_1"] == "opencode_sess_1"


def test_run_turn_reuses_captured_session_on_second_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second turn passes ``--session <captured_id>`` to opencode.

    This is the core resume contract — without it, every turn would
    start a brand-new opencode session and the conversation would
    forget itself between user messages.
    """
    monkeypatch.setenv("HARNESS_OPENCODE_PATH", "/fake/opencode")

    def _scripted(session_id: str) -> list[bytes]:
        return [
            json.dumps(
                {
                    "type": "text",
                    "sessionID": session_id,
                    "part": {"text": "ok"},
                }
            ).encode()
            + b"\n",
            b"",
        ]

    recorded_argv: list[list[str]] = []

    async def _fake_subprocess(*argv: str, **_: object) -> _FakeProcess:
        recorded_argv.append(list(argv))
        return _FakeProcess(_scripted("opencode_sess_persistent"), return_code=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess)

    executor = opencode_executor.OpenCodeExecutor()
    asyncio.run(_collect(executor, "first"))
    asyncio.run(_collect(executor, "second"))

    assert len(recorded_argv) == 2
    # First turn has no --session flag.
    assert "--session" not in recorded_argv[0]
    # Second turn carries the captured ID.
    assert "--session" in recorded_argv[1]
    sid_index = recorded_argv[1].index("--session") + 1
    assert recorded_argv[1][sid_index] == "opencode_sess_persistent"


def test_run_turn_emits_executor_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit code surfaces as :class:`ExecutorError`.

    Without this branch a silent failure would land in the AP REPL
    as an empty assistant turn — the operator would have no
    indication that the CLI crashed.
    """
    monkeypatch.setenv("HARNESS_OPENCODE_PATH", "/fake/opencode")

    async def _fake_subprocess(*_: str, **__: object) -> _FakeProcess:
        return _FakeProcess([b""], return_code=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_subprocess)

    executor = opencode_executor.OpenCodeExecutor()
    events = asyncio.run(_collect(executor, "hi"))

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert len(errors) == 1
    assert "exited with code 1" in errors[0].message


def test_run_turn_empty_prompt_emits_executor_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request with no user message must not silently no-op."""
    monkeypatch.setenv("HARNESS_OPENCODE_PATH", "/fake/opencode")
    executor = opencode_executor.OpenCodeExecutor()

    async def _run() -> list:
        events: list = []
        async for event in executor.run_turn(
            messages=[{"role": "assistant", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=None,
        ):
            events.append(event)
        return events

    events = asyncio.run(_run())
    assert any(isinstance(e, ExecutorError) and "no user message" in e.message for e in events)


# ── Capability flags ────────────────────────────────────────────────


def test_executor_capability_flags() -> None:
    """The executor advertises the right capabilities to the Session layer.

    ``handles_tools_internally=True`` is the critical one — without
    it the Session would re-execute every tool OpenCode already ran.
    """
    executor = opencode_executor.OpenCodeExecutor()
    assert executor.supports_streaming() is True
    assert executor.supports_tool_calling() is True
    assert executor.handles_tools_internally() is True
    assert executor.max_context_tokens() is None
    assert executor.supports_live_message_queue() is False


def test_close_session_drops_cached_session_id() -> None:
    """``close_session`` forgets the cached opencode session ID."""

    async def _run() -> bool:
        executor = opencode_executor.OpenCodeExecutor()
        executor._session_ids["k"] = "opencode_sess"
        await executor.close_session("k")
        return "k" not in executor._session_ids

    assert asyncio.run(_run()) is True
