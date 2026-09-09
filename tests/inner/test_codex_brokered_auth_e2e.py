"""Real Codex + macOS Seatbelt brokered-auth acceptance test."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

from omnigent.inner.codex_executor import CodexExecutor, _require_brokered_codex_version
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import TextChunk, TurnComplete
from omnigent.inner.model_egress import FrozenModelRoute
from omnigent.inner.model_signer import SignerLaunchConfig

_CODEX = Path("/opt/homebrew/bin/codex")
_MARKER = "BROKERED_E2E_OK upstream_saw_signer_only_fake_bearer=true"


@pytest.mark.skipif(sys.platform != "darwin", reason="requires real macOS Seatbelt")
@pytest.mark.skipif(not _CODEX.is_file(), reason="requires installed Homebrew Codex")
async def test_real_codex_seatbelt_signer_turn_and_cleanup(
    tmp_path: Path,
) -> None:
    try:
        await _require_brokered_codex_version(str(_CODEX))
    except RuntimeError as exc:
        pytest.skip(f"requires brokered Codex version pin: {exc}")
    config = SignerLaunchConfig(
        binding_id="test-fake-provider-v1",
        endpoint="https://model.test/v1",
        routes=(FrozenModelRoute(method="POST", host="model.test", path="/v1/responses"),),
    )
    executor = CodexExecutor(
        cwd=str(tmp_path),
        os_env=OSEnvSpec(
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(
                type="darwin_seatbelt",
                read_paths=[str(tmp_path)],
                write_paths=[str(tmp_path)],
                allow_network=False,
                cwd_hidden_scan_overflow="error",
            ),
        ),
        codex_path=str(_CODEX),
        model="gpt-5.4-mini",
        enable_web_search=False,
        disable_native_tools=True,
        signer_launch_config=config,
    )
    events: list[object] = []
    public_dir: Path | None = None
    private_dirs_before = set(Path("/tmp").glob("omnigent-model-signer-private-*"))

    async def _collect_turn() -> None:
        async for event in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": "Return the provider's deterministic marker.",
                    "session_id": "brokered-e2e",
                }
            ],
            [],
            "Do not use tools. Return the model response exactly.",
        ):
            events.append(event)

    try:
        try:
            await asyncio.wait_for(_collect_turn(), timeout=30)
        except asyncio.TimeoutError as exc:
            state = executor._session_states.get("brokered-e2e")
            stderr = (
                state.app_session._recent_stderr
                if state is not None and state.app_session is not None
                else []
            )
            raise AssertionError(f"real Codex turn timed out; stderr={stderr!r}") from exc

        state = executor._session_states["brokered-e2e"]
        assert state.app_session is not None
        readiness = state.app_session._signer_readiness
        assert readiness is not None
        public_dir = readiness.ca_bundle_path.parent
        assert readiness.placeholder.startswith("oa_cred_")
        assert readiness.placeholder not in repr(events)
        assert state.app_session._containment_confirmed
        assert state.app_session._worker_launch is not None
        assert state.app_session._worker_launch.sandboxed
    finally:
        await executor.close()

    response = "".join(
        event.text if isinstance(event, TextChunk) else event.response or ""
        for event in events
        if isinstance(event, (TextChunk, TurnComplete))
    )
    assert _MARKER in response, repr(events)
    assert public_dir is not None
    assert not public_dir.exists()
    assert set(Path("/tmp").glob("omnigent-model-signer-private-*")) == private_dirs_before
    assert not list(tmp_path.glob("omnigent-codex-tmp/omnigent-codex-home-*"))
    assert shutil.which("sandbox-exec") is not None


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="requires Linux with bubblewrap",
)
async def test_linux_bwrap_real_signer_relay_with_deterministic_worker(tmp_path: Path) -> None:
    """Exercise real bwrap containment and the actual signer relay in CI."""
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        """#!/usr/bin/python3
import http.client, json, os, ssl, sys
from urllib.parse import urlsplit
if "--version" in sys.argv:
    print("codex-cli 0.146.0")
    raise SystemExit(0)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result = {}
    elif method == "thread/start":
        result = {"thread": {"id": "thread-bwrap"}}
    elif method == "turn/start":
        body = json.dumps({"model": "gpt-5.4-mini", "input": "probe"}).encode()
        proxy = urlsplit(os.environ["HTTPS_PROXY"])
        tls = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
        connection = http.client.HTTPSConnection(
            proxy.hostname,
            proxy.port,
            timeout=10,
            context=tls,
        )
        connection.set_tunnel("model.test", 443)
        try:
            connection.request(
                "POST",
                "/v1/responses",
                body=body,
                headers={
                "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                "Content-Type": "application/json",
                "Connection": "keep-alive",
                },
            )
            response = connection.getresponse()
            assert response.status == 200
            assert b"BROKERED_E2E_OK" in response.read()
        finally:
            connection.close()
        result = {"turn": {"id": "turn-bwrap"}}
    else:
        result = {}
    print(json.dumps({"id": request_id, "result": result}), flush=True)
    if method == "turn/start":
        print(json.dumps({
            "method": "item/completed",
            "params": {
                "turnId": "turn-bwrap",
                "item": {
                    "id": "msg-bwrap",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "BROKERED_E2E_OK",
                },
            },
        }), flush=True)
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    config = SignerLaunchConfig(
        binding_id="test-fake-provider-v1",
        endpoint="https://model.test/v1",
        routes=(FrozenModelRoute(method="POST", host="model.test", path="/v1/responses"),),
    )
    executor = CodexExecutor(
        cwd=str(tmp_path),
        os_env=OSEnvSpec(
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(
                type="linux_bwrap",
                read_paths=[str(tmp_path)],
                write_paths=[str(tmp_path)],
                allow_network=False,
                cwd_hidden_scan_overflow="error",
            ),
        ),
        codex_path=str(fake_codex),
        model="gpt-5.4-mini",
        enable_web_search=False,
        disable_native_tools=True,
        signer_launch_config=config,
    )
    events: list[object] = []
    try:
        async for event in executor.run_turn(
            [{"role": "user", "content": "probe", "session_id": "bwrap-e2e"}],
            [],
            "Return the marker.",
        ):
            events.append(event)
        state = executor._session_states["bwrap-e2e"]
        assert state.app_session is not None
        assert state.app_session._containment_confirmed
        assert state.app_session._worker_launch is not None
        assert state.app_session._worker_launch.sandboxed
    finally:
        await executor.close()

    assert any(
        isinstance(event, TurnComplete) and event.response == "BROKERED_E2E_OK" for event in events
    )
