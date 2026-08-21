"""Tests for the ``harness: kimi`` wrap + the inner ``KimiExecutor``.

Covers the harness registry, FastAPI app shape, env-var-driven
construction, and the executor's argv / event-translation / run-turn
flows with the upstream ``kimi`` subprocess stubbed out (so the suite
passes on machines without the binary).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import kimi_executor, kimi_harness
from omnigent.inner.executor import (
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)
from omnigent.inner.kimi_executor import (
    _SESSION_RESUME_RE,
    KimiExecutor,
    _latest_user_text,
    _parse_truthy,
    _resolve_kimi_binary,
    _resolve_skills_dirs,
)
from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.spec._omnigent_compat import OMNIGENT_HARNESS_ALIASES, OMNIGENT_HARNESSES

# ---------------------------------------------------------------------------
# Registry / allowlist
# ---------------------------------------------------------------------------


def test_kimi_in_module_registry() -> None:
    assert _HARNESS_MODULES.get("kimi") == "omnigent.inner.kimi_harness"
    assert _HARNESS_MODULES.get("kimi-code") == "omnigent.inner.kimi_harness"


def test_kimi_in_omnigent_harnesses_allowlist() -> None:
    assert "kimi" in OMNIGENT_HARNESSES
    assert "kimi-code" in OMNIGENT_HARNESS_ALIASES


def test_kimi_canonical_alias_resolution() -> None:
    from omnigent.harness_aliases import canonicalize_harness

    assert canonicalize_harness("kimi-code") == "kimi"
    assert canonicalize_harness("kimi") == "kimi"


# ---------------------------------------------------------------------------
# FastAPI app + factory
# ---------------------------------------------------------------------------


def test_create_app_returns_fastapi_with_required_routes() -> None:
    app = kimi_harness.create_app()
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/v1/sessions/{conversation_id}/events" in paths


def test_executor_factory_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_KIMI_MODEL", "kimi-k2-turbo")
    monkeypatch.setenv("HARNESS_KIMI_CWD", "/tmp/kimi-cwd")
    monkeypatch.setenv("HARNESS_KIMI_PATH", "/custom/bin/kimi")
    monkeypatch.delenv("OMNIGENT_KIMI_PATH", raising=False)
    monkeypatch.setenv("HARNESS_KIMI_PLAN", "yes")
    monkeypatch.setenv("HARNESS_KIMI_CONTINUE_LAST", "true")
    monkeypatch.setenv("HARNESS_KIMI_SKILLS_DIRS", json.dumps(["/a", "/b"]))

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.kimi_harness.KimiExecutor.__init__",
        _fake_init,
    ):
        kimi_harness._build_kimi_executor()

    assert captured["model"] == "kimi-k2-turbo"
    assert captured["cwd"] == "/tmp/kimi-cwd"
    assert captured["binary_path"] == "/custom/bin/kimi"
    assert captured["plan"] is True
    assert captured["continue_last_session"] is True
    assert captured["skills_dirs"] == ["/a", "/b"]


def test_executor_factory_defaults_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HARNESS_KIMI_MODEL",
        "HARNESS_KIMI_CWD",
        "HARNESS_KIMI_PATH",
        "HARNESS_KIMI_PLAN",
        "HARNESS_KIMI_CONTINUE_LAST",
        "HARNESS_KIMI_SKILLS_DIRS",
        # Cleared too: cwd now falls back to it, so a dev with it exported
        # mustn't flip this default-path assertion.
        "OMNIGENT_RUNNER_WORKSPACE",
        # Canonical path env var — would shadow the legacy HARNESS_* delenv above.
        "OMNIGENT_KIMI_PATH",
    ):
        monkeypatch.delenv(var, raising=False)

    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch(
        "omnigent.inner.kimi_harness.KimiExecutor.__init__",
        _fake_init,
    ):
        kimi_harness._build_kimi_executor()

    assert captured["plan"] is False
    assert captured["continue_last_session"] is False
    assert captured["binary_path"] is None  # passes through; executor resolves
    assert captured["model"] is None
    assert captured["cwd"] is None
    assert captured["skills_dirs"] == []


def test_executor_factory_falls_back_to_runner_workspace_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no HARNESS_KIMI_CWD, kimi runs in OMNIGENT_RUNNER_WORKSPACE — the
    session workspace the user launched in — not the runner's /tmp cwd.

    Regression: kimi lacked the workspace fallback the other SDK harnesses have,
    so `omni --harness kimi` launched in the repo but kimi's tools reported the
    /tmp launcher dir. An explicit HARNESS_KIMI_CWD still wins over the fallback.
    """
    monkeypatch.delenv("HARNESS_KIMI_CWD", raising=False)
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/home/me/project")

    captured: dict[str, Any] = {}
    with patch(
        "omnigent.inner.kimi_harness.KimiExecutor.__init__",
        lambda self, **kwargs: captured.update(kwargs),
    ):
        kimi_harness._build_kimi_executor()
    assert captured["cwd"] == "/home/me/project"

    # An explicit HARNESS_KIMI_CWD overrides the workspace fallback.
    monkeypatch.setenv("HARNESS_KIMI_CWD", "/tmp/explicit")
    captured.clear()
    with patch(
        "omnigent.inner.kimi_harness.KimiExecutor.__init__",
        lambda self, **kwargs: captured.update(kwargs),
    ):
        kimi_harness._build_kimi_executor()
    assert captured["cwd"] == "/tmp/explicit"


