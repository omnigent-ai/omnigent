"""Inline prompt preservation and Pi's independent workspace-context discovery.

The bundle tests need no Pi installation. The process tests use the real Pi CLI
with an isolated config and a local mock LLM, asserting requests, not replies.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from omnigent.chat import _bundle_agent
from omnigent.inner import pi_executor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.runtime.prompt import build_instructions
from omnigent.spec import AgentSpec, load

_INLINE_MARKER = "PI_INLINE_PROMPT_42D9"
_WORKSPACE_MARKER = "PI_WORKSPACE_CONTEXT_7B21"
_BUNDLE_MARKER = "PI_BUNDLE_CONTEXT_5F13"
_GLOBAL_MARKER = "PI_GLOBAL_CONTEXT_8C14"
_FRAMEWORK_MARKER = "PI_FRAMEWORK_INSTRUCTIONS_6A29"


def _uploaded_spec(tmp_path: Path, *, directory: bool) -> tuple[AgentSpec, Path]:
    """Round-trip the CLI bundle with an inline prompt and a sibling context file."""
    source = tmp_path / "agent"
    source.mkdir()
    yaml_path = source / "agent.yaml"
    yaml_path.write_text(
        "name: pi-prompt-context\n"
        "executor:\n  harness: pi\n"
        "skills: none\n"
        f"prompt: Always include {_INLINE_MARKER} in replies.\n",
        encoding="utf-8",
    )
    (source / "AGENTS.md").write_text(_BUNDLE_MARKER, encoding="utf-8")
    bundle = _bundle_agent(source if directory else yaml_path)
    extracted = tmp_path / "uploaded"
    return load(bundle, dest=extracted), extracted


@pytest.mark.parametrize("directory", [False, True], ids=["yaml", "directory"])
def test_inline_prompt_survives_bundle_with_sibling_context(
    tmp_path: Path, directory: bool
) -> None:
    """An explicit prompt survives upload without a sibling AGENTS.md replacing it."""
    spec, _ = _uploaded_spec(tmp_path, directory=directory)

    assert spec.executor.config["harness"] == "pi"
    assert spec.instructions == f"Always include {_INLINE_MARKER} in replies."
    composed = build_instructions(spec, None, [])
    assert _INLINE_MARKER in composed
    assert _BUNDLE_MARKER not in composed


@pytest.fixture
def captured_llm_requests() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Capture OpenAI-compatible requests and return a fixed, marker-free reply."""
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            chunk = {
                "id": "chatcmpl-prompt-probe",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "probe-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "Captured."},
                        "finish_reason": "stop",
                    }
                ],
            }
            payload = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.timeout(45)
@pytest.mark.parametrize("context_files", [True, False], ids=["enabled", "disabled"])
@pytest.mark.parametrize(
    ("context_path", "expect_context"),
    [(None, False), ("AGENT.md", False), ("AGENTS.md", True), ("workspace/AGENTS.md", True)],
    ids=["absent", "singular-filename", "ancestor", "workspace"],
)
async def test_real_pi_adds_workspace_context_after_inline_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_llm_requests: tuple[str, list[dict[str, Any]]],
    context_path: str | None,
    expect_context: bool,
    context_files: bool,
) -> None:
    """Inspect the actual model request after bundling, composition, and Pi startup."""
    pi_path = os.environ.get("OMNIGENT_PI_PATH") or shutil.which("pi")
    if not pi_path:
        pytest.skip("real Pi CLI is required for the prompt-context process test")
    spec, bundle_dir = _uploaded_spec(tmp_path, directory=True)
    project = tmp_path / "project"
    workspace = project / "workspace"
    workspace.mkdir(parents=True)
    if context_path is not None:
        (project / context_path).write_text(_WORKSPACE_MARKER, encoding="utf-8")

    base_url, requests = captured_llm_requests
    # Pi resolves the home directory at startup; an isolated HOME keeps that
    # working on hosts whose uid has no passwd entry, without leaking ~/.pi.
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    agent_dir = tmp_path / "pi-config"
    agent_dir.mkdir()
    (agent_dir / "AGENTS.md").write_text(_GLOBAL_MARKER, encoding="utf-8")
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "local-probe": {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": "dummy-local-key",
                        "models": [{"id": "probe-model", "reasoning": False}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    # Isolate provider credentials, global extensions, and Pi settings.
    monkeypatch.setattr(
        pi_executor,
        "_clean_pi_env",
        lambda *_: {
            "PATH": os.environ["PATH"],
            "HOME": str(home_dir),
            "PI_CODING_AGENT_DIR": str(agent_dir),
        },
    )
    executor = pi_executor.PiExecutor(
        pi_path=pi_path,
        cwd=str(workspace),
        bundle_dir=bundle_dir,
        model="local-probe/probe-model",
        skills_filter=spec.skills_filter,
        context_files=context_files,
    )
    try:
        async with asyncio.timeout(30):
            events = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "hello"}],
                    [],
                    build_instructions(spec, None, [], framework_instructions=[_FRAMEWORK_MARKER]),
                )
            ]
    finally:
        await executor.close()

    assert not [event for event in events if isinstance(event, ExecutorError)]
    assert any(isinstance(event, TurnComplete) for event in events)
    assert len(requests) == 1
    system = "\n".join(
        message["content"]
        for message in requests[0]["messages"]
        if message["role"] in ("system", "developer")
    )
    assert _INLINE_MARKER in system
    assert _FRAMEWORK_MARKER in system
    assert (_GLOBAL_MARKER in system) is context_files
    assert (_WORKSPACE_MARKER in system) is (expect_context and context_files)
    if expect_context and context_files:
        assert system.index(_INLINE_MARKER) < system.index(_WORKSPACE_MARKER)
