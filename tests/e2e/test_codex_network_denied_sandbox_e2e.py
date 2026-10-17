"""A codex agent whose bwrap sandbox denies network must still complete a turn.

With no signer and no ``egress_rules``, an app-server wrapped in that sandbox
has no route to the model and the turn never answers. Drives the real ``codex``
CLI through :class:`CodexExecutor` against the mock model server, so it skips
where the CLI or bubblewrap is unavailable. The fixed worker runs unwrapped, so
user namespaces are not required.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import configure_mock_llm

pytestmark = [
    pytest.mark.skipif(
        (_codex_reason := cli_unavailable_reason("codex")) is not None,
        reason=f"requires a runnable 'codex' CLI; {_codex_reason}",
    ),
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
        reason="linux_bwrap requires Linux with bubblewrap installed",
    ),
]

_MODEL = "mock-network-denied"
_TURN_BUDGET_S = 90.0


@pytest.mark.asyncio
async def test_network_denied_bwrap_codex_agent_completes_a_turn(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": "OK"}] * 3, key=_MODEL)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("workspace\n")
    executor = CodexExecutor(
        cwd=str(workspace),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(
                type="linux_bwrap",
                write_paths=["."],
                allow_network=False,
            ),
        ),
        model=_MODEL,
        gateway=True,
        gateway_host=isolated_mock_llm_server_url,
        base_url_override=f"{isolated_mock_llm_server_url}/v1",
        gateway_auth_command="printf %s mock-key",
        enable_web_search=False,
        disable_native_tools=True,
    )
    events: list[object] = []

    async def _collect() -> None:
        async for event in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": "Reply with exactly: OK",
                    "session_id": "network-denied",
                }
            ],
            [],
            "Do not use tools. Answer the user literally.",
        ):
            events.append(event)

    try:
        try:
            await asyncio.wait_for(_collect(), timeout=_TURN_BUDGET_S)
        except asyncio.TimeoutError:
            pytest.fail(
                f"the turn did not complete within {_TURN_BUDGET_S:.0f}s: the sandboxed "
                f"app-server has no route to the model (events so far: {events!r})"
            )
    finally:
        await executor.close()

    errors = [event for event in events if isinstance(event, ExecutorError)]
    assert not errors, [error.message for error in errors]
    reply = "".join(
        event.text if isinstance(event, TextChunk) else (event.response or "")
        for event in events
        if isinstance(event, (TextChunk, TurnComplete))
    )
    assert "OK" in reply, events