def test_malformed_os_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_KIMI_OS_ENV", "{not-json")
    captured: dict[str, Any] = {}

    def _fake_init(self: Any, **kwargs: Any) -> None:
        captured["os_env"] = kwargs["os_env"]

    with patch(
        "omnigent.inner.kimi_harness.KimiExecutor.__init__",
        _fake_init,
    ):
        kimi_harness._build_kimi_executor()

    assert captured["os_env"].type == "caller_process"
    assert captured["os_env"].sandbox.type == "none"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("y", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("", False),
        (None, False),
    ],
)
def test_parse_truthy(value: str | None, expected: bool) -> None:
    assert _parse_truthy(value) is expected


def test_resolve_kimi_binary_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_KIMI_PATH", raising=False)
    monkeypatch.delenv("HARNESS_KIMI_PATH", raising=False)
    assert _resolve_kimi_binary() == "kimi"


def test_resolve_kimi_binary_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Canonical OMNIGENT_KIMI_PATH wins.
    monkeypatch.setenv("OMNIGENT_KIMI_PATH", "/opt/bin/kimi")
    assert _resolve_kimi_binary() == "/opt/bin/kimi"


def test_resolve_kimi_binary_legacy_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # Deprecated HARNESS_KIMI_PATH still honored as a fallback.
    monkeypatch.delenv("OMNIGENT_KIMI_PATH", raising=False)
    monkeypatch.setenv("HARNESS_KIMI_PATH", "/legacy/bin/kimi")
    assert _resolve_kimi_binary() == "/legacy/bin/kimi"


def test_latest_user_text_string_message() -> None:
    messages = [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "hello"},
    ]
    assert _latest_user_text(messages) == "hello"


def test_latest_user_text_picks_most_recent_user() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
    ]
    assert _latest_user_text(messages) == "second"


def test_latest_user_text_concats_text_blocks() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "input_text", "text": "world"},
            ],
        }
    ]
    assert _latest_user_text(messages) == "hello world"


def test_latest_user_text_drops_image_blocks_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.WARNING, logger="omnigent.inner.kimi_executor")
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": "data:image/png;base64,..."},
                {"type": "text", "text": "what's in this image?"},
            ],
        }
    ]
    assert _latest_user_text(messages) == "what's in this image?"
    assert any("dropped 1 non-text content block" in rec.message for rec in caplog.records)


def test_latest_user_text_returns_empty_when_no_user_message() -> None:
    assert _latest_user_text([{"role": "assistant", "content": "hi"}]) == ""


def test_resolve_skills_dirs_valid() -> None:
    payload = json.dumps(["/x/skills", "/y/skills"])
    assert _resolve_skills_dirs(payload) == ["/x/skills", "/y/skills"]


def test_resolve_skills_dirs_unset() -> None:
    assert _resolve_skills_dirs(None) == []
    assert _resolve_skills_dirs("") == []
    assert _resolve_skills_dirs("   ") == []


def test_resolve_skills_dirs_invalid_json() -> None:
    assert _resolve_skills_dirs("{not-json") == []


def test_resolve_skills_dirs_wrong_shape() -> None:
    assert _resolve_skills_dirs(json.dumps("scalar")) == []
    assert _resolve_skills_dirs(json.dumps([1, 2])) == []


def test_session_resume_regex_captures_session_id() -> None:
    line = "To resume this session: kimi -r session_1fac96e7-5223-4021-9bf4-6413bedf38ee"
    m = _SESSION_RESUME_RE.search(line)
    assert m is not None
    assert m.group(1) == "session_1fac96e7-5223-4021-9bf4-6413bedf38ee"


def test_session_resume_regex_no_match() -> None:
    assert _SESSION_RESUME_RE.search("nothing to see here") is None


# ---------------------------------------------------------------------------
# Argv builder
# ---------------------------------------------------------------------------


