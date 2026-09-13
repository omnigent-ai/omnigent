"""
Integration tests for the multipart ``POST /v1/sessions`` external-host path.

The multipart form (agent bundle + ``metadata`` JSON part) accepts
``host_id``/``workspace`` in ``SessionCreateMetadata``, and the schema
documents ``host_id`` as triggering the host launch flow. These tests
drive the full route against a fake connected host that answers
``host.stat`` (workspace validation) and ``host.launch_runner`` frames,
proving the bundled session binds to the host and gets a runner launched
on it — exactly like the JSON create form.

Regression guard: the multipart path used to silently drop
``metadata.host_id`` — the session was created with ``host_id: null`` /
``runner_id: null`` and no launch frame was ever sent, while
``workspace`` from the same metadata part was persisted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio

_HOST_ID = "7a2b1c9dfe310a4bb2cc56d1a0e47b3c"
_WORKSPACE = "/Users/alice/projects/bundled"


@pytest.fixture()
def conv_store(db_uri: str) -> SqlAlchemyConversationStore:
    """Conversation store shared with the app fixture below.

    :param db_uri: SQLite database URI.
    :returns: The store the app under test writes session rows to.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    conv_store: SqlAlchemyConversationStore,
) -> FastAPI:
    """FastAPI app wired WITH ``host_store`` so host launches can run.

    Overrides the shared ``app`` fixture (which passes
    ``host_store=None`` and so skips the launch flow entirely).

    :param runtime_init: Initializes the runtime + mock LLM.
    :param db_uri: SQLite database URI.
    :param tmp_path: Pytest temp dir for artifacts and cache.
    :param conv_store: Conversation store shared with assertions.
    :returns: A configured FastAPI app with host routes mounted.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conv_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        host_store=HostStore(db_uri),
    )


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


@dataclass
class _HostCapture:
    """
    Frames a fake host received during one multipart create.

    :param stats: ``host.stat`` frames received (workspace validation).
    :param launch: ``host.launch_runner`` frames received.
    """

    stats: list[HostStatFrame] = field(default_factory=list)
    launch: list[HostLaunchRunnerFrame] = field(default_factory=list)


# register(*, launch_status=) -> _HostCapture
RegisterHost = Callable[..., _HostCapture]


@pytest_asyncio.fixture()
async def register_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory that registers a fake host with a replying drain.

    The drain answers ``host.stat`` (workspace validation passes,
    canonical path echoed back) and ``host.launch_runner`` — capturing
    each into a :class:`_HostCapture`. Every drain is poisoned and
    awaited at teardown so no background task leaks into the next
    test's event loop.

    :param app: App whose ``host_registry`` to register into.
    :param db_uri: DB URI so the ``host_id`` FK target row exists.
    :returns: Async iterator yielding a ``register`` factory. Kwargs:
        ``launch_status`` (``"launched"``/``"failed"``). Returns the
        :class:`_HostCapture` accumulating frames the host received.
    """
    conns: list[HostConnection] = []

    def _register(*, launch_status: str = "launched") -> _HostCapture:
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "bundle-host", RESERVED_USER_LOCAL)
        conn = app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
            hello=HostHelloFrame(
                version="0.1.0-test", frame_protocol_version=1, name="bundle-host"
            ),
            owner=RESERVED_USER_LOCAL,
        )
        cap = _HostCapture()

        async def _drain() -> None:
            """Answer stat/launch frames; capture them."""
            while True:
                frame_text = await conn.outbound_queue.get()
                if frame_text is None:
                    return
                frame = decode_host_frame(frame_text)
                if isinstance(frame, HostStatFrame):
                    cap.stats.append(frame)
                    fut = conn.pending_stats.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": "ok",
                                "exists": True,
                                "type": "directory",
                                "canonical_path": frame.path,
                                "error": None,
                            }
                        )
                elif isinstance(frame, HostLaunchRunnerFrame):
                    cap.launch.append(frame)
                    fut = conn.pending_launches.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": launch_status,
                                "runner_id": (
                                    "runner_from_host" if launch_status == "launched" else None
                                ),
                                "error": None if launch_status == "launched" else "boom",
                            }
                        )

        conn._drain_task_for_test = asyncio.create_task(_drain())  # type: ignore[attr-defined]
        conns.append(conn)
        return cap

    yield _register

    for conn in conns:
        conn.outbound_queue.put_nowait(None)
        task = conn._drain_task_for_test  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        if not task.done():
            task.cancel()


