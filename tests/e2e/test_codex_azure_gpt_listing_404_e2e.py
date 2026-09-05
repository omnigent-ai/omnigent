"""E2E: an Azure listing that advertises unserved GPT models must not 404 the first codex turn.

On Azure workspaces the AI Gateway's Unity Catalog
model-services listing advertises GPT models even though the workspace does
not serve them -- POSTing a turn to the Codex Responses route returns
``404 RESOURCE_DOES_NOT_EXIST``. Omnigent's codex launch trusts that listing
(:func:`omnigent.databricks_model_discovery.discover_databricks_codex_models`)
and pins the top-ranked GPT id as the launch default, so the user's very
first ucode turn errors with the gateway 404 -- even though the same
workspace serves another codex-compatible model that would have worked.

The journey is the user's own, driven end to end through a real codex CLI:

1. The user's ``azure`` profile is healthy -- the fake ``databricks`` CLI on
   PATH mints a token the workspace accepts.
2. The workspace's model-services listing advertises GPT models it does not
   serve (Azure behavior), alongside a codex-compatible model it does serve.
3. The user launches ucode with no explicit model; omnigent resolves the
   launch default from the listing (the exact resolution the codex-native
   launch runs) and runs the first turn on it.

Expected: the first turn completes -- omnigent must not pin a model the
workspace refuses to serve when a servable one exists. Before a fix the
launch pins ``system.ai.gpt-5-6-sol`` and the turn dies with the gateway's
``404 RESOURCE_DOES_NOT_EXIST``.

Self-contained: mocks only the workspace HTTP endpoints and the
``databricks`` CLI binary; the codex CLI, the executor, the discovery, and
the launch-default resolution are all real. Requires no server, no
credentials, and no network.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.spec.types import RetryPolicy
from tests.e2e._harness_probes import cli_unavailable_reason

FRESH_TOKEN = "fresh-profile-token"

# What the Azure workspace's listing advertises: two GPT ids the gateway
# will 404 (the reported Azure behavior), one codex-compatible id it truly
# serves, and a Claude id on the Anthropic surface (not codex-servable).
_LISTING = {
    "model_services": [
        {
            "name": "model-services/system.ai.gpt-5-6-sol",
            "supported_api_types": ["openai/v1/responses"],
        },
        {
            "name": "model-services/system.ai.gpt-5-5",
            "supported_api_types": ["openai/v1/responses"],
        },
        {
            "name": "model-services/system.ai.glm-5-2",
            "supported_api_types": ["openai/v1/responses"],
        },
        {
            "name": "model-services/system.ai.claude-sonnet-4-5",
            "supported_api_types": ["anthropic/v1/messages"],
        },
    ]
}

_SERVED_MODEL = "system.ai.glm-5-2"


class _FakeAzureWorkspace(http.server.ThreadingHTTPServer):
    """Loopback stand-in for an Azure Databricks workspace + AI Gateway.

    The Unity Catalog model-services listing advertises GPT models, but the
    Codex Responses route 404s every GPT turn (Azure does not serve them).
    ``system.ai.glm-5-2`` is genuinely served, so a launch that picks a
    servable model completes.
    """

    def __init__(self) -> None:
        self.responses_models_seen: list[str] = []
        super().__init__(("127.0.0.1", 0), _FakeAzureWorkspaceHandler)

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _FakeAzureWorkspaceHandler(http.server.BaseHTTPRequestHandler):
    server: _FakeAzureWorkspace

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if "/unity-catalog/model-services" in self.path:
            self._send(200, "application/json", json.dumps(_LISTING).encode())
            return
        # codex polls /models on startup; an empty list keeps it quiet.
        self._send(200, "application/json", json.dumps({"models": []}).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            model = json.loads(body).get("model", "")
        except (ValueError, AttributeError):
            model = ""
        self.server.responses_models_seen.append(model)
        if model == _SERVED_MODEL:
            self._send(200, "text/event-stream", _sse_text_response("hello from the gateway"))
            return
        # The workspace's real-world Azure rejection for advertised GPT ids.
        self._send(
            404,
            "application/json",
            json.dumps(
                {
                    "error_code": "RESOURCE_DOES_NOT_EXIST",
                    "message": (f"Model '{model}' is not available in this workspace region."),
                }
            ).encode(),
        )

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


def _sse_text_response(text: str) -> bytes:
    """Minimal Responses-API SSE stream: created -> message -> completed."""
    message_item = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    completed = {
        "id": "resp-1",
        "object": "response",
        "status": "completed",
        "output": [message_item],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": None,
            "output_tokens": 1,
            "output_tokens_details": None,
            "total_tokens": 2,
        },
    }
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"response": {"id": "resp-1"}}),
        ("response.output_item.done", {"item": message_item}),
        ("response.completed", {"response": completed}),
    ]
    return "".join(
        f"event: {evt}\ndata: {json.dumps({'type': evt, **payload})}\n\n"
        for evt, payload in events
    ).encode()


def _install_azure_profile(tmp_path: Path, host: str) -> tuple[Path, Path]:
    """Set up a healthy ``azure`` profile whose mint always works.

    Returns (bin dir with the fake ``databricks`` CLI, cfg path).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_cli = bindir / "databricks"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in *--help*) echo "usage"; exit 0;; esac\n'
        f'echo \'{{"access_token":"{FRESH_TOKEN}"}}\'\n'
    )
    fake_cli.chmod(0o755)
    cfg = tmp_path / "databrickscfg"
    cfg.write_text(f"[azure]\nhost = {host}\ntoken = {FRESH_TOKEN}\n")
    return bindir, cfg