def test_build_argv_minimal() -> None:
    ex = KimiExecutor(binary_path="kimi")
    argv = ex._build_argv(prompt_text="hi")
    assert argv[0] == "kimi"
    assert argv[1:3] == ["--output-format", "stream-json"]
    # ``-p`` always lands at the tail (it consumes a single argument).
    assert argv[-2:] == ["-p", "hi"]
    # No --print, --yolo, --afk, --thinking, --work-dir on the upstream binary.
    for flag in ("--print", "--yolo", "--afk", "--thinking", "--no-thinking", "--work-dir"):
        assert flag not in argv


def test_build_argv_threads_model() -> None:
    ex = KimiExecutor(binary_path="kimi", model="kimi-k2-turbo")
    argv = ex._build_argv(prompt_text="hi")
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "kimi-k2-turbo"


def test_build_argv_plan_flag() -> None:
    ex = KimiExecutor(binary_path="kimi", plan=True)
    argv = ex._build_argv(prompt_text="hi")
    assert "--plan" in argv


def test_build_argv_session_resume() -> None:
    ex = KimiExecutor(binary_path="kimi")
    ex._session_id = "session_deadbeef-1234-5678-9abc-def012345678"
    argv = ex._build_argv(prompt_text="next")
    assert "-S" in argv
    assert argv[argv.index("-S") + 1] == "session_deadbeef-1234-5678-9abc-def012345678"


def test_build_argv_continue_last_when_no_session_id() -> None:
    ex = KimiExecutor(binary_path="kimi", continue_last_session=True)
    argv = ex._build_argv(prompt_text="next")
    assert "-C" in argv
    assert "-S" not in argv


def test_build_argv_explicit_session_id_wins_over_continue() -> None:
    """``-S <id>`` and ``-C`` are mutually exclusive; the explicit id wins."""
    ex = KimiExecutor(binary_path="kimi", continue_last_session=True)
    ex._session_id = "session_abc"
    argv = ex._build_argv(prompt_text="next")
    assert "-S" in argv
    assert "-C" not in argv


def test_build_argv_skills_dirs_repeats_flag() -> None:
    ex = KimiExecutor(binary_path="kimi", skills_dirs=["/a/skills", "/b/skills"])
    argv = ex._build_argv(prompt_text="hi")
    assert argv.count("--skills-dir") == 2
    skills_positions = [i for i, v in enumerate(argv) if v == "--skills-dir"]
    assert argv[skills_positions[0] + 1] == "/a/skills"
    assert argv[skills_positions[1] + 1] == "/b/skills"


# ---------------------------------------------------------------------------
# Translate event
# ---------------------------------------------------------------------------


def test_translate_event_assistant_text_as_string() -> None:
    """Upstream emits ``content`` as a plain string; emit one TextChunk."""
    ex = KimiExecutor(binary_path="kimi")
    events = ex._translate_event({"role": "assistant", "content": "Hi there!"})
    assert len(events) == 1
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "Hi there!"


def test_translate_event_assistant_empty_string_yields_no_events() -> None:
    ex = KimiExecutor(binary_path="kimi")
    assert ex._translate_event({"role": "assistant", "content": ""}) == []


def test_translate_event_tool_call() -> None:
    ex = KimiExecutor(binary_path="kimi")
    events = ex._translate_event(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "type": "function",
                    "id": "tool_abc",
                    "function": {
                        "name": "Bash",
                        "arguments": '{"command": "ls -la"}',
                    },
                }
            ],
        }
    )
    assert len(events) == 1
    assert isinstance(events[0], ToolCallRequest)
    assert events[0].name == "Bash"
    assert events[0].args == {"command": "ls -la"}
    assert events[0].metadata == {"call_id": "tool_abc"}


def test_translate_event_tool_result() -> None:
    ex = KimiExecutor(binary_path="kimi")
    events = ex._translate_event(
        {
            "role": "tool",
            "content": "total 0\n",
            "tool_call_id": "tool_abc",
        }
    )
    assert len(events) == 1
    assert isinstance(events[0], ToolCallComplete)
    assert events[0].result == "total 0\n"
    assert events[0].metadata == {"call_id": "tool_abc"}


def test_translate_event_meta_captures_session_id() -> None:
    """``role:"meta"`` + ``type:"session.resume_hint"`` updates the executor."""
    ex = KimiExecutor(binary_path="kimi")
    events = ex._translate_event(
        {
            "role": "meta",
            "type": "session.resume_hint",
            "session_id": "session_abc123",
            "command": "kimi -r session_abc123",
        }
    )
    assert events == []  # meta events yield no Omnigent-visible events
    assert ex._session_id == "session_abc123"


