"""Regression tests for ``DELETE /v1/sessions/{id}`` session cleanup.

Covers two independent problems:

#1350 — native bridge dirs leak on session delete.
Each native session's bridge dir holds a per-conversation bridge token + MCP
config (secret material). ``DELETE /v1/sessions/{id}`` closes the pane but
historically never removed this SEPARATE dir, so token-bearing
``/tmp/omnigent-*`` (and ``~/.omnigent``) dirs accumulated even on a clean
delete. The delete path must now ``rmtree`` it — for ALL 11 native families,
not just the original 5.

#3728 — spec-fill generation guard prevents ABA cache repopulation.
Session ids are caller-supplied and can be reused.  A spec fill parked across
``delete_session`` + same-id recreate must NOT write its stale result into the
new session's cache.  ``_session_cache_generations`` is incremented (never
popped) on every delete so that a fill that captured generation N before the
delete is rejected by the N+1 current value.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.harnesses.antigravity_native.bridge import (
    bridge_dir_for_bridge_id as antigravity_bridge_dir,
)
from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    read_active_session_id,
    write_active_session_id,
    write_tmux_target,
)
from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_bridge_id as claude_bridge_dir,
)
from omnigent.harnesses.codex_native.bridge import (
    bridge_dir_for_bridge_id as codex_bridge_dir,
)
from omnigent.harnesses.cursor_native.bridge import (
    bridge_dir_for_session_id as cursor_bridge_dir,
)
from omnigent.harnesses.goose_native.bridge import (
    bridge_dir_for_session_id as goose_bridge_dir,
)
from omnigent.harnesses.hermes_native.bridge import (
    bridge_dir_for_session_id as hermes_bridge_dir,
)
from omnigent.harnesses.kimi_native.bridge import (
    bridge_dir_for_session_id as kimi_bridge_dir,
)
from omnigent.harnesses.kiro_native.bridge import (
    bridge_dir_for_session_id as kiro_bridge_dir,
)
from omnigent.harnesses.opencode_native.bridge import (
    bridge_dir_for_bridge_id as opencode_bridge_dir,
)
from omnigent.harnesses.pi_native.bridge import (
    bridge_dir_for_session_id as pi_bridge_dir,
)
from omnigent.harnesses.qwen_native.bridge import (
    bridge_dir_for_session_id as qwen_bridge_dir,
)
from omnigent.inner.native_attachments import (
    attachment_cache_dir,
    materialize_attachment,
    materialize_text,
    pasted_text_cache_dir,
)
from omnigent.native.native_bridge_common import bridge_dir_preparation_lock
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient

# One resolver per native family — the session_id-keyed dir each harness leaves
# behind. _delete_native_bridge_dirs falls back to session_id for every family
# (label-rotated ids resolve to the same dir under the NullServerClient stub),
# so keying purely on session_id exercises the cleanup for all 11.
BRIDGE_DIR_RESOLVERS: dict[str, Callable[[str], Path]] = {
    "antigravity": antigravity_bridge_dir,
    "claude": claude_bridge_dir,
    "codex": codex_bridge_dir,
    "cursor": cursor_bridge_dir,
    "goose": goose_bridge_dir,
    "hermes": hermes_bridge_dir,
    "kimi": kimi_bridge_dir,
    "kiro": kiro_bridge_dir,
    "opencode": opencode_bridge_dir,
    "pi": pi_bridge_dir,
    "qwen": qwen_bridge_dir,
}


@pytest.fixture(autouse=True)
def isolated_bridge_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Deletion tests must never inspect the developer's native session roots."""
    for family in BRIDGE_DIR_RESOLVERS:
        monkeypatch.setattr(
            f"omnigent.harnesses.{family}_native.bridge._BRIDGE_ROOT", tmp_path / family
        )
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)


@pytest.fixture
def app() -> FastAPI:
    """Build a runner app with a stub server client."""
    return create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client bound to the runner app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


