"""Session cleanup must preserve bridges handed to replacement sessions."""

from __future__ import annotations

import importlib
import json
import selectors
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.antigravity_native import bridge as agy_bridge
from omnigent.harnesses.antigravity_native import reader as agy_reader
from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.harnesses.codex_native import forwarder as codex_forwarder
from omnigent.inner.native_attachments import attachment_cache_dir, materialize_attachment
from omnigent.native import native_bridge_common
from omnigent.runner import create_runner_app
from omnigent.runner.native.orchestration import _delete_native_bridge_dirs

_FAMILIES = ("codex", "antigravity")


@pytest.fixture(autouse=True)
def _isolate_native_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All candidates touched by cleanup and their caches stay disposable."""
    for family in (
        "antigravity",
        "claude",
        "codex",
        "cursor",
        "goose",
        "hermes",
        "kimi",
        "kiro",
        "opencode",
        "pi",
        "qwen",
    ):
        module = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
        monkeypatch.setattr(module, "_BRIDGE_ROOT", tmp_path / family)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))


def _bridge_dir(family: str, bridge_id: str) -> Path:
    module = codex_bridge if family == "codex" else agy_bridge
    return module.bridge_dir_for_bridge_id(bridge_id)


def _write_owner(family: str, bridge_dir: Path, session_id: str) -> None:
    if family == "codex":
        codex_bridge.write_bridge_state(
            bridge_dir,
            codex_bridge.CodexNativeBridgeState(
                session_id=session_id,
                socket_path="ws://127.0.0.1:12345",
                thread_id="thread_old",
                codex_home=str(bridge_dir / "codex-home"),
            ),
        )
    else:
        agy_bridge.write_bridge_state(
            bridge_dir,
            agy_bridge.AntigravityNativeBridgeState(
                session_id=session_id, conversation_id="cascade_old"
            ),
        )


def _cache_attachment(bridge_dir: Path) -> Path:
    cached = materialize_attachment(
        {"type": "input_file", "filename": "note.txt", "file_data": "data:text/plain;base64,aGk="},
        bridge_dir,
    )
    assert cached is not None
    return cached


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize("labeled", [False, True])
@pytest.mark.parametrize("route_suffix", ["", "/resources"])
async def test_cleanup_after_completed_native_transfer_preserves_replacement(
    family: str,
    labeled: bool,
    route_suffix: str,
) -> None:
    """Real rotation helpers publish a replacement owner before the old DELETE."""
    old_session = "conv_old"
    new_session = "conv_new"
    bridge_id = "shared_bridge" if labeled else old_session
    label_key = (
        codex_bridge.CODEX_NATIVE_BRIDGE_ID_LABEL_KEY
        if family == "codex"
        else agy_bridge.ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY
    )
    labels = {label_key: bridge_id} if labeled else {}
    replacement_labels: dict[str, str] = {}
    transfers: list[dict[str, str]] = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            session_id = request.url.path.split("/")[3]
            return httpx.Response(
                200,
                json={
                    "id": session_id,
                    "agent_id": "agent_native",
                    "runner_id": "runner_test",
                    "labels": replacement_labels if session_id == new_session else labels,
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            replacement_labels.update(json.loads(request.content)["labels"])
            return httpx.Response(200, json={"id": new_session})
        if request.url.path.endswith("/transfer"):
            transfers.append(json.loads(request.content))
        return httpx.Response(200, json={})

    bridge_dir = _bridge_dir(family, bridge_id)
    _write_owner(family, bridge_dir, old_session)
    token = bridge_dir / "bridge.json"
    token.write_text("replacement bridge material")
    history = bridge_dir / ("codex-home" if family == "codex" else "agy-home") / "history"
    history.parent.mkdir()
    history.write_text("replacement history")
    cached = _cache_attachment(bridge_dir)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://server"
    ) as server_client:
        if family == "codex":
            replacement = await codex_forwarder._create_thread_replacement_session(
                client=server_client,
                old_session_id=old_session,
                bridge_dir=bridge_dir,
                app_server_url="ws://127.0.0.1:12345",
                new_thread_id="thread_new",
            )
        else:
            replacement = await agy_reader._rotate_session_for_cascade(
                client=server_client,
                old_session_id=old_session,
                bridge_dir=bridge_dir,
                new_cascade_id="cascade_new",
            )
        assert replacement == new_session
        assert transfers == [{"target_session_id": new_session}]
        assert replacement_labels[label_key] == bridge_id
        assert json.loads((bridge_dir / "state.json").read_text())["session_id"] == new_session

        runner = create_runner_app(server_client=server_client)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runner), base_url="http://runner"
        ) as client:
            response = await client.delete(f"/v1/sessions/{old_session}{route_suffix}")
            assert response.status_code == 200
            assert token.read_text() == "replacement bridge material"
            assert history.read_text() == "replacement history"
            assert cached.read_text() == "hi"

            response = await client.delete(f"/v1/sessions/{new_session}{route_suffix}")
            assert response.status_code == 200
            assert not bridge_dir.exists()
            assert not attachment_cache_dir(bridge_dir).exists()


@pytest.mark.parametrize("family", _FAMILIES)
async def test_cleanup_skips_bridge_and_cache_during_preparation(family: str) -> None:
    """Cleanup is nonblocking and leaves both paths while the stable lock is held."""
    bridge_dir = _bridge_dir(family, "conv_old")
    _write_owner(family, bridge_dir, "conv_old")
    cached = _cache_attachment(bridge_dir)
    with native_bridge_common.bridge_dir_preparation_lock(bridge_dir):
        await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
        assert (bridge_dir / "state.json").exists()
        assert cached.exists()
    await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
    assert not bridge_dir.exists()
    assert not cached.exists()


_PAUSED_OWNER_WRITER = """
import importlib
import os
import sys
from pathlib import Path