def test_translate_event_ignores_unknown_role() -> None:
    ex = KimiExecutor(binary_path="kimi")
    assert ex._translate_event({"role": "system", "content": "x"}) == []


# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------


def test_kimi_executor_capabilities() -> None:
    ex = KimiExecutor(binary_path="kimi")
    assert ex.handles_tools_internally() is True
    assert ex.supports_streaming() is True
    assert ex.supports_tool_calling() is True


# ---------------------------------------------------------------------------
# run_turn end-to-end with stubbed subprocess
# ---------------------------------------------------------------------------


class _FakeStdout:
    """Async-iterable stdout that yields the prepared JSONL lines."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    """Reader returning a single buffered stderr blob then EOF."""

    def __init__(self, blob: bytes) -> None:
        self._blob = blob
        self._done = False

    async def read(self, _n: int) -> bytes:
        if self._done:
            return b""
        self._done = True
        return self._blob


class _FakeProcess:
    """asyncio.subprocess.Process double the tests inject in place of a real spawn."""

    def __init__(self, stdout_lines: list[str], stderr_blob: bytes, returncode: int = 0) -> None:
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr(stderr_blob)
        self._returncode = returncode

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        return self._returncode

    def terminate(self) -> None:  # pragma: no cover — happy path doesn't terminate
        pass

    def kill(self) -> None:  # pragma: no cover
        pass


async def _collect(ex: KimiExecutor, messages: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    async for evt in ex.run_turn(messages=messages, tools=[], system_prompt=""):
        out.append(evt)
    return out


def test_run_turn_streams_text_and_emits_turn_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: assistant text + meta resume_hint → TextChunk + session id captured."""
    stdout_lines = [
        json.dumps({"role": "assistant", "content": "Hi there!"}),
        json.dumps(
            {
                "role": "meta",
                "type": "session.resume_hint",
                "session_id": "session_abc12345-6789",
                "command": "kimi -r session_abc12345-6789",
            }
        ),
    ]
    fake = _FakeProcess(stdout_lines, b"", returncode=0)

    captured_argv: list[str] = []

    async def _fake_spawn(*args: Any, **_kwargs: Any) -> _FakeProcess:
        captured_argv.extend(args)
        return fake

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    ex = KimiExecutor(binary_path="kimi", model="kimi-k2-turbo")
    events = asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))

    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    turn_completes = [e for e in events if isinstance(e, TurnComplete)]
    errors = [e for e in events if isinstance(e, ExecutorError)]

    assert errors == []
    assert [c.text for c in text_chunks] == ["Hi there!"]
    assert turn_completes and turn_completes[0].response == "Hi there!"
    assert ex._session_id == "session_abc12345-6789"
    assert captured_argv[0] == "kimi"
    # No --print on upstream — make sure we don't reintroduce it.
    assert "--print" not in captured_argv
    assert "--output-format" in captured_argv
    assert "stream-json" in captured_argv


def test_run_turn_uses_session_resume_on_second_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the first turn captures a session id, the next spawn passes -S."""
    fake_first = _FakeProcess(
        [
            json.dumps({"role": "assistant", "content": "first"}),
            json.dumps(
                {"role": "meta", "type": "session.resume_hint", "session_id": "session_aaaaa"}
            ),
        ],
        b"",
        returncode=0,
    )
    fake_second = _FakeProcess(
        [
            json.dumps({"role": "assistant", "content": "second"}),
            json.dumps(
                {"role": "meta", "type": "session.resume_hint", "session_id": "session_aaaaa"}
            ),
        ],
        b"",
        returncode=0,
    )
    second_argv: list[str] = []
    calls = {"count": 0}

    async def _fake_spawn(*args: Any, **_kwargs: Any) -> _FakeProcess:
        calls["count"] += 1
        if calls["count"] == 1:
            return fake_first
        second_argv.extend(args)
        return fake_second

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    ex = KimiExecutor(binary_path="kimi")
    asyncio.run(_collect(ex, [{"role": "user", "content": "first"}]))
    asyncio.run(_collect(ex, [{"role": "user", "content": "next"}]))

    assert "-S" in second_argv
    idx = second_argv.index("-S")
    assert second_argv[idx + 1] == "session_aaaaa"


def test_run_turn_falls_back_to_stderr_regex_for_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the meta JSON event is absent, the stderr footer regex picks up the id."""
    fake = _FakeProcess(
        [json.dumps({"role": "assistant", "content": "hi"})],
        b"To resume this session: kimi -r session_fallback-1234\n",
        returncode=0,
    )

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return fake

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    ex = KimiExecutor(binary_path="kimi")
    asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))

    assert ex._session_id == "session_fallback-1234"


