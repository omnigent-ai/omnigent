"""E2E regression: a Codex turn on an account-unsupported model surfaces the
raw upstream 400 as an opaque ``inner executor error`` failure.

User journey (harness ``codex`` -- the ``codex app-server`` executor that renders
in the web chat):

1. A user runs a Codex session whose configured model the account does not allow
   -- here ``gpt-6-astra`` on a ChatGPT-account-backed Codex provider.
2. The user sends a message; the Codex app-server issues the Responses request.
3. The provider rejects it with HTTP ``400 invalid_request_error``::

       The 'gpt-6-astra' model is not supported when using Codex with a ChatGPT account.

4. Observable failure: the turn fails and surfaces to the chat UI as
   ``turn surfaced to UI as failed for ... (harness=codex): {'code':
   'runner_error', 'message': 'inner executor error: {"type":"error",
   "status":400,"error":{"type":"invalid_request_error","message":"The
   'gpt-6-astra' model is not supported ..."}}'}`` -- the raw provider JSON
   error envelope is dumped verbatim rather than surfaced as a useful,
   structured reason.

This drives the REAL ``codex app-server`` via :class:`CodexExecutor` (the
``harness=codex`` path) against a mock Responses endpoint that returns the exact
production 400 -- injecting the upstream fault that a ChatGPT account produces
for an unsupported model. The mock stands in for the real ChatGPT-account
provider; the Omnigent surfacing code (``CodexExecutor``'s ``method == "error"``
handler and the ``ExecutorAdapter`` ``inner executor error:`` wrapping in
``omnigent/runtime/harnesses/_executor_adapter.py``) runs for real and produces
the exact reported message.

The ``ExecutorError`` this executor yields is what
``ExecutorAdapter`` wraps verbatim into
``RuntimeError(f"inner executor error: {detail}")`` and the runner/server then
publish as the failed turn -- so asserting on ``ExecutorError.message`` asserts
on the text a user ultimately sees in chat.

Regression guard (holds before and after a fix): the turn genuinely fails on an
unsupported model and the upstream reason is preserved. Fail->pass target
(reproduces today; a fix flips it): the surfaced failure must carry a useful,
structured reason -- not the raw provider JSON error envelope dumped verbatim.

Usage::

    pytest tests/e2e/test_codex_unsupported_model_e2e.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.spec.types import RetryPolicy

# The exact model and rejection string quoted in the bug report.
_UNSUPPORTED_MODEL = "gpt-6-astra"
_UNSUPPORTED_REASON = (
    f"The '{_UNSUPPORTED_MODEL}' model is not supported when using Codex with a ChatGPT account."
)
# The provider's verbatim 400 body (the ChatGPT-account rejection for an
# unsupported model). This is the upstream fault the account produces.
_ERROR_BODY = {
    "type": "error",
    "status": 400,
    "error": {"type": "invalid_request_error", "message": _UNSUPPORTED_REASON},
}

# The Codex CLI must support the mocked app-server transport (gateway mode).
_CODEX_MIN_VERSION = (0, 139, 0)


def _codex_bin_or_skip() -> str:
    """Return a Codex CLI path new enough for the mocked app-server, else skip."""
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the unsupported-model e2e")
    probe = subprocess.run([codex_path, "--version"], text=True, capture_output=True, check=False)
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", f"{probe.stdout}\n{probe.stderr}")
    if probe.returncode != 0 or not match:
        pytest.skip("could not determine codex CLI version")
    if tuple(int(part) for part in match.groups()) < _CODEX_MIN_VERSION:
        pytest.skip("codex CLI >= 0.139.0 is required for the mocked app-server e2e")
    return codex_path


class _UnsupportedModelHandler(BaseHTTPRequestHandler):
    """Mock Responses provider: reject every turn with the account's 400."""

    def do_GET(self) -> None:
        # The Codex app-server refreshes its model list on startup; answer it
        # so the turn proceeds to the /responses call that carries the 400.
        payload = json.dumps({"models": [{"id": _UNSUPPORTED_MODEL, "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        payload = json.dumps(_ERROR_BODY).encode()
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def unsupported_model_provider() -> Iterator[str]:
    """A local mock Responses endpoint that 400s every turn. Yields its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UnsupportedModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _run_turn(executor: CodexExecutor, prompt: str) -> list[object]:
    events: list[object] = []
    async for event in executor.run_turn(
        [{"role": "user", "content": prompt, "session_id": "session-1"}],
        [],
        "You are a test assistant.",
    ):
        events.append(event)
    return events


@pytest.mark.timeout(180)
async def test_codex_unsupported_model_surfaces_structured_reason(
    unsupported_model_provider: str,
    tmp_path: Path,
) -> None:
    """A Codex turn on an unsupported model surfaces a useful reason, not a blob."""
    codex_bin = _codex_bin_or_skip()

    codex_home = tmp_path / "source-codex-home"
    codex_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Real ``codex app-server`` (harness=codex) routed at the mock provider, with
    # the account-unsupported model pinned exactly as the bug reports. Retries are
    # pinned off for determinism: a 400 invalid_request_error is a permanent
    # client error and is never retried in production either.
    executor = CodexExecutor(
        codex_path=codex_bin,
        cwd=str(workspace),
        gateway=True,
        gateway_host="http://127.0.0.1",
        base_url_override=unsupported_model_provider,
        gateway_auth_command="printf %s dummy",
        model=_UNSUPPORTED_MODEL,
        enable_web_search=False,
        skills_filter="none",
        retry_policy=RetryPolicy(max_retries=0),
    )
    # CODEX_HOME must exist before the app-server spawns.
    executor._env["CODEX_HOME"] = str(codex_home)

    try:
        events = await _run_turn(executor, "say hi")
    finally:
        await executor.close()

    completions = [event for event in events if isinstance(event, TurnComplete)]
    errors = [event for event in events if isinstance(event, ExecutorError)]

    # Regression guard: the turn genuinely fails on an unsupported model and the
    # upstream reason is preserved (must hold before AND after any fix).
    assert not completions, f"expected the turn to fail, got a completion: {events}"
    assert len(errors) == 1, f"expected exactly one executor error, got: {events}"
    surfaced = errors[0].message
    assert _UNSUPPORTED_REASON in surfaced, (
        "the upstream unsupported-model reason must be preserved in the failure; "
        f"got: {surfaced!r}"
    )

    # Fail->pass target (reproduces on the current build; a fix flips it): the
    # user-facing failure must carry a useful, STRUCTURED reason -- not the raw
    # provider JSON error envelope dumped verbatim. Today the message is the raw
    # envelope, which reaches the chat as
    # ``inner executor error: {"type":"error","status":400,...}``.
    assert not surfaced.lstrip().startswith("{"), (
        "the surfaced failure is the raw provider JSON error envelope, not a "
        f"useful structured reason: {surfaced!r}"
    )
    assert '"invalid_request_error"' not in surfaced, (
        "the raw provider error envelope leaked into the user-facing failure "
        f"instead of a clean reason: {surfaced!r}"
    )