async def _run_codex_turn(workspace: Path, model: str) -> list[Any]:
    """Run one real codex turn on the Databricks gateway and collect events."""
    executor = CodexExecutor(
        cwd=str(workspace),
        gateway=True,
        databricks_profile="azure",
        model=model,
        enable_web_search=False,
        skills_filter="none",
        retry_policy=RetryPolicy(max_retries=1, backoff_base_s=0.1, backoff_max_s=0.5),
    )
    events: list[Any] = []
    try:
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello?", "session_id": "session-1"}],
            [],
            "You are a test assistant.",
        ):
            events.append(event)
    finally:
        await executor.close()
    return events


@pytest.mark.posix_only
@pytest.mark.timeout(300)
async def test_azure_gpt_listing_does_not_404_first_codex_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The codex launch default must be a model the workspace actually serves.

    The Azure listing advertises GPT ids the gateway 404s alongside a
    codex-compatible id it serves. Omnigent resolves the launch default from
    that listing (exactly what the ucode/codex-native launch runs) and the
    first turn runs on the pinned model. The only way this turn can fail is
    omnigent pinning an advertised-but-unserved GPT model -- the regression
    this guards: the user's first ucode turn on Azure dies with the gateway's
    ``404 RESOURCE_DOES_NOT_EXIST``.
    """
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    gateway = _FakeAzureWorkspace()
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        bindir, cfg = _install_azure_profile(tmp_path, gateway.host)

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()

        # Isolate from any ambient Databricks state on the test machine.
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
        monkeypatch.delenv("DATABRICKS_BEARER", raising=False)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        # Keep any corporate proxy out of the loopback gateway path.
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

        # Rig sanity: the served model completes on this exact stack, so a
        # failure below is model-selection, not harness or rig breakage.
        control_events = await _run_codex_turn(workspace, _SERVED_MODEL)
        control_completions = [e for e in control_events if isinstance(e, TurnComplete)]
        assert control_completions, (
            "control turn on the served model failed; the rig (codex CLI, "
            f"executor, fake workspace) is broken: {control_events}"
        )

        # The launch-default resolution the ucode/codex-native launch runs
        # with no explicit model: pick from the workspace's live listing.
        from omnigent.codex_native_app_server import _resolve_databricks_codex_model

        launch_model = _resolve_databricks_codex_model(gateway.host, "azure", None)

        gateway.responses_models_seen.clear()
        events = await _run_codex_turn(workspace, launch_model)
        errors = [e for e in events if isinstance(e, ExecutorError)]
        completions = [e for e in events if isinstance(e, TurnComplete)]
        assert not errors, (
            f"the codex launch pinned {launch_model!r} from the Azure listing "
            "and the first turn died on the gateway rejection: "
            f"{errors[0].message!r}; the same workspace serves "
            f"{_SERVED_MODEL!r}, so a servable launch default exists; "
            f"gateway saw models: {gateway.responses_models_seen}"
        )
        assert len(completions) == 1, events
    finally:
        gateway.shutdown()
        gateway.server_close()