async def _multipart_create(
    client: httpx.AsyncClient,
    metadata: dict[str, object],
    *,
    agent_name: str = "bundle-host-agent",
) -> httpx.Response:
    """POST a multipart create with the given metadata part.

    :param client: The test HTTP client.
    :param metadata: The ``metadata`` JSON part.
    :param agent_name: Name baked into the uploaded bundle.
    :returns: The raw HTTP response.
    """
    bundle = build_agent_bundle(name=agent_name)
    return await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )


async def test_multipart_create_with_host_id_binds_and_launches(
    register_host: RegisterHost,
    client: httpx.AsyncClient,
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    A multipart create carrying ``metadata.host_id`` binds the session
    to that host and launches a runner on it — the frame the host
    receives carries the session id and validated workspace, and the
    persisted row carries host_id + workspace + a token-bound runner_id,
    mirroring the JSON create form.
    """
    cap = register_host()

    resp = await _multipart_create(client, {"host_id": _HOST_ID, "workspace": _WORKSPACE})
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session_id"]

    conv = conv_store.get_conversation(session_id)
    assert conv is not None
    assert conv.host_id == _HOST_ID, (
        f"multipart create dropped metadata.host_id: row has host_id={conv.host_id!r}"
    )
    assert conv.workspace == _WORKSPACE
    assert conv.runner_id is not None and conv.runner_id.startswith("runner_token_")

    # The host actually received a launch frame for this session with
    # the validated workspace (not just a row write).
    assert len(cap.launch) == 1, f"expected one launch frame, got {len(cap.launch)}"
    assert cap.launch[0].session_id == session_id
    assert cap.launch[0].workspace == _WORKSPACE
    # Workspace validation ran against the host (host.stat round-trip).
    assert any(frame.path == _WORKSPACE for frame in cap.stats)


async def test_multipart_create_launch_failure_keeps_binding(
    register_host: RegisterHost,
    client: httpx.AsyncClient,
    conv_store: SqlAlchemyConversationStore,
) -> None:
    """
    A host that refuses the launch does not fail the create — lenient
    like the JSON path: the session keeps its host binding and rotated
    runner_id so the first message can drive a relaunch.
    """
    register_host(launch_status="failed")

    resp = await _multipart_create(
        client,
        {"host_id": _HOST_ID, "workspace": _WORKSPACE},
        agent_name="bundle-host-launchfail-agent",
    )
    assert resp.status_code == 201, resp.text
    conv = conv_store.get_conversation(resp.json()["session_id"])
    assert conv is not None
    assert conv.host_id == _HOST_ID
    assert conv.runner_id is not None


async def test_multipart_create_host_id_requires_workspace(
    register_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """
    ``metadata.host_id`` without a workspace is rejected before any row
    exists — the same "workspace required when host_id is set" contract
    as the JSON form, not a 500 from the DB check constraint.
    """
    register_host()

    resp = await _multipart_create(
        client,
        {"host_id": _HOST_ID},
        agent_name="bundle-host-nows-agent",
    )
    assert resp.status_code == 400, resp.text
    assert "workspace required" in resp.text


async def test_multipart_create_rejects_workspace_missing_on_host(
    register_host: RegisterHost,
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """
    A workspace the host reports as missing fails the create with a 400
    naming the path — validated against the uploaded bundle BEFORE any
    session row is written (no orphan row on failure).
    """
    cap = register_host()

    # Replace the drain's stat answer: report the path missing.
    conn = app.state.host_registry.get(_HOST_ID)
    assert conn is not None

    async def _missing_drain() -> None:
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if isinstance(frame, HostStatFrame):
                fut = conn.pending_stats.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "ok",
                            "exists": False,
                            "type": None,
                            "canonical_path": None,
                            "error": None,
                        }
                    )

    old_task = conn._drain_task_for_test  # type: ignore[attr-defined]
    old_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await old_task
    conn._drain_task_for_test = asyncio.create_task(_missing_drain())  # type: ignore[attr-defined]

    resp = await _multipart_create(
        client,
        {"host_id": _HOST_ID, "workspace": "/nope/not-there"},
        agent_name="bundle-host-badws-agent",
    )
    assert resp.status_code == 400, resp.text
    assert "does not exist" in resp.text
    assert not cap.launch, "no launch frame may be sent for a rejected workspace"