def test_run_turn_no_resume_hint_leaves_session_id_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither a meta event nor the stderr footer surfaces a resume id,
    ``_session_id`` stays ``None`` so the next turn omits ``-S`` and starts a
    fresh kimi session — never an invented uuid that upstream might reject."""
    fake = _FakeProcess(
        [json.dumps({"role": "assistant", "content": "hi"})],
        b"",  # no resume footer
        returncode=0,
    )

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return fake

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    ex = KimiExecutor(binary_path="kimi")
    asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))

    assert ex._session_id is None
    # And the next turn's argv must NOT carry -S.
    assert "-S" not in ex._build_argv(prompt_text="next")


def test_run_turn_passes_generous_stream_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subprocess is spawned with a large per-line ``limit=`` so a big
    JSONL line (kimi emits whole messages, not deltas) doesn't overrun
    asyncio's 64 KiB default and crash the turn."""
    fake = _FakeProcess([json.dumps({"role": "assistant", "content": "hi"})], b"", returncode=0)
    captured_kwargs: dict[str, Any] = {}

    async def _fake_spawn(*_args: Any, **kwargs: Any) -> _FakeProcess:
        captured_kwargs.update(kwargs)
        return fake

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    ex = KimiExecutor(binary_path="kimi")
    asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))

    assert captured_kwargs.get("limit", 0) >= 1024 * 1024


def test_sandbox_launch_path_bare_binary_when_no_sandbox() -> None:
    """No os_env (or sandbox=none) → spawn the bare binary, never a launcher."""
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    assert KimiExecutor(binary_path="kimi")._sandbox_launch_path(()) == "kimi"

    os_env = OSEnvSpec(
        type="caller_process", cwd=None, sandbox=OSEnvSandboxSpec(type="none"), fork=False
    )
    ex = KimiExecutor(binary_path="kimi", os_env=os_env)
    assert ex._sandbox_launch_path(()) == "kimi"


def test_sandbox_launch_path_wraps_when_sandbox_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec requesting confinement routes the binary through the platform
    sandbox launcher so kimi's in-process tools run jailed."""
    from omnigent.inner import sandbox as sandbox_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    class _ActivePolicy:
        active = True

    monkeypatch.setattr(sandbox_mod, "resolve_sandbox", lambda *_a, **_k: _ActivePolicy())
    monkeypatch.setattr(sandbox_mod, "with_additional_read_roots", lambda s, _roots: s)
    monkeypatch.setattr(sandbox_mod, "with_additional_write_roots", lambda s, _roots: s)
    monkeypatch.setattr(sandbox_mod, "with_spawn_env_allowlist", lambda s, _names: s)
    monkeypatch.setattr(
        sandbox_mod, "create_exec_launcher", lambda target, _policy: f"LAUNCHER::{target}"
    )

    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt"),
        fork=False,
    )
    ex = KimiExecutor(binary_path="kimi", os_env=os_env)
    launch = ex._sandbox_launch_path(("PATH",))

    assert launch.startswith("LAUNCHER::")


def test_run_turn_emits_error_when_kimi_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: None)
    monkeypatch.setattr(kimi_executor.Path, "exists", lambda _self: False)

    ex = KimiExecutor(binary_path="kimi")
    events = asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors and "not found on PATH" in errors[0].message
    assert errors[0].retryable is False


def test_run_turn_with_empty_user_text_emits_turn_complete_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    async def _never_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("subprocess must not be spawned when prompt is empty")

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _never_called)

    ex = KimiExecutor(binary_path="kimi")
    events = asyncio.run(_collect(ex, [{"role": "assistant", "content": "no user msg"}]))

    assert len(events) == 1
    assert isinstance(events[0], TurnComplete)
    assert events[0].response is None


def test_run_turn_nonzero_exit_yields_executor_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProcess([], b"boom\n", returncode=2)

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return fake

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    ex = KimiExecutor(binary_path="kimi")
    events = asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))

    errors = [e for e in events if isinstance(e, ExecutorError)]
    assert errors and "exited with code 2" in errors[0].message