family, directory = sys.argv[1:]
bridge_dir = Path(directory)
bridge = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
replace = os.replace
def paused_replace(source, destination):
    if Path(destination) == bridge_dir / "state.json":
        print("ready", flush=True)
        assert sys.stdin.readline() == "release\\n"
    return replace(source, destination)
os.replace = paused_replace
if family == "codex":
    state = bridge.CodexNativeBridgeState(
        session_id="conv_new", socket_path="ws://127.0.0.1:12345",
        thread_id="thread_new", codex_home=str(bridge_dir / "codex-home"),
    )
else:
    state = bridge.AntigravityNativeBridgeState(
        session_id="conv_new", conversation_id="cascade_new",
    )
bridge.write_bridge_state(bridge_dir, state)
"""


@pytest.mark.parametrize("family", _FAMILIES)
async def test_cleanup_preserves_inflight_owner_write_in_another_process(family: str) -> None:
    """A supported writer's lock covers publication while the old owner is visible."""
    bridge_dir = _bridge_dir(family, "conv_old")
    _write_owner(family, bridge_dir, "conv_old")
    cached = _cache_attachment(bridge_dir)
    process = subprocess.Popen(
        [sys.executable, "-c", _PAUSED_OWNER_WRITER, family, str(bridge_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "owner writer did not reach publication"
        assert process.stdout.readline().strip() == "ready"
        assert json.loads((bridge_dir / "state.json").read_text())["session_id"] == "conv_old"
        await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
        assert (bridge_dir / "state.json").exists()
        assert cached.exists()
    finally:
        try:
            _, stderr = process.communicate("release\n", timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert process.returncode == 0, stderr
    assert json.loads((bridge_dir / "state.json").read_text())["session_id"] == "conv_new"
    await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
    assert cached.exists()


@pytest.mark.parametrize("family", _FAMILIES)
@pytest.mark.parametrize(
    "raw_state", [None, "not json", "[]", "{}", '{"session_id": 5}', '{"session_id": ""}']
)
async def test_cleanup_reclaims_ownerless_legacy_bridge(
    family: str, raw_state: str | None
) -> None:
    """Legacy or malformed state without a usable owner retains explicit cleanup."""
    bridge_dir = _bridge_dir(family, "conv_old")
    bridge_dir.mkdir(parents=True)
    if raw_state is not None:
        (bridge_dir / "state.json").write_text(raw_state)
    cached = _cache_attachment(bridge_dir)
    await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
    assert not bridge_dir.exists()
    assert not cached.exists()


@pytest.mark.parametrize("family", _FAMILIES)
async def test_cleanup_preserves_foreign_owner_with_incomplete_state(family: str) -> None:
    """A valid owner remains authoritative when other runtime fields are absent."""
    bridge_dir = _bridge_dir(family, "conv_old")
    bridge_dir.mkdir(parents=True)
    (bridge_dir / "state.json").write_text('{"session_id": "conv_new"}')
    cached = _cache_attachment(bridge_dir)
    await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
    assert (bridge_dir / "state.json").exists()
    assert cached.exists()


@pytest.mark.parametrize("family", _FAMILIES)
async def test_cleanup_reclaims_cache_after_bridge_is_gone(family: str) -> None:
    """Orphan attachment cleanup does not depend on a bridge directory existing."""
    bridge_dir = _bridge_dir(family, "conv_old")
    cached = _cache_attachment(bridge_dir)
    await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
    assert not cached.exists()