async def test_delete_session_removes_native_bridge_dir(
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    """``DELETE`` must remove the token-bearing claude-native bridge dir."""
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    assert bridge_dir == bridge_dir_for_bridge_id(session_id)
    # prepare_bridge_dir writes a bridge.json holding the bridge token.
    assert (bridge_dir / "bridge.json").exists()
    cached = materialize_attachment(
        {
            "type": "input_file",
            "filename": "hello.txt",
            "file_data": "data:text/plain;base64,aGk=",
        },
        bridge_dir,
    )
    assert cached is not None
    pasted = materialize_text("a cached paste", bridge_dir)
    assert pasted is not None

    resp = await client.delete(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200

    assert not bridge_dir.exists(), "bridge dir (with token) must be deleted"
    assert not cached.exists()
    assert not pasted.exists()


@pytest.mark.parametrize("family", sorted(BRIDGE_DIR_RESOLVERS))
@pytest.mark.parametrize("bridge_present", [True, False])
async def test_cleanup_resources_removes_native_bridge_dir(
    client: httpx.AsyncClient,
    family: str,
    bridge_present: bool,
) -> None:
    """The PRODUCTION delete path must remove every family's bridge dir.

    Server-side session delete drives ``DELETE /v1/sessions/{id}/resources``
    (``cleanup_session_resources``), NOT the bare ``DELETE /v1/sessions/{id}``
    route — so the token-bearing bridge dir must be removed there too, else the
    #1350 leak persists in real deletes. All 11 native families create such a
    dir, so all 11 must be cleaned up.
    """
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = BRIDGE_DIR_RESOLVERS[family](session_id)
    # Materialize the token-bearing dir the harness would have left behind.
    if bridge_present:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        (bridge_dir / "bridge.json").write_text("{}")
    cached = materialize_attachment(
        {
            "type": "input_file",
            "filename": "bundle.zip",
            "file_data": "data:application/zip;base64,UEsDBA==",
        },
        bridge_dir,
    )
    assert cached is not None
    pasted = materialize_text("a cached paste", bridge_dir)
    assert pasted is not None

    resp = await client.delete(f"/v1/sessions/{session_id}/resources")
    assert resp.status_code == 200

    assert not bridge_dir.exists(), (
        f"{family} bridge dir must be deleted on the real /resources path"
    )
    assert not attachment_cache_dir(bridge_dir).exists()
    assert not pasted_text_cache_dir(bridge_dir).exists()


@pytest.mark.parametrize("suffix", ["", "/resources"])
@pytest.mark.parametrize("foreign_path", ["natural", "labeled"])
async def test_cleanup_preserves_bridge_transferred_to_another_session(
    tmp_path: Path,
    suffix: str,
    foreign_path: str,
) -> None:
    """Deleting the pre-clear session must keep its replacement's bridge usable."""
    old_session = f"conv_{uuid.uuid4().hex}"
    replacement = f"conv_{uuid.uuid4().hex}"
    retired_bridge_id = f"{old_session}-cleared"
    natural = prepare_bridge_dir(old_session, workspace=tmp_path)
    labeled = prepare_bridge_dir(old_session, bridge_id=retired_bridge_id, workspace=tmp_path)
    foreign, owned = (natural, labeled) if foreign_path == "natural" else (labeled, natural)
    write_active_session_id(foreign, replacement)
    write_tmux_target(foreign, socket_path=tmp_path / "replacement.sock", tmux_target="live:0.0")
    attachment = {
        "type": "input_file",
        "filename": "input.txt",
        "file_data": "data:text/plain;base64,aGk=",
    }
    foreign_attachment = materialize_attachment(attachment, foreign)
    owned_attachment = materialize_attachment(attachment, owned)
    assert foreign_attachment is not None and owned_attachment is not None
    metadata = {path.name: path.read_bytes() for path in foreign.iterdir() if path.is_file()}

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"labels": {BRIDGE_ID_LABEL_KEY: retired_bridge_id}}
        )
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server:
        async with _runner_client(create_runner_app(server_client=server)) as runner:
            response = await runner.delete(f"/v1/sessions/{old_session}{suffix}")

    assert response.status_code == 200
    assert read_active_session_id(foreign) == replacement
    assert {
        path.name: path.read_bytes() for path in foreign.iterdir() if path.is_file()
    } == metadata
    assert foreign_attachment.read_bytes() == b"hi"
    assert not owned.exists()
    assert not attachment_cache_dir(owned).exists()