def test_run_turn_reports_wire_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The turn's ``usage.record`` rows sum into ``TurnComplete.usage``.

    Pinned Kimi Code 0.34.0 wire schema: stream-json stdout carries no usage,
    so the executor reads the persisted wire log after the subprocess exits.
    ``input_tokens`` = Σ inputOther; cache reads/creation and output map to
    their own keys; the effective model comes from ``llm.request`` (the
    provider-resolved id, not the alias). Rows are written at spawn time —
    like real kimi — so the strict turn-start time gate admits them.
    """

    def _rows() -> list[dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        return [
            {
                "type": "llm.request",
                "kind": "loop",
                "provider": "openai",
                "model": "system.ai.kimi-k3",
                "modelAlias": "kimi-k3-databricks",
                "maxTokens": 65536,
                "time": now_ms,
            },
            _usage_row(input_other=20559, output=160, time_ms=now_ms),
            {
                "type": "usage.record",
                "model": "kimi-k3-databricks",
                "usage": {
                    "inputOther": 2975,
                    "output": 76,
                    "inputCacheRead": 17920,
                    "inputCacheCreation": 8,
                },
                "usageScope": "turn",
                "time": now_ms,
            },
        ]

    events, _ex = _run_stubbed_turn(
        monkeypatch,
        tmp_path,
        session_id="session_usage-1",
        on_spawn=lambda: _write_wire(tmp_path, "session_usage-1", _rows()),
    )

    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage == {
        "input_tokens": 20559 + 2975,
        "output_tokens": 160 + 76,
        "cache_read_input_tokens": 17920,
        "cache_creation_input_tokens": 8,
        # The relay path accumulates total_tokens independently (never
        # derived), so it must be reported or the total stays 0 forever.
        "total_tokens": 20559 + 2975 + 160 + 76 + 17920 + 8,
        "model": "system.ai.kimi-k3",
    }


def test_run_turn_second_turn_counts_only_new_wire_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The per-session byte checkpoint makes each turn sum only its own rows."""
    events, ex = _run_stubbed_turn(
        monkeypatch,
        tmp_path,
        session_id="session_ckpt-1",
        on_spawn=lambda: _write_wire(
            tmp_path,
            "session_ckpt-1",
            [_usage_row(input_other=100, output=10, time_ms=int(time.time() * 1000))],
        ),
    )
    first = next(e for e in events if isinstance(e, TurnComplete))
    assert first.usage is not None
    assert first.usage["input_tokens"] == 100

    wire = _wire_path(tmp_path, "session_ckpt-1")

    def _append_second() -> None:
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(_usage_row(input_other=7, output=3, time_ms=int(time.time() * 1000)))
                + "\n"
            )

    events2 = _collect_stubbed_turn(
        monkeypatch, ex, session_id="session_ckpt-1", on_spawn=_append_second
    )
    second = next(e for e in events2 if isinstance(e, TurnComplete))
    assert second.usage is not None
    assert second.usage["input_tokens"] == 7
    assert second.usage["output_tokens"] == 3


def test_run_turn_resumed_session_skips_prior_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first read of a resumed (-S/-C) session's wire log is STRICTLY
    time-gated at turn start: history written even milliseconds before the
    resume is never re-billed; only rows stamped during the turn count."""
    # Prewritten history, stamped one millisecond in the past — the smallest
    # representable "before the resume". The old 10s backward allowance would
    # have re-billed it.
    _write_wire(
        tmp_path,
        "session_resume-1",
        [_usage_row(input_other=99999, output=9999, time_ms=int(time.time() * 1000) - 1)],
    )

    def _append_current() -> None:
        wire = _wire_path(tmp_path, "session_resume-1")
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(_usage_row(input_other=11, output=2, time_ms=int(time.time() * 1000)))
                + "\n"
            )

    events, _ex = _run_stubbed_turn(
        monkeypatch, tmp_path, session_id="session_resume-1", on_spawn=_append_current
    )
    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None
    assert turn.usage["input_tokens"] == 11
    assert turn.usage["output_tokens"] == 2


def test_run_turn_known_session_seeds_checkpoint_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A known session id with no checkpoint snapshots the wire EOF pre-spawn.

    Everything already in the file predates the spawn (e.g. a prior run in
    another process), so a turn that produces no new records reports no usage
    instead of billing the backlog.
    """
    _write_wire(
        tmp_path,
        "session_seed-1",
        [_usage_row(input_other=99999, output=9999, time_ms=int(time.time() * 1000))],
    )
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path))
    ex = KimiExecutor(binary_path="kimi")
    ex._session_id = "session_seed-1"

    events = _collect_stubbed_turn(monkeypatch, ex, session_id="session_seed-1")

    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is None