@pytest.mark.parametrize(
    ("config", "preserved"),
    [
        ('{"conversation_id": "conv_other"}', True),
        ('{"conversation_id": "conv_delete"}', False),
        ('{"active_session_id": "conv_delete", "conversation_id": "conv_other"}', False),
        ("{}", False),
        ("{broken", False),
        ('{"active_session_id": 7}', False),
        (None, False),
    ],
)
async def test_cleanup_honors_legacy_owner_and_cleans_unowned_metadata(
    client: httpx.AsyncClient,
    config: str | None,
    preserved: bool,
) -> None:
    """A known legacy owner is protected; inconclusive old metadata still gets reclaimed."""
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = claude_bridge_dir(session_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    config_path = bridge_dir / "bridge.json"
    if config is None:
        config_path.unlink(missing_ok=True)
    else:
        config_path.write_text(config.replace("conv_delete", session_id))
    attachment = materialize_attachment(
        {
            "type": "input_file",
            "filename": "input.txt",
            "file_data": "data:text/plain;base64,aGk=",
        },
        bridge_dir,
    )
    assert attachment is not None

    response = await client.delete(f"/v1/sessions/{session_id}/resources")

    assert response.status_code == 200
    assert bridge_dir.exists() == preserved
    assert attachment.exists() == preserved
    if preserved:
        assert read_active_session_id(bridge_dir) == "conv_other"
        assert attachment.read_bytes() == b"hi"


@pytest.mark.parametrize("suffix", ["", "/resources"])
async def test_cleanup_preserves_bridge_and_cache_while_metadata_writer_holds_lock(
    client: httpx.AsyncClient, tmp_path: Path, suffix: str
) -> None:
    """A busy metadata writer defers deletion; a subsequent cleanup can remove the owner."""
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    attachment = materialize_attachment(
        {
            "type": "input_file",
            "filename": "input.txt",
            "file_data": "data:text/plain;base64,aGk=",
        },
        bridge_dir,
    )
    assert attachment is not None
    config = (bridge_dir / "bridge.json").read_bytes()

    with bridge_dir_preparation_lock(bridge_dir):
        response = await client.delete(f"/v1/sessions/{session_id}{suffix}")
        assert response.status_code == 200
        assert (bridge_dir / "bridge.json").read_bytes() == config
        assert attachment.read_bytes() == b"hi"

    response = await client.delete(f"/v1/sessions/{session_id}{suffix}")
    assert response.status_code == 200
    assert not bridge_dir.exists()
    assert not attachment_cache_dir(bridge_dir).exists()


async def test_cleanup_preserves_bridge_while_separate_process_updates_owner(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A real metadata writer excludes cleanup across processes, then releases the lock."""
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    attachment = materialize_attachment(
        {
            "type": "input_file",
            "filename": "input.txt",
            "file_data": "data:text/plain;base64,aGk=",
        },
        bridge_dir,
    )
    assert attachment is not None
    config = (bridge_dir / "bridge.json").read_bytes()
    ready, release = tmp_path / "writer-ready", tmp_path / "writer-release"
    script = """
import sys
import time
from pathlib import Path
from omnigent.harnesses.claude_native import bridge

bridge_dir, ready, release = map(Path, sys.argv[1:4])
bridge._BRIDGE_ROOT = bridge_dir.parent
bridge._TRUSTED_PARENT = bridge_dir.parent
original_read = bridge._read_json_file

def paused_read(path):
    value = original_read(path)
    ready.touch()
    deadline = time.monotonic() + 30
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("writer release timed out")
        time.sleep(0.01)
    return value

bridge._read_json_file = paused_read
bridge.write_active_session_id(bridge_dir, sys.argv[4])
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(bridge_dir),
        str(ready),
        str(release),
        session_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            while not ready.exists():
                if process.returncode is not None:
                    _, stderr = await process.communicate()
                    pytest.fail(f"metadata writer exited before locking: {stderr.decode()}")
                await asyncio.sleep(0.01)
        response = await client.delete(f"/v1/sessions/{session_id}/resources")
        assert response.status_code == 200
        assert (bridge_dir / "bridge.json").read_bytes() == config
        assert attachment.read_bytes() == b"hi"
        release.touch()
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0, stderr.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()

    assert read_active_session_id(bridge_dir) == session_id
    response = await client.delete(f"/v1/sessions/{session_id}/resources")
    assert response.status_code == 200
    assert not bridge_dir.exists()
    assert not attachment_cache_dir(bridge_dir).exists()


# ── #3728: spec-fill generation guard ───────────────────────────────────────

_AGENT_ID_GENERATION = "ag_test_generation"


class _AgentSnapshotServerClient(NullServerClient):
    """Server client whose session GET returns a valid agent_id."""

    def __init__(self, session_id: str, agent_id: str) -> None:
        self._session_id = session_id
        self._agent_id = agent_id

    class _SessionResp:
        status_code = 200

        def __init__(self, agent_id: str) -> None:
            self._payload = {"agent_id": agent_id, "created_at": 0}
            self.text = json.dumps(self._payload)

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            pass

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(self._session_id):
            return self._SessionResp(self._agent_id)
        return self._Response()


async def test_spec_fill_parked_across_delete_discards(tmp_path: Path) -> None:
    """A spec fill that starts before delete must not repopulate the cache.

    Sequence:
    1. Terminal launch triggers ``_resolve_session_spec_entry`` → spec
       resolver parks (generation captured = 0).
    2. ``DELETE /v1/sessions/{id}`` increments generation to 1.
    3. Spec resolver unparks → generation check: 1 ≠ 0 → discard.
    4. Second terminal launch triggers a fresh fill → resolver called again.

    If the generation guard were absent or broken (pop+reset to 0), the stale
    fill from step 1 would write its result and step 4 would NOT invoke the
    resolver a second time.
    """
    session_id = "conv_test_generation_aba"

    resolver_started = asyncio.Event()
    resolver_gate = asyncio.Event()
    resolve_count = 0

    async def _pausable_resolver(agent_id: str, sid: str | None = None) -> AgentSpec:
        nonlocal resolve_count
        resolve_count += 1
        resolver_started.set()
        await resolver_gate.wait()
        return AgentSpec(
            spec_version=1,
            name=agent_id,
            executor=ExecutorSpec(type="omnigent"),
        )

    app = create_runner_app(
        server_client=_AgentSnapshotServerClient(session_id, _AGENT_ID_GENERATION),  # type: ignore[arg-type]
        spec_resolver=_pausable_resolver,
    )

    async with _runner_client(app) as client:
        # Phase 1: start a terminal launch that parks in the spec resolver.
        launch_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "bash", "session_key": "main"},
            )
        )
        await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

        # Phase 2: delete the session — increments the generation counter.
        del_resp = await client.delete(f"/v1/sessions/{session_id}")
        assert del_resp.status_code == 200

        # Phase 3: release the resolver — the fill completes and must discard
        # its result because the generation no longer matches.
        resolver_gate.set()
        await asyncio.wait_for(launch_task, timeout=5.0)

        count_after_stale_fill = resolve_count
        assert count_after_stale_fill >= 1, "resolver should have been called at least once"

        # Phase 4: trigger a second spec fill for the same session id.
        # If the stale fill had populated the cache, the resolver would NOT
        # be called again.  A correctly guarded fill leaves the cache empty,
        # so the next fill calls the resolver a second time.
        resolver_started.clear()
        resolver_gate.clear()

        launch_task2 = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "bash", "session_key": "main"},
            )
        )

        # Wait for the resolver to start again (proves a fresh fill began).
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                "spec resolver was not called a second time — stale fill "
                "from before deletion may have repopulated the cache (ABA)"
            )

        resolver_gate.set()
        await asyncio.wait_for(launch_task2, timeout=5.0)

        assert resolve_count > count_after_stale_fill, (
            "resolver was not called after delete: stale fill may have "
            "populated the spec cache across the session boundary"
        )