def test_run_turn_missing_wire_log_reports_no_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No wire log for the session (or none yet) → usage None, never an error —
    and the extraction recovers once the log appears on a later turn."""
    import logging

    caplog.set_level(logging.WARNING, logger="omnigent.inner.kimi_executor")
    (tmp_path / "sessions").mkdir()
    events, ex = _run_stubbed_turn(monkeypatch, tmp_path, session_id="session_missing-1")
    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is None
    assert not [e for e in events if isinstance(e, ExecutorError)]
    assert any("no wire log found" in r.message for r in caplog.records)

    # The wire log shows up during a later turn (kimi created the session
    # mid-run): that turn reports usage — proving the None above was the
    # missing file, not a missing feature.
    events2 = _collect_stubbed_turn(
        monkeypatch,
        ex,
        session_id="session_missing-1",
        on_spawn=lambda: _write_wire(
            tmp_path,
            "session_missing-1",
            [_usage_row(input_other=6, output=2, time_ms=int(time.time() * 1000))],
        ),
    )
    turn2 = next(e for e in events2 if isinstance(e, TurnComplete))
    assert turn2.usage is not None
    assert turn2.usage["input_tokens"] == 6


def test_run_turn_malformed_wire_rows_are_skipped_whole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Garbage lines and schema-drifted records are skipped WHOLE (never
    partially counted) and never crash the turn; only records matching the
    pinned 0.34.0 schema count."""

    def _write_mixed() -> None:
        now_ms = int(time.time() * 1000)
        wire = _write_wire(
            tmp_path,
            "session_mal-1",
            [
                # Drifted: usage is not a dict.
                {
                    "type": "usage.record",
                    "model": "m",
                    "usage": "not-a-dict",
                    "usageScope": "turn",
                    "time": now_ms,
                },
                # Drifted: missing count field — must not count partially.
                {
                    "type": "usage.record",
                    "model": "m",
                    "usage": {"inputOther": 500, "output": 500, "inputCacheRead": 0},
                    "usageScope": "turn",
                    "time": now_ms,
                },
                # Drifted: missing model.
                {
                    "type": "usage.record",
                    "usage": {
                        "inputOther": 500,
                        "output": 500,
                        "inputCacheRead": 0,
                        "inputCacheCreation": 0,
                    },
                    "usageScope": "turn",
                    "time": now_ms,
                },
                # Wrong scope.
                {
                    "type": "usage.record",
                    "model": "m",
                    "usage": {
                        "inputOther": 5,
                        "output": 5,
                        "inputCacheRead": 0,
                        "inputCacheCreation": 0,
                    },
                    "usageScope": "aggregate",
                    "time": now_ms,
                },
                # Valid.
                _usage_row(input_other=4, output=1, time_ms=now_ms),
            ],
        )
        with wire.open("a", encoding="utf-8") as fh:
            fh.write("this is not json\n{truncated\n")

    import logging

    caplog.set_level(logging.WARNING, logger="omnigent.inner.kimi_executor")
    events, _ex = _run_stubbed_turn(
        monkeypatch, tmp_path, session_id="session_mal-1", on_spawn=_write_mixed
    )
    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None
    assert turn.usage["input_tokens"] == 4
    assert turn.usage["output_tokens"] == 1
    # Drifted records and non-JSON lines each leave a once-per-file diagnostic.
    assert any("not matching the pinned" in r.message for r in caplog.records)
    assert any("unparseable wire row" in r.message for r in caplog.records)


def test_run_turn_model_falls_back_to_usage_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no usable llm.request after the checkpoint, the counted record's
    own model still attributes the tokens."""
    events, _ex = _run_stubbed_turn(
        monkeypatch,
        tmp_path,
        session_id="session_recmodel-1",
        on_spawn=lambda: _write_wire(
            tmp_path,
            "session_recmodel-1",
            [_usage_row(input_other=9, output=1, time_ms=int(time.time() * 1000))],
        ),
    )
    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None
    assert turn.usage["model"] == "kimi-k3-databricks"


def test_run_turn_historical_request_model_does_not_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resumed log's historical llm.request must not attribute this turn:
    only turn-window requests count, so the counted record's own model wins
    when no current request exists."""
    _write_wire(
        tmp_path,
        "session_histreq-1",
        [
            {
                "type": "llm.request",
                "kind": "loop",
                "model": "system.ai.stale-model",
                "modelAlias": "stale-alias",
                "time": int(time.time() * 1000) - 1,
            }
        ],
    )

    def _append_current_usage() -> None:
        wire = _wire_path(tmp_path, "session_histreq-1")
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(_usage_row(input_other=9, output=1, time_ms=int(time.time() * 1000)))
                + "\n"
            )

    events, _ex = _run_stubbed_turn(
        monkeypatch, tmp_path, session_id="session_histreq-1", on_spawn=_append_current_usage
    )
    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is not None
    assert turn.usage["model"] == "kimi-k3-databricks"