# ── delete-vs-create race: in-flight init must be cancelled ──────────────────


async def test_delete_session_cancels_inflight_init() -> None:
    """Deleting a session cancels its pending initialization before resource teardown."""
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

    session_id = f"conv_{uuid.uuid4().hex}"
    resolver_started = asyncio.Event()
    resolver_cancelled = asyncio.Event()

    async def parked_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        resolver_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            resolver_cancelled.set()
            raise
        raise AssertionError("unreachable")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=parked_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        create_request = asyncio.create_task(
            client.post("/v1/sessions", json={"session_id": session_id, "agent_id": "ag_x"})
        )
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

            resp = await client.delete(f"/v1/sessions/{session_id}")

            assert resp.status_code == 200
            try:
                await asyncio.wait_for(resolver_cancelled.wait(), timeout=5.0)
            except TimeoutError:
                pytest.fail(
                    "delete_session left the in-flight session init running; "
                    "a startup racing a delete leaks whatever it registers next"
                )
        finally:
            create_request.cancel()
            with contextlib.suppress(BaseException):
                await create_request


async def test_delete_session_survives_a_failing_inflight_init(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An init failure during cancellation is logged without aborting deletion."""
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

    session_id = f"conv_{uuid.uuid4().hex}"
    resolver_started = asyncio.Event()

    class _InitUnwindError(Exception):
        """A failure the init path does not handle, so it reaches the task."""

    async def exploding_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        resolver_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Use an error that initialization does not convert to an HTTP response.
            raise _InitUnwindError("init cleanup exploded") from None
        raise AssertionError("unreachable")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=exploding_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        create_request = asyncio.create_task(
            client.post("/v1/sessions", json={"session_id": session_id, "agent_id": "ag_x"})
        )
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

            with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
                resp = await client.delete(f"/v1/sessions/{session_id}")

            assert resp.status_code == 200, (
                "a failing in-flight init aborted the delete; the teardown that "
                f"reaps native resources never ran (body={resp.text[:200]})"
            )
            assert any(
                record.name == "omnigent.runner.app"
                and "failed while being cancelled" in record.getMessage()
                for record in caplog.records
            ), "the init failure was swallowed silently instead of being logged"
        finally:
            create_request.cancel()
            with contextlib.suppress(BaseException):
                await create_request


async def test_delete_session_bounds_the_wait_on_a_slow_init_unwind(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion proceeds after the timeout if cancelled initialization is still unwinding."""
    from omnigent.runner import app as runner_app_mod
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

    session_id = f"conv_{uuid.uuid4().hex}"
    resolver_started = asyncio.Event()
    unwind_started = asyncio.Event()
    unwind_release = asyncio.Event()

    # Initialization stays blocked until explicitly released below.
    monkeypatch.setattr(runner_app_mod, "_SESSION_INIT_CANCEL_TIMEOUT_S", 0.05)

    async def slow_unwind_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        resolver_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwind_started.set()
            await unwind_release.wait()
            raise
        raise AssertionError("unreachable")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=slow_unwind_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        create_request = asyncio.create_task(
            client.post("/v1/sessions", json={"session_id": session_id, "agent_id": "ag_x"})
        )
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

            with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
                try:
                    resp = await asyncio.wait_for(
                        client.delete(f"/v1/sessions/{session_id}"), timeout=5.0
                    )
                except TimeoutError:
                    pytest.fail(
                        "delete_session blocked on a cancelled init that never "
                        "finished unwinding; the wait must be bounded"
                    )

            assert resp.status_code == 200
            assert unwind_started.is_set(), "the init was never cancelled"
            assert not unwind_release.is_set(), (
                "test bug: the init was released before DELETE returned, so this "
                "run does not exercise the bound"
            )
            assert any(
                record.name == "omnigent.runner.app"
                and "did not finish within" in record.getMessage()
                for record in caplog.records
            ), "the bounded wait timed out without logging a warning"
        finally:
            unwind_release.set()
            create_request.cancel()
            with contextlib.suppress(BaseException):
                await create_request