def test_run_turn_survives_homeless_environment_when_seeding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-spawn checkpoint seeding is best-effort: a HOME-less environment
    (resolve_user_kimi_home → Path.home() raises RuntimeError) must not fail
    the turn."""
    monkeypatch.setattr(
        kimi_executor,
        "resolve_user_kimi_home",
        lambda: (_ for _ in ()).throw(RuntimeError("no HOME")),
    )
    ex = KimiExecutor(binary_path="kimi")
    ex._session_id = "session_nohome-1"

    events = _collect_stubbed_turn(monkeypatch, ex, session_id="session_nohome-1")

    assert not [e for e in events if isinstance(e, ExecutorError)]
    turn = next(e for e in events if isinstance(e, TurnComplete))
    assert turn.usage is None


def test_sum_wire_usage_unreadable_file_logs_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable wire log reports no usage and leaves one diagnostic."""
    import logging

    from omnigent.inner.kimi_executor import _sum_wire_usage

    caplog.set_level(logging.WARNING, logger="omnigent.inner.kimi_executor")
    unreadable = tmp_path / "wire.jsonl"
    unreadable.mkdir()  # opening a directory as a file raises OSError

    assert _sum_wire_usage(unreadable, offset=0, turn_start_ms=0) == (None, None, 0)
    assert _sum_wire_usage(unreadable, offset=0, turn_start_ms=0) == (None, None, 0)

    warnings = [r for r in caplog.records if "cannot read wire log" in r.message]
    assert len(warnings) == 1


def _usage_row(*, input_other: int, output: int, time_ms: int) -> dict[str, Any]:
    return {
        "type": "usage.record",
        "model": "kimi-k3-databricks",
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": 0,
            "inputCacheCreation": 0,
        },
        "usageScope": "turn",
        "time": time_ms,
    }


def _wire_path(home: Path, session_id: str) -> Path:
    return home / "sessions" / "wd_x" / session_id / "agents" / "main" / "wire.jsonl"


def _write_wire(home: Path, session_id: str, rows: list[dict[str, Any]]) -> Path:
    """Create ``<home>/sessions/wd_x/<session_id>/agents/main/wire.jsonl``."""
    wire = _wire_path(home, session_id)
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return wire


def _run_stubbed_turn(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    *,
    session_id: str,
    on_spawn: Callable[[], object] | None = None,
) -> tuple[list[Any], KimiExecutor]:
    """Run one stubbed turn whose resume hint reports *session_id*."""
    monkeypatch.setenv("KIMI_CODE_HOME", str(home))
    ex = KimiExecutor(binary_path="kimi")
    events = _collect_stubbed_turn(monkeypatch, ex, session_id=session_id, on_spawn=on_spawn)
    return events, ex


def _collect_stubbed_turn(
    monkeypatch: pytest.MonkeyPatch,
    ex: KimiExecutor,
    *,
    session_id: str,
    on_spawn: Callable[[], object] | None = None,
) -> list[Any]:
    """One stubbed turn; *on_spawn* writes wire rows at spawn time (like kimi)."""
    fake = _FakeProcess(
        [
            json.dumps({"role": "assistant", "content": "done"}),
            json.dumps({"role": "meta", "type": "session.resume_hint", "session_id": session_id}),
        ],
        b"",
        returncode=0,
    )

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        if on_spawn is not None:
            on_spawn()
        return fake

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")
    return asyncio.run(_collect(ex, [{"role": "user", "content": "hi"}]))


def test_run_turn_warns_once_when_tools_declared(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tools on the spec are silently dropped (no MCP bridge on upstream kimi
    yet) — we should warn exactly once per session.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="omnigent.inner.kimi_executor")

    def _make_fake() -> _FakeProcess:
        return _FakeProcess(
            [
                json.dumps({"role": "assistant", "content": "ok"}),
                json.dumps(
                    {"role": "meta", "type": "session.resume_hint", "session_id": "session_x"}
                ),
            ],
            b"",
            returncode=0,
        )

    fakes = [_make_fake(), _make_fake()]

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        return fakes.pop(0)

    monkeypatch.setattr(kimi_executor, "_create_subprocess_exec", _fake_spawn)
    monkeypatch.setattr(kimi_executor.shutil, "which", lambda _binary: "/usr/local/bin/kimi")

    ex = KimiExecutor(binary_path="kimi")
    tools = [{"name": "my_tool", "description": "x", "parameters": {}}]

    async def _two_turns() -> None:
        async for _ in ex.run_turn(
            messages=[{"role": "user", "content": "hi"}], tools=tools, system_prompt=""
        ):
            pass
        async for _ in ex.run_turn(
            messages=[{"role": "user", "content": "again"}], tools=tools, system_prompt=""
        ):
            pass

    asyncio.run(_two_turns())

    warnings = [rec for rec in caplog.records if "tool-injection bridge" in rec.message]
    assert len(warnings) == 1, "should warn exactly once across both turns"
