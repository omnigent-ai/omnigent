"""Tests for ``POST /v1/sessions/{source_id}/fork``.

Exercises the fork endpoint's validation logic (404 for missing
session, 400 for no agent binding) and the happy-path response
shape using minimal real-type stubs — no MagicMock.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport
from starlette.testclient import TestClient

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import (
    Agent,
    Conversation,
    ConversationItem,
    MessageData,
    PagedList,
    StoredFile,
)
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider
from omnigent.server.managed_hosts import (
    MANAGED_REPO_LABEL_KEY,
    ManagedLaunchTracker,
    parse_sandbox_config,
    resolve_managed_agent_label,
)
from omnigent.server.routes import _session_create_validation as create_validation
from omnigent.server.routes.sessions import create_sessions_router, routes_core
from omnigent.stores.conversation_store import _FORK_ONLY_DROPPED_LABEL_KEYS

# ── Minimal store stubs ──────────────────────────────────────────


class _AgentStore:
    """Agent store stub that supports get and create for fork tests.

    Pre-populated with agents keyed by ID. ``create`` records the
    call and stores the new agent so the route's clone-then-fork
    sequence succeeds.

    :param agents: Pre-populated map of agent_id → Agent.
    """

    def __init__(self, agents: dict[str, Agent] | None = None) -> None:
        """
        Initialize the stub.

        :param agents: Map from agent ID to Agent entity.
        """
        self._agents: dict[str, Agent] = dict(agents or {})
        self.create_calls: list[dict[str, Any]] = []

    def get(self, agent_id: str) -> Agent | None:
        """
        Return the agent or None.

        :param agent_id: Agent ID to look up.
        :returns: The Agent if found, else None.
        """
        return self._agents.get(agent_id)

    def create(
        self,
        agent_id: str,
        name: str,
        bundle_location: str,
        description: str | None = None,
    ) -> Agent:
        """
        Record the create call and store the new agent.

        :param agent_id: New agent ID, e.g. ``"104c4932179e16161e9ed9298fd5a3e2"``.
        :param name: Agent name.
        :param bundle_location: Bundle location string.
        :param description: Optional description.
        :returns: The newly created Agent.
        """
        self.create_calls.append(
            {
                "agent_id": agent_id,
                "name": name,
                "bundle_location": bundle_location,
                "description": description,
            }
        )
        agent = Agent(
            id=agent_id,
            created_at=1,
            name=name,
            bundle_location=bundle_location,
            version=1,
            description=description,
        )
        self._agents[agent_id] = agent
        return agent


class _ConversationStore:
    """In-memory conversation store stub for route-level tests.

    Provides the subset of the :class:`ConversationStore` interface
    that the fork route calls. Using a real class (not MagicMock)
    so that unexpected attribute access fails loud.

    :param conversations: Pre-populated map of id → Conversation.
    :param items_by_conv: Pre-populated map of conv_id → item list.
    """

    def __init__(
        self,
        conversations: dict[str, Conversation],
        items_by_conv: dict[str, list[ConversationItem]] | None = None,
    ) -> None:
        """
        Initialize the stub.

        :param conversations: Map from conversation ID to Conversation.
        :param items_by_conv: Map from conversation ID to items.
        """
        self._convs = conversations
        self._items = items_by_conv or {}
        self.fork_calls: list[dict[str, Any]] = []
        self.label_writes: list[tuple[str, dict[str, str]]] = []

    def set_labels(
        self,
        conversation_id: str,
        updates: dict[str, str],
        updated_at: int | None = None,
    ) -> None:
        """
        Upsert labels on a conversation, recording the call.

        The managed launch re-stamps its repository here, so applying the
        write (not just recording it) is what lets a test read the fork's
        settled labels.

        :param conversation_id: Conversation the labels land on.
        :param updates: Label keys to upsert.
        :param updated_at: Ignored by the stub.
        """
        del updated_at
        self.label_writes.append((conversation_id, dict(updates)))
        conv = self._convs.get(conversation_id)
        if conv is not None:
            conv.labels.update(updates)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """
        Return the conversation or None.

        :param conversation_id: Conversation ID to look up.
        :returns: The Conversation if found, else None.
        """
        return self._convs.get(conversation_id)

    def fork_conversation(
        self,
        source_conversation_id: str,
        *,
        title: str | None = None,
        agent_id: str | None = None,
        cloned_agent_name: str | None = None,
        cloned_agent_bundle_location: str | None = None,
        cloned_agent_description: str | None = None,
        copy_model_settings: bool = True,
        copy_terminal_launch_args: bool = True,
        override_model_override: str | None = None,
        override_model_override_set: bool = False,
        override_reasoning_effort: str | None = None,
        override_reasoning_effort_set: bool = False,
        override_terminal_launch_args: list[str] | None = None,
        override_terminal_launch_args_set: bool = False,
        dropped_label_keys: frozenset[str] = frozenset(),
        extra_labels: dict[str, str] | None = None,
        carry_history_into_native: bool = False,
        resume_source_native_session: bool = True,
        presentation_labels: dict[str, str] | None = None,
        up_to_response_id: str | None = None,
        project_id: str | None = None,
        file_id_map: dict[str, str] | None = None,
    ) -> Conversation:
        """
        Record the fork call and return a fixed new conversation.

        :param source_conversation_id: Source ID, e.g. ``"e9f8f58523cec9a57d3bdf93be543e8c"``.
        :param title: Optional title for the fork.
        :param agent_id: Agent ID override. When ``None``, inherits
            the source's ``agent_id``.
        :param cloned_agent_name: Name for the fork's cloned agent row
            (route supplies ``"<name> (fork <id>)"`` when cloning).
        :param cloned_agent_bundle_location: Bundle the fork clones into
            a session-scoped agent row created atomically in the store.
        :param cloned_agent_description: Optional clone description.
        :param copy_model_settings: Whether the source's model settings
            carry over (route passes ``False`` on a cross-family switch).
        :param copy_terminal_launch_args: Whether the source's launch args
            carry over (route passes ``False`` on any agent switch — launch
            flags are CLI-specific and would break a different target CLI).
        :param carry_history_into_native: Whether to mark the fork for
            native transcript rebuild (route passes ``True`` for any
            native target, regardless of family).
        :param resume_source_native_session: Whether the source's native
            session id may be stamped for the runner's clone path (route
            passes ``False`` on a cross-family switch — the source's
            native transcript is the wrong format for the target).
        :param presentation_labels: Web UI mode labels for the switched-to
            target (``{}`` to drop them for an SDK target, ``{ui, wrapper}``
            for a native target), or ``None`` on a same-agent fork.
        :param up_to_response_id: Truncation point, e.g. ``"resp_a"``.
            Mirrors the real store: ``None`` copies everything; a value
            matching no item's ``response_id`` raises ValueError.
        :param project_id: First-class project the fork is filed into
            (route passes the source's project only when the forker
            owns it), or ``None`` for unfiled.
        :param file_id_map: Source file id → fork-owned file id for the
            file resources the route copies into the fork (empty when
            the source has none, or when no file store is configured).
        :returns: A new Conversation with a deterministic ID.
        :raises LookupError: If source is not in our map.
        :raises ValueError: If *up_to_response_id* matches no item.
        """
        self.fork_calls.append(
            {
                "source": source_conversation_id,
                "title": title,
                "agent_id": agent_id,
                "cloned_agent_name": cloned_agent_name,
                "cloned_agent_bundle_location": cloned_agent_bundle_location,
                "cloned_agent_description": cloned_agent_description,
                "copy_model_settings": copy_model_settings,
                "copy_terminal_launch_args": copy_terminal_launch_args,
                "override_model_override": override_model_override,
                "override_model_override_set": override_model_override_set,
                "override_reasoning_effort": override_reasoning_effort,
                "override_reasoning_effort_set": override_reasoning_effort_set,
                "override_terminal_launch_args": override_terminal_launch_args,
                "override_terminal_launch_args_set": override_terminal_launch_args_set,
                "dropped_label_keys": dropped_label_keys,
                "extra_labels": extra_labels,
                "carry_history_into_native": carry_history_into_native,
                "resume_source_native_session": resume_source_native_session,
                "presentation_labels": presentation_labels,
                "up_to_response_id": up_to_response_id,
                "project_id": project_id,
                "file_id_map": file_id_map,
            }
        )
        src = self._convs.get(source_conversation_id)
        if src is None:
            raise LookupError(f"not found: {source_conversation_id}")
        if up_to_response_id is not None and not any(
            item.response_id == up_to_response_id
            for item in self._items.get(source_conversation_id, [])
        ):
            raise ValueError(
                f"response not found in conversation "
                f"{source_conversation_id!r}: {up_to_response_id!r}"
            )
        effective_agent_id = agent_id if agent_id is not None else src.agent_id
        # Also store items under the fork ID so list_items returns
        # the copied items (mirrors real store behavior, including the
        # up-to-and-including-last-item-of-the-response truncation).
        fork_id = "c538360473d41c84c1eee13918fbeca0"
        source_items = list(self._items.get(source_conversation_id, []))
        if up_to_response_id is not None:
            cutoff_index = max(
                index
                for index, item in enumerate(source_items)
                if item.response_id == up_to_response_id
            )
            source_items = source_items[: cutoff_index + 1]
        self._items[fork_id] = source_items
        # Mirror the real store's label handling for the three rules the route
        # depends on: source labels are copied EXCEPT the store's own fork-only
        # denylist and the keys the route asked to drop, then extra_labels are
        # stamped on top (so a deliberate opt-in beats the drop). Without this
        # the stub returned a label-less fork, so no test could see a source
        # label riding onto the clone. presentation_labels is deliberately NOT
        # modelled — the route's own assertions read it off the recorded call
        # above.
        fork_labels = {
            key: value
            for key, value in src.labels.items()
            if key not in (_FORK_ONLY_DROPPED_LABEL_KEYS | dropped_label_keys)
        }
        fork_labels.update(extra_labels or {})
        fork = Conversation(
            id=fork_id,
            created_at=100,
            updated_at=100,
            root_conversation_id=fork_id,
            title=title or f"Fork of {src.title}",
            agent_id=effective_agent_id,
            labels=fork_labels,
        )
        self._convs[fork_id] = fork
        return fork

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> PagedList[ConversationItem]:
        """
        Return items for the given conversation.

        :param conversation_id: Conversation to list items for.
        :param limit: Max items.
        :param after: Cursor.
        :param before: Cursor.
        :param order: Sort order.
        :param type: Item type filter.
        :returns: A PagedList of items.
        """
        items = list(self._items.get(conversation_id, []))
        # Honor order + limit like the real store, so route tests can pin
        # which page of the copied history a response carries.
        if order == "desc":
            items.reverse()
        has_more = len(items) > limit
        items = items[:limit]
        return PagedList(
            data=items,
            first_id=items[0].id if items else None,
            last_id=items[-1].id if items else None,
            has_more=has_more,
        )


class _FileStore:
    """In-memory file store stub scoped like the real one.

    :param files: Pre-populated map of file_id → StoredFile.
    """

    def __init__(self, files: dict[str, StoredFile] | None = None) -> None:
        """
        Initialize the stub.

        :param files: Map from file ID to StoredFile entity.
        """
        self.files: dict[str, StoredFile] = dict(files or {})

    def list(
        self,
        session_id: str,
        limit: int = 20,
        after: str | None = None,
        before: str | None = None,
        order: str = "desc",
        include_unscoped: bool = False,
    ) -> PagedList[StoredFile]:
        """
        Return the session's files (single page — stub keeps few).

        :param session_id: Owning session whose files to list.
        :param limit: Max files.
        :param after: Cursor (unused by the stub).
        :param before: Cursor (unused by the stub).
        :param order: Sort order (unused by the stub).
        :param include_unscoped: Unused by the stub.
        :returns: A PagedList of the session's files.
        """
        del after, before, order, include_unscoped
        data = [f for f in self.files.values() if f.session_id == session_id][:limit]
        return PagedList(
            data=data,
            first_id=data[0].id if data else None,
            last_id=data[-1].id if data else None,
            has_more=False,
        )

    def create(
        self,
        filename: str,
        bytes: int,
        content_type: str | None = None,
        session_id: str | None = None,
        file_id: str | None = None,
        blob_key: str | None = None,
        source_metadata: dict[str, Any] | None = None,
    ) -> StoredFile:
        """
        Record a new file row, honoring a caller-chosen id and blob_key.

        :param filename: Original filename.
        :param bytes: File size in bytes.
        :param content_type: MIME type.
        :param session_id: Owning session id.
        :param file_id: Caller-chosen id, or ``None`` to derive one.
        :param blob_key: Artifact-store key for the bytes (a fork copy
            shares the source's blob); defaults to the row's own id.
        :param source_metadata: Opaque upload metadata carried onto the copy.
        :returns: The newly created StoredFile.
        """
        new_id = file_id or f"gen{len(self.files):029d}"
        stored = StoredFile(
            id=new_id,
            created_at=1,
            filename=filename,
            bytes=bytes,
            content_type=content_type,
            session_id=session_id,
            blob_key=blob_key if blob_key is not None else new_id,
            source_metadata=source_metadata,
        )
        self.files[new_id] = stored
        return stored


class _ArtifactStore:
    """In-memory artifact (blob) store stub.

    :param blobs: Pre-populated map of file_id → content bytes.
    """

    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        """
        Initialize the stub.

        :param blobs: Map from file ID to blob bytes.
        """
        self.blobs: dict[str, bytes] = dict(blobs or {})
        self.exists_calls = 0

    def exists(self, artifact_id: str) -> bool:
        """
        Whether a blob exists (counts calls so tests can assert no probing).

        :param artifact_id: Blob key (the file id).
        :returns: True when the blob is present.
        """
        self.exists_calls += 1
        return artifact_id in self.blobs

    def get(self, artifact_id: str) -> bytes:
        """
        Return blob bytes.

        :param artifact_id: Blob key (the file id).
        :returns: The blob's bytes.
        """
        return self.blobs[artifact_id]

    def put(self, artifact_id: str, content: bytes) -> None:
        """
        Store blob bytes.

        :param artifact_id: Blob key (the file id).
        :param content: The bytes to store.
        """
        self.blobs[artifact_id] = content

    def delete(self, artifact_id: str) -> None:
        """
        Delete a blob if present.

        :param artifact_id: Blob key (the file id).
        """
        self.blobs.pop(artifact_id, None)


# ── Helpers ──────────────────────────────────────────────────────


def _make_conversation(
    conv_id: str = "e9f8f58523cec9a57d3bdf93be543e8c",
    agent_id: str | None = "087b7cb7ac30abf4debfaa578d052ec6",
    title: str = "Source Chat",
    kind: str = "default",
    labels: dict[str, str] | None = None,
) -> Conversation:
    """
    Build a minimal Conversation entity for testing.

    :param conv_id: Conversation id.
    :param agent_id: Agent id or None.
    :param title: Title string.
    :param kind: Conversation kind, e.g. ``"default"`` or
        ``"sub_agent"``.
    :param labels: Session labels, e.g. the sandbox repository a managed
        source recorded. Empty when omitted.
    :returns: A Conversation.
    """
    return Conversation(
        id=conv_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=conv_id,
        agent_id=agent_id,
        title=title,
        kind=kind,
        labels=dict(labels or {}),
    )


def _make_item(item_id: str, text: str, response_id: str = "resp_001") -> ConversationItem:
    """
    Build a minimal ConversationItem for testing.

    :param item_id: Item id.
    :param text: Message text content.
    :param response_id: Response the item belongs to, e.g. ``"resp_001"``.
    :returns: A ConversationItem.
    """
    return ConversationItem(
        id=item_id,
        type="message",
        status="completed",
        response_id=response_id,
        created_at=1,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": text}],
        ),
    )


def _build_app(
    store: _ConversationStore,
    agent_store: _AgentStore | None = None,
    auth_provider: AuthProvider | None = None,
    file_store: _FileStore | None = None,
    artifact_store: _ArtifactStore | None = None,
) -> FastAPI:
    """
    Build a FastAPI app with the sessions router and error handler.

    Mirrors the error-handler registration in ``create_app()`` so
    that ``OmnigentError`` is translated into the correct HTTP
    status rather than surfacing as an unhandled 500.

    :param store: The conversation store stub.
    :param agent_store: The agent store stub. Defaults to a
        pre-populated stub with ``087b7cb7ac30abf4debfaa578d052ec6``.
    :param auth_provider: Auth provider supplying the caller identity, or
        ``None`` (the default) for an auth-disabled app.
    :param file_store: File store stub for fork file-copy tests, or
        ``None`` (the default) to leave file routes unconfigured.
    :param artifact_store: Artifact store stub paired with *file_store*.
    :returns: A configured FastAPI app ready for TestClient.
    """
    if agent_store is None:
        agent_store = _AgentStore(
            agents={
                "087b7cb7ac30abf4debfaa578d052ec6": Agent(
                    id="087b7cb7ac30abf4debfaa578d052ec6",
                    created_at=1,
                    name="test-agent",
                    bundle_location="087b7cb7ac30abf4debfaa578d052ec6/fakehash",
                    version=1,
                ),
            }
        )
    router = create_sessions_router(
        conversation_store=store,  # type: ignore[arg-type]
        agent_store=agent_store,  # type: ignore[arg-type]
        auth_provider=auth_provider,
        file_store=file_store,  # type: ignore[arg-type]
        artifact_store=artifact_store,  # type: ignore[arg-type]
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        """Translate OmnigentError to an HTTP error response."""
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(router, prefix="/v1")
    return app


# The stub's fork_conversation always mints this id (see _ConversationStore).
_FORK_ID = "c538360473d41c84c1eee13918fbeca0"


async def _post_fork_and_drain(
    app: FastAPI, source_id: str, json: dict[str, Any] | None = None
) -> httpx.Response:
    """POST the fork endpoint via an in-loop async client and await the
    background materialization so its effects (fork_calls, files, launches,
    announcements) are observable when the call returns.

    The fork is async: the POST returns 202 immediately and the heavy work
    runs in a task tracked in ``_fork_tasks``. Driving the app on the test's
    own loop (not the sync TestClient's separate loop) lets us await those
    tasks deterministically.
    """
    from omnigent.server.routes._sessions.common import _fork_tasks

    before = set(_fork_tasks)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/sessions/{source_id}/fork", json=json or {})
    # Drain only the task(s) THIS POST spawned, so a shared module set doesn't
    # make one test await another's stragglers.
    await asyncio.gather(*(set(_fork_tasks) - before), return_exceptions=True)
    return resp


async def _post_fork_and_drain_headers(
    app: FastAPI, source_id: str, json: dict[str, Any], headers: dict[str, str]
) -> httpx.Response:
    """Like :func:`_post_fork_and_drain` but with request headers (auth)."""
    from omnigent.server.routes._sessions.common import _fork_tasks

    before = set(_fork_tasks)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/v1/sessions/{source_id}/fork", json=json, headers=headers)
    await asyncio.gather(*(set(_fork_tasks) - before), return_exceptions=True)
    return resp


def _fork_status_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str | None]]:
    """Record every (status, error) the fork task's _publish_fork_status emits."""
    statuses: list[tuple[str, str | None]] = []
    orig = routes_core._publish_fork_status

    def _spy(user_id, source_id, op_id, status, *, fork_id=None, error=None):  # type: ignore[no-untyped-def]
        statuses.append((status, error))
        return orig(user_id, source_id, op_id, status, fork_id=fork_id, error=error)

    monkeypatch.setattr(routes_core, "_publish_fork_status", _spy)
    return statuses


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fork_session_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /sessions/{id}/fork returns 201, clones the agent, and
    binds the fork to the cloned agent.

    Verifies that the route clones the source agent, calls
    fork_conversation with the cloned agent_id, applies runner
    affinity, and returns the correct response shape. A wrong
    response shape or missing agent clone breaks clients that
    reconfigure the forked agent independently.
    """
    conv = _make_conversation()
    items = [
        _make_item("9980c8a9248139f14f4165e5d53088aa", "Hello"),
        _make_item("0fd4e86b2daa009cd9929641dbd7dab6", "World"),
    ]
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={"e9f8f58523cec9a57d3bdf93be543e8c": items},
    )
    agent_store = _AgentStore(
        agents={
            "087b7cb7ac30abf4debfaa578d052ec6": Agent(
                id="087b7cb7ac30abf4debfaa578d052ec6",
                created_at=1,
                name="test-agent",
                bundle_location="087b7cb7ac30abf4debfaa578d052ec6/fakehash",
                version=1,
                description="A test agent",
            ),
        }
    )
    app = _build_app(conv_store, agent_store=agent_store)
    original_resolver = create_validation.resolve_project_session_create
    chokepoint_calls = 0

    async def _recording_resolver(**kwargs: Any) -> Any:
        nonlocal chokepoint_calls
        chokepoint_calls += 1
        return await original_resolver(**kwargs)

    monkeypatch.setattr(create_validation, "resolve_project_session_create", _recording_resolver)

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"title": "My Fork"}
    )

    # The fork is accepted immediately; its materialization ran in the drained
    # background task, so the fork now exists in the store.
    assert resp.status_code == 202, f"Expected 202 Accepted, got {resp.status_code}: {resp.text}"
    accept = resp.json()
    assert accept["source_id"] == "e9f8f58523cec9a57d3bdf93be543e8c"
    assert accept["status"] == "cloning"
    assert accept["operation_id"].startswith("forkop_e9f8f58523cec9a57d3bdf93be543e8c_")
    fork = conv_store._convs[_FORK_ID]
    # The agent_id should be the cloned agent, NOT the original.
    assert fork.agent_id != "087b7cb7ac30abf4debfaa578d052ec6", (
        "Fork should be bound to a cloned agent, not the source's agent"
    )
    assert fork.agent_id is not None and len(fork.agent_id) == 32
    # 2 items copied from the source — proves the store copied the items.
    fork_items = conv_store.list_items(_FORK_ID).data
    item_texts = [
        part["text"]
        for item in fork_items
        for part in (item.data.content if isinstance(item.data, MessageData) else [])
        if isinstance(part, dict) and part.get("type") == "input_text"
    ]
    assert item_texts == ["Hello", "World"], (
        f"Copied items should preserve content and order, got {item_texts}"
    )
    assert fork.title == "My Fork"
    assert chokepoint_calls == 1

    # The agent clone is created INSIDE fork_conversation (atomically), not
    # via a separate agent_store.create — a pre-created row would leak as a
    # phantom built-in on a fork failure. So the route must NOT pre-create,
    # and must hand the clone's bundle/description to the store instead.
    assert len(agent_store.create_calls) == 0, (
        "Route must not pre-create the clone; it's created atomically in the fork txn"
    )

    # Exactly 1 store fork — more means the route called fork_conversation
    # multiple times; 0 means it never forked.
    assert len(conv_store.fork_calls) == 1
    fork_call = conv_store.fork_calls[0]
    assert fork_call["source"] == "e9f8f58523cec9a57d3bdf93be543e8c"
    assert fork_call["title"] == "My Fork"
    # The store receives the clone's bundle/name/description so it can mint
    # the session-scoped agent row in the same transaction.
    assert fork_call["cloned_agent_bundle_location"] == "087b7cb7ac30abf4debfaa578d052ec6/fakehash"
    assert fork_call["cloned_agent_description"] == "A test agent"
    # The clone keeps the source's ROOT name — no "(fork …)" suffix. Being
    # session-scoped it's exempt from the unique built-in-name index, so no
    # disambiguator is needed and the name matches its origin directly.
    assert fork_call["cloned_agent_name"] == "test-agent", (
        f"Cloned agent should keep the source's root name, got {fork_call['cloned_agent_name']!r}"
    )
    assert fork_call["agent_id"] == fork.agent_id, (
        "Fork must bind the same cloned agent id it asked the store to create"
    )


@pytest.mark.asyncio
async def test_fork_background_failure_leaves_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork whose materialization raises publishes ``failed`` and no ghost row.

    The 202 is already sent, so the failure surfaces as a durable
    ``fork_status: failed`` (an actionable toast the client can retry). Because
    nothing is announced until the copy succeeds, a failed fork leaves no
    committed session — the "no ghost session" acceptance criterion.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("disk full")

    monkeypatch.setattr(conv_store, "fork_conversation", _boom)
    app = _build_app(conv_store)
    statuses = _fork_status_spy(monkeypatch)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    # cloning was seeded, then the op failed with the raised message.
    assert ("cloning", None) in statuses
    failed = [error for status, error in statuses if status == "failed"]
    assert failed and "disk full" in (failed[0] or ""), statuses
    # No fork committed and none announced (only the source remains).
    assert _FORK_ID not in conv_store._convs
    assert set(conv_store._convs) == {"e9f8f58523cec9a57d3bdf93be543e8c"}


@pytest.mark.asyncio
async def test_fork_session_shares_source_file_blob_without_copying_bytes() -> None:
    """A fork creates its own file rows but SHARES the source's blob.

    The store gets the old→new id map so copied items reference the fork's
    rows; each fork row's blob_key points at the source's blob, so no bytes
    are duplicated. Without a fork-owned row the fork's session-scoped file
    endpoints 404 for every attachment the copied items reference.
    """
    source_id = "e9f8f58523cec9a57d3bdf93be543e8c"
    src_file_id = "aa11bb22cc33dd44ee55ff6677889900"
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={source_id: conv},
        items_by_conv={source_id: [_make_item("9980c8a9248139f14f4165e5d53088aa", "Hello")]},
    )
    file_store = _FileStore(
        files={
            src_file_id: StoredFile(
                id=src_file_id,
                created_at=1,
                filename="photo.png",
                bytes=4,
                content_type="image/png",
                session_id=source_id,
                blob_key=src_file_id,
            ),
        }
    )
    artifact_store = _ArtifactStore(blobs={src_file_id: b"\x89PNG"})
    app = _build_app(conv_store, file_store=file_store, artifact_store=artifact_store)

    resp = await _post_fork_and_drain(app, source_id, {})

    assert resp.status_code == 202, resp.text
    fork_id = _FORK_ID

    # The store fork received the complete old→new mapping.
    file_id_map = conv_store.fork_calls[0]["file_id_map"]
    assert set(file_id_map) == {src_file_id}
    new_file_id = file_id_map[src_file_id]
    assert new_file_id != src_file_id

    # The fork row is its own (fork-scoped, fresh id) but points at the
    # SOURCE's blob — metadata preserved, no bytes duplicated.
    copied = file_store.files[new_file_id]
    assert copied.session_id == fork_id
    assert copied.filename == "photo.png"
    assert copied.bytes == 4
    assert copied.content_type == "image/png"
    assert copied.blob_key == src_file_id

    # No new blob was written: the artifact store still holds exactly the
    # one source blob, and both the source row and blob are untouched.
    assert set(artifact_store.blobs) == {src_file_id}
    assert artifact_store.blobs[src_file_id] == b"\x89PNG"
    assert file_store.files[src_file_id].session_id == source_id


@pytest.mark.asyncio
async def test_fork_copies_all_file_rows_without_probing_blobs() -> None:
    """The fork carries every source file row as pure metadata — no per-file
    artifact-store probe (an S3 HEAD / Volumes stat is real per-fork latency).

    A source file whose blob is already gone still gets a fork row; it 404s on
    read exactly as the source already does, so probing would only trade
    latency for the same outcome. Forking never fails on a deleted file.
    """
    source_id = "e9f8f58523cec9a57d3bdf93be543e8c"
    live_file_id = "aa11bb22cc33dd44ee55ff6677889900"
    gone_file_id = "bb22cc33dd44ee55ff66778899001122"
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={source_id: conv})
    file_store = _FileStore(
        files={
            live_file_id: StoredFile(
                id=live_file_id,
                created_at=1,
                filename="kept.png",
                bytes=4,
                content_type="image/png",
                session_id=source_id,
            ),
            gone_file_id: StoredFile(
                id=gone_file_id,
                created_at=1,
                filename="lost.png",
                bytes=4,
                content_type="image/png",
                session_id=source_id,
            ),
        }
    )
    # Only the live file has a blob; the fork must not probe either way.
    artifact_store = _ArtifactStore(blobs={live_file_id: b"\x89PNG"})
    app = _build_app(conv_store, file_store=file_store, artifact_store=artifact_store)

    resp = await _post_fork_and_drain(app, source_id, {})

    assert resp.status_code == 202, resp.text
    # Both source files are mapped and copied — no probe-driven skipping.
    file_id_map = conv_store.fork_calls[0]["file_id_map"]
    assert set(file_id_map) == {live_file_id, gone_file_id}
    fork_id = _FORK_ID
    fork_owned = sorted(f.filename for f in file_store.files.values() if f.session_id == fork_id)
    assert fork_owned == ["kept.png", "lost.png"]
    # The fork touched the artifact store zero times (no bytes moved, no HEADs).
    assert artifact_store.exists_calls == 0
    assert set(artifact_store.blobs) == {live_file_id}


@pytest.mark.asyncio
async def test_fork_session_run_config_overrides_pass_through() -> None:
    """The dialog's model / effort / launch-args picks reach the store as
    explicit overrides with their set-flags on.

    Omitting these fields must leave the store on the inherit path
    (``override_*_set=False``), so the flags gate on the request having sent
    each field — a regression here would either drop a user's pick or clobber
    an inherited value the user never touched.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {
            "model_override": "opus",
            "reasoning_effort": "high",
            "terminal_launch_args": ["--permission-mode", "auto"],
        },
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["override_model_override_set"] is True
    assert fork_call["override_model_override"] == "opus"
    assert fork_call["override_reasoning_effort_set"] is True
    assert fork_call["override_reasoning_effort"] == "high"
    assert fork_call["override_terminal_launch_args_set"] is True
    assert fork_call["override_terminal_launch_args"] == ["--permission-mode", "auto"]
    # Explicit launch args ⇒ drop the source's copied mode labels so a stale
    # permission-mode label can't shadow the freshly chosen mode.
    assert "omnigent.claude_native.permission_mode" in fork_call["dropped_label_keys"]


@pytest.mark.asyncio
async def test_fork_session_run_config_omitted_inherits() -> None:
    """A fork with no run-config fields leaves every override unset, so the
    store keeps today's inherit behavior (and drops no mode labels)."""
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["override_model_override_set"] is False
    assert fork_call["override_reasoning_effort_set"] is False
    assert fork_call["override_terminal_launch_args_set"] is False
    # No run-config pick, so the route asks for no conditional drop at all.
    assert fork_call["dropped_label_keys"] == frozenset()


@pytest.mark.asyncio
async def test_fork_session_run_config_clear_aliases() -> None:
    """A "default" model/effort (the picker's Default row) reaches the store as
    a cleared override — set-flag on, value None — resetting the fork to the
    bound agent's default rather than inheriting the source's."""
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {"model_override": "default", "reasoning_effort": "default"},
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["override_model_override_set"] is True
    assert fork_call["override_model_override"] is None
    assert fork_call["override_reasoning_effort_set"] is True
    assert fork_call["override_reasoning_effort"] is None


@pytest.mark.asyncio
async def test_fork_session_400_invalid_model_override() -> None:
    """A shell-/flag-shaped model id is rejected before any fork happens."""
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    client = TestClient(_build_app(conv_store))

    resp = client.post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork",
        json={"model_override": "--rm -rf"},
    )

    assert resp.status_code == 400, f"got {resp.status_code}: {resp.text}"
    assert conv_store.fork_calls == [], "No fork should happen on a bad model override"


@pytest.mark.asyncio
async def test_fork_session_up_to_response_id_passes_through_and_truncates() -> None:
    """``up_to_response_id`` reaches the store and the response is truncated.

    The route must forward the request field to
    ``fork_conversation`` verbatim — dropping it would silently fork
    the full history — and the returned session must contain only the
    items up to the selected response.
    """
    conv = _make_conversation()
    items = [
        _make_item("9980c8a9248139f14f4165e5d53088aa", "Q1", response_id="resp_001"),
        _make_item("0fd4e86b2daa009cd9929641dbd7dab6", "A1", response_id="resp_001"),
        _make_item("8b30166735242c192258d4974f662a5f", "Q2", response_id="resp_002"),
    ]
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={"e9f8f58523cec9a57d3bdf93be543e8c": items},
    )
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"up_to_response_id": "resp_001"}
    )

    assert resp.status_code == 202, f"Expected 202 Accepted, got {resp.status_code}: {resp.text}"
    # The route forwarded the truncation point to the store — None here
    # means the request field was dropped and the fork copied everything.
    assert conv_store.fork_calls[0]["up_to_response_id"] == "resp_001"
    # Only resp_001's two items survive the truncation in the materialized fork;
    # resp_002 appearing means the store ignored the cutoff.
    fork_items = conv_store.list_items(_FORK_ID).data
    assert [item.response_id for item in fork_items] == ["resp_001", "resp_001"], (
        f"Fork should contain only resp_001 items, got {fork_items!r}"
    )


@pytest.mark.asyncio
async def test_fork_accept_carries_no_transcript() -> None:
    """The 202 accept body carries only the operation handle, never items.

    Materialization is a background operation now, so the POST returns before
    any transcript is copied — the whole point of the async fork. This replaces
    the old "response bounded to the newest 100-item page" guarantee, which was
    a property of the synchronous 201 body that no longer exists. The copy
    itself still happens (verified against the store below).
    """
    conv = _make_conversation()
    items = [
        _make_item(f"{index:032x}", f"turn {index}", response_id=f"resp_{index:03d}")
        for index in range(150)
    ]
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={"e9f8f58523cec9a57d3bdf93be543e8c": items},
    )
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"Expected 202 Accepted, got {resp.status_code}: {resp.text}"
    assert "items" not in resp.json(), "the accept handle must not ship the transcript"
    # The materialized fork copied the full source history (the store, not the
    # response, now carries it).
    assert len(conv_store.list_items(_FORK_ID, limit=1000).data) == 150


@pytest.mark.asyncio
async def test_fork_unknown_up_to_response_id_fails_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``up_to_response_id`` matching no response fails the async op.

    The store raises ValueError for an unknown response id (stale client
    state). Since materialization now runs after the 202 accept, the request
    still succeeds and the failure surfaces as a ``fork_status: failed`` event
    (an actionable toast the client can retry) rather than a synchronous 4xx.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Q1")
            ]
        },
    )
    app = _build_app(conv_store)
    statuses: list[tuple[str, str | None]] = []
    orig = routes_core._publish_fork_status

    def _spy(user_id, source_id, op_id, status, *, fork_id=None, error=None):  # type: ignore[no-untyped-def]
        statuses.append((status, error))
        return orig(user_id, source_id, op_id, status, fork_id=fork_id, error=error)

    monkeypatch.setattr(routes_core, "_publish_fork_status", _spy)

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"up_to_response_id": "resp_nope"}
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    # The background op reported failure with the store's message; no fork
    # materialized (the ValueError aborted before the fork conversation exists).
    assert ("cloning", None) in statuses
    assert any(status == "failed" for status, _ in statuses), statuses
    assert _FORK_ID not in conv_store._convs, "a failed fork must leave no committed session"


@pytest.mark.asyncio
async def test_fork_session_404_missing_source() -> None:
    """POST /sessions/{id}/fork returns 404 when source doesn't exist.

    If the route silently creates an empty fork instead of 404, the
    client loses the original conversation's context.
    """
    store = _ConversationStore(conversations={})
    client = TestClient(_build_app(store))

    resp = client.post("/v1/sessions/5eca720dc2bc6cdc3a99028d7bd0f917/fork", json={})

    # The route should return 404, not 500 or 201.
    assert resp.status_code == 404, (
        f"Expected 404 for missing source, got {resp.status_code}: {resp.text}"
    )
    # Verify the error body contains a structured error so clients can
    # distinguish "not found" from generic failures.
    error = resp.json().get("error", {})
    assert error.get("code") == "not_found", f"Expected error code 'not_found', got {error}"


@pytest.mark.asyncio
async def test_fork_session_promotes_sub_agent() -> None:
    """POST /sessions/{id}/fork accepts a sub-agent source.

    Forking a sub-agent is how it is promoted to a top-level session:
    the store always builds the fork parentless, so the copy reaches the
    sidebar and outlives the parent. The route must not reject the
    source for being a child.
    """
    conv = _make_conversation(kind="sub_agent")
    store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, (
        f"Expected 202 promoting a sub-agent, got {resp.status_code}: {resp.text}"
    )
    assert len(store.fork_calls) == 1, f"Expected one fork call, got {store.fork_calls}"


@pytest.mark.asyncio
async def test_fork_session_sub_agent_replaces_presentation_labels() -> None:
    """Promoting a sub-agent recomputes the fork's UI-mode labels.

    A sub-agent carries a wrapper label marking it as somebody's child
    (no terminal of its own — the parent owns the tmux pane). Copying
    that onto a top-level fork would strand it in a child's UI mode, so
    the route supplies the labels for the harness the fork actually
    binds — here an SDK agent, i.e. plain chat (``{}``).
    """
    conv = _make_conversation(kind="sub_agent")
    store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    # None would mean "keep the source's labels" — the child's wrapper.
    assert store.fork_calls[0]["presentation_labels"] == {}, (
        "Promoting a sub-agent must replace its UI-mode labels, got "
        f"{store.fork_calls[0]['presentation_labels']!r}"
    )


@pytest.mark.asyncio
async def test_fork_session_400_no_agent_binding() -> None:
    """POST /sessions/{id}/fork returns 400 when source has no agent_id.

    A conversation without an agent binding is not a session — the
    fork route must reject it so the client doesn't end up with an
    orphaned fork.
    """
    conv = _make_conversation(agent_id=None)
    store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    client = TestClient(_build_app(store))

    resp = client.post("/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork", json={})

    # 400 for invalid request (no agent binding).
    assert resp.status_code == 400, (
        f"Expected 400 for no agent binding, got {resp.status_code}: {resp.text}"
    )
    # Verify the error body explains the rejection reason so clients
    # can surface a meaningful message.
    error = resp.json().get("error", {})
    assert "agent" in error.get("message", "").lower(), (
        f"Error message should mention 'agent' binding, got: {error}"
    )


# ── Agent-switch on fork ─────────────────────────────────────────


class _StubLoadedSpec:
    """Minimal stand-in for ``LoadedAgent.spec`` exposing harness_kind.

    The route's ``_agent_provider_family`` / ``_agent_is_native`` only read
    ``spec.executor.harness_kind``; this real (not MagicMock) stub returns a
    controlled value so the family/native logic runs on a known harness.

    :param harness_kind: The harness id to expose, e.g. ``"claude-native"``.
    """

    def __init__(self, harness_kind: str) -> None:
        """:param harness_kind: Harness id, e.g. ``"claude_sdk"``."""

        class _Executor:
            def __init__(self, hk: str) -> None:
                self.harness_kind = hk

        self.executor = _Executor(harness_kind)


class _StubLoadedAgent:
    """Stand-in for ``AgentCache.load(...)`` result; carries ``.spec``."""

    def __init__(self, harness_kind: str) -> None:
        """:param harness_kind: Harness id to expose on the spec."""
        self.spec = _StubLoadedSpec(harness_kind)


class _StubAgentCache:
    """Agent cache stub mapping agent_id → harness_kind.

    :param harness_by_id: Map of agent_id → harness_kind to return from
        ``load``, e.g. ``{"087b7cb7ac30abf4debfaa578d052ec6": "claude_sdk"}``.
    """

    def __init__(self, harness_by_id: dict[str, str]) -> None:
        """:param harness_by_id: agent_id → harness_kind map."""
        self._harness = harness_by_id

    def load(
        self,
        agent_id: str,
        bundle_location: str,
        *,
        expand_env: bool = False,
    ) -> _StubLoadedAgent:
        """
        Return a loaded-agent stub for *agent_id*.

        :param agent_id: Agent id to resolve, e.g. ``"12c8c7631b209d1027416b4bf7604999"``.
        :param bundle_location: Ignored — the stub keys on agent_id.
        :param expand_env: Ignored — accepted to match the real
            ``AgentCache.load`` signature (this kwarg exists;
            callers pass ``expand_env=agent.session_id is None``). The
            stub returns a fixed harness regardless, but it must accept
            the kwarg or the call raises ``TypeError``, which
            ``_agent_is_native`` swallows and misreports the harness.
        :returns: A :class:`_StubLoadedAgent` with the mapped harness.
        :raises KeyError: If *agent_id* has no mapped harness (a test
            setup error — fail loud rather than silently treating the
            agent as unknown-family).
        """
        del bundle_location, expand_env
        return _StubLoadedAgent(self._harness[agent_id])


def _switch_agent_store() -> _AgentStore:
    """Build an agent store with a source agent and switchable targets.

    :returns: A store holding ``087b7cb7…`` (source), ``280d725b…``
        and ``44b4151dd6cdfed6ee19430832398e05`` (bindable built-ins), and
        ``a98bb825ebd41391c19637c58fe3c0b7`` (a session-scoped agent that must be
        rejected as a switch target).
    """
    return _AgentStore(
        agents={
            "087b7cb7ac30abf4debfaa578d052ec6": Agent(
                id="087b7cb7ac30abf4debfaa578d052ec6",
                created_at=1,
                name="source-agent",
                bundle_location="087b7cb7ac30abf4debfaa578d052ec6/hash",
                version=1,
            ),
            "280d725b404d2915f9e9d6cccce91303": Agent(
                id="280d725b404d2915f9e9d6cccce91303",
                created_at=1,
                name="claude-code",
                bundle_location="280d725b404d2915f9e9d6cccce91303/hash",
                version=1,
            ),
            "44b4151dd6cdfed6ee19430832398e05": Agent(
                id="44b4151dd6cdfed6ee19430832398e05",
                created_at=1,
                name="codex",
                bundle_location="44b4151dd6cdfed6ee19430832398e05/hash",
                version=1,
            ),
            "a98bb825ebd41391c19637c58fe3c0b7": Agent(
                id="a98bb825ebd41391c19637c58fe3c0b7",
                created_at=1,
                name="scoped",
                bundle_location="a98bb825ebd41391c19637c58fe3c0b7/hash",
                version=1,
                session_id="aef8aa8b6e9cf6eda406cb88cf33708c",
            ),
        }
    )


@pytest.mark.asyncio
async def test_fork_switch_binds_target_agent_bundle() -> None:
    """Switching agent clones the TARGET's bundle, not the source's.

    With ``agent_id`` set to a different built-in, the fork must clone
    that agent's bundle into the new session-scoped row. If the route
    cloned the source's bundle instead, the fork would run the wrong
    harness — defeating the switch.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hello")
            ]
        },
    )
    agent_store = _switch_agent_store()
    app = _build_app(conv_store, agent_store=agent_store)

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"agent_id": "44b4151dd6cdfed6ee19430832398e05"}
    )

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    # The clone is minted inside fork_conversation, so the route hands it the
    # TARGET agent's bundle (not ag_test/hash) — not a separate create call.
    assert len(agent_store.create_calls) == 0
    fork_call = conv_store.fork_calls[0]
    assert fork_call["cloned_agent_bundle_location"] == "44b4151dd6cdfed6ee19430832398e05/hash", (
        "Switch must clone the target agent's bundle; cloning the source's "
        "bundle would launch the wrong harness."
    )
    # Clone keeps the TARGET agent's root name ("codex"), proving the
    # response reflects the bound (switched) agent — not the source.
    assert fork_call["cloned_agent_name"] == "codex"
    # The fork binds the cloned agent id it asked the store to create.
    assert fork_call["agent_id"] is not None
    assert fork_call["agent_id"] != "087b7cb7ac30abf4debfaa578d052ec6"


@pytest.mark.asyncio
async def test_fork_switch_drops_claude_permission_mode_label() -> None:
    """An agent switch drops the source's claude-native permission-mode label.

    That label is Claude-specific and rides alongside launch args, which a
    switching fork already drops. Carrying the label onto a switched fork would
    leave stale mode metadata that could hydrate a wrong mode in native-wrapper
    UI state, so the route lists it in ``dropped_label_keys`` whenever the agent
    changes — independent of whether the dialog sent explicit launch args.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hello")
            ]
        },
    )
    app = _build_app(conv_store, agent_store=_switch_agent_store())

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"agent_id": "44b4151dd6cdfed6ee19430832398e05"}
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    dropped = conv_store.fork_calls[0]["dropped_label_keys"]
    assert "omnigent.claude_native.permission_mode" in dropped, (
        f"agent switch must drop the claude permission-mode label, got {dropped!r}"
    )


@pytest.mark.asyncio
async def test_fork_same_agent_keeps_permission_mode_label() -> None:
    """A same-agent fork with no explicit launch args drops NO mode labels.

    The permission-mode label is Claude-specific but valid for a same-agent
    fork (same CLI), so it must carry over — the drop is gated on an agent
    switch or an explicit launch-args pick, neither of which applies here.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    # Nothing conditional to drop, so the permission-mode label carries over.
    assert conv_store.fork_calls[0]["dropped_label_keys"] == frozenset()


@pytest.mark.asyncio
async def test_fork_codex_bypass_stamps_label_on_codex_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bypass opt-in stamps the codex label via ``extra_labels``.

    The source's own bypass label is always dropped (instance-scoped), so this
    explicit, banner-gated request field is the ONLY path that arms bypass on a
    fork — and the store applies ``extra_labels`` AFTER the drop, so the opt-in
    wins. Gated on the target actually being codex-native.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    # The codex target (44b4…) must report codex-native so the route stamps.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache(
            {
                "087b7cb7ac30abf4debfaa578d052ec6": "claude_sdk",
                "44b4151dd6cdfed6ee19430832398e05": "codex-native",
            }
        ),
    )
    app = _build_app(conv_store, agent_store=_switch_agent_store())

    resp = await _post_fork_and_drain(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {"agent_id": "44b4151dd6cdfed6ee19430832398e05", "codex_bypass_sandbox": True},
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    extra = conv_store.fork_calls[0]["extra_labels"]
    assert extra == {"omnigent.codex_native.bypass_sandbox": "1"}, (
        f"bypass opt-in must stamp the codex bypass label, got {extra!r}"
    )


@pytest.mark.asyncio
async def test_fork_codex_bypass_rejected_on_non_codex_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arming bypass for a non-codex target is a 400 (the label is inert there).

    Refusing rather than silently ignoring keeps the dangerous flag from being
    set on a fork it can't apply to — the request is a client bug.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache(
            {
                "087b7cb7ac30abf4debfaa578d052ec6": "claude_sdk",
                "280d725b404d2915f9e9d6cccce91303": "claude-native",
            }
        ),
    )
    client = TestClient(_build_app(conv_store, agent_store=_switch_agent_store()))

    resp = client.post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork",
        json={"agent_id": "280d725b404d2915f9e9d6cccce91303", "codex_bypass_sandbox": True},
    )

    assert resp.status_code == 400, f"got {resp.status_code}: {resp.text}"
    assert conv_store.fork_calls == [], "no fork should happen on an invalid bypass opt-in"


@pytest.mark.asyncio
async def test_fork_switch_404_session_scoped_target() -> None:
    """Switching to a session-scoped agent is rejected with 404.

    A session-scoped agent (``session_id`` set) belongs to one
    conversation — possibly another user's. Binding it to a fork would
    leak/alias it across sessions, so only built-in agents are bindable.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    client = TestClient(_build_app(conv_store, agent_store=_switch_agent_store()))

    resp = client.post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork",
        json={"agent_id": "a98bb825ebd41391c19637c58fe3c0b7"},
    )

    assert resp.status_code == 404, (
        f"Expected 404 for session-scoped target, got {resp.status_code}: {resp.text}"
    )
    # No fork happened — the route rejected before cloning/forking.
    assert conv_store.fork_calls == []


@pytest.mark.asyncio
async def test_fork_switch_404_unknown_target() -> None:
    """Switching to a non-existent agent id is rejected with 404."""
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    client = TestClient(_build_app(conv_store, agent_store=_switch_agent_store()))

    resp = client.post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork",
        json={"agent_id": "566230693b3591362a085cffdb224484"},
    )

    assert resp.status_code == 404, (
        f"Expected 404 for unknown target, got {resp.status_code}: {resp.text}"
    )
    assert conv_store.fork_calls == []


@pytest.mark.parametrize(
    "source_harness,target_harness,expect_copy_model,expect_carry,"
    "expect_resume_source,expect_presentation",
    [
        # SDK → native, same provider family: model settings carry AND the
        # fork is marked for native transcript rebuild (the headline case).
        # The clone becomes terminal-first (claude-code-native-ui).
        (
            "claude_sdk",
            "claude-native",
            True,
            True,
            True,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"},
        ),
        # cross-family into a native target: model id is meaningless across
        # providers → reset. History still carries — the runner rebuilds the
        # native transcript from the copied Omnigent items — but the source's
        # native session id must NOT be stamped (wrong transcript format for
        # the target; a doomed clone attempt would launch fresh instead).
        # Still terminal-first, but the codex wrapper.
        (
            "claude_sdk",
            "codex-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"},
        ),
        # cursor target carries history via a text preamble (its conversation
        # is server-backed, so the runner can't seed a local store for --resume),
        # so carry_history_into_native IS stamped — the runner branches on the
        # harness to choose preamble vs transcript rebuild.
        (
            "claude_sdk",
            "cursor-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "cursor-native-ui"},
        ),
        # pi-native CAN carry fork history: the runner rebuilds Pi's JSONL
        # session file from the copied Omnigent items. Cross-family from a
        # claude SDK source, so model settings reset and the source's native
        # session id is NOT stamped (Pi rebuilds from items, not a source
        # file) — same shape as the codex-native cross-family case.
        (
            "claude_sdk",
            "pi-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "pi-native-ui"},
        ),
        # qwen-native CAN carry fork history: the runner rebuilds qwen's on-disk
        # chat recording (+ runtime/meta sidecars) from the copied Omnigent items
        # (see write_qwen_session_recording). Cross-family here (claude SDK source
        # is anthropic, qwen is openai-family), so model settings reset and the
        # source's native session id is NOT stamped — same shape as the pi-native
        # cross-family case.
        (
            "claude_sdk",
            "qwen-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "qwen-native-ui"},
        ),
        # native → SDK, same family: model carries, but an SDK target
        # replays the transcript itself so no native-rebuild marker is set.
        # The clone drops terminal-first mode (chat) — the bug this fixes.
        ("claude-native", "claude_sdk", True, False, True, {}),
        # cross-family into native (openai source → anthropic native): reset
        # model settings, carry history via rebuild-from-items, skip the
        # source-session directive — same as the SDK cross-family case.
        # Terminal-first.
        (
            "openai-agents",
            "claude-native",
            False,
            True,
            False,
            {"omnigent.ui": "terminal", "omnigent.wrapper": "claude-code-native-ui"},
        ),
    ],
)
@pytest.mark.asyncio
async def test_fork_switch_model_and_carry_gating(
    monkeypatch: pytest.MonkeyPatch,
    source_harness: str,
    target_harness: str,
    expect_copy_model: bool,
    expect_carry: bool,
    expect_resume_source: bool,
    expect_presentation: dict[str, str],
) -> None:
    """The switch gates model copy + native carry + UI mode on the target.

    A model id is provider-bound, so ``copy_model_settings`` must be True
    only within a family. ``carry_history_into_native`` must be True for
    native targets that carry fork history (claude/codex/pi rebuild a
    transcript, cursor replays a text preamble), and SDK targets replay
    history themselves so they never set it. ``resume_source_native_session``
    must be False on a cross-family switch so the store skips the fork-source
    directive (the source's native transcript is the wrong format; a clone
    attempt would fail and launch fresh). ``presentation_labels`` must reflect the TARGET
    harness so the clone's UI mode is right — an SDK target drops
    terminal-first mode (``{}``), a native target sets it; copying the
    source's would leave an SDK clone of a native session with a stale
    interactive terminal.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    agent_store = _switch_agent_store()
    # Target every switch at ag_claude_native; the stub cache, not the
    # bundle, dictates the harness each agent reports.
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache(
            {
                "087b7cb7ac30abf4debfaa578d052ec6": source_harness,
                "280d725b404d2915f9e9d6cccce91303": target_harness,
            }
        ),
    )
    app = _build_app(conv_store, agent_store=agent_store)

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"agent_id": "280d725b404d2915f9e9d6cccce91303"}
    )

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["copy_model_settings"] is expect_copy_model, (
        f"{source_harness}->{target_harness}: copy_model_settings should be "
        f"{expect_copy_model} (model id is provider-bound)."
    )
    assert fork_call["copy_terminal_launch_args"] is False, (
        f"{source_harness}->{target_harness}: an agent switch must NOT carry the "
        f"source's launch args (CLI-specific flags break a different target CLI)."
    )
    assert fork_call["carry_history_into_native"] is expect_carry, (
        f"{source_harness}->{target_harness}: carry_history_into_native should "
        f"be {expect_carry} (only native harnesses with replayable fork history)."
    )
    assert fork_call["resume_source_native_session"] is expect_resume_source, (
        f"{source_harness}->{target_harness}: resume_source_native_session should "
        f"be {expect_resume_source} (the source's native session id is only "
        f"resumable within the same provider family)."
    )
    assert fork_call["presentation_labels"] == expect_presentation, (
        f"{source_harness}->{target_harness}: presentation_labels should be "
        f"{expect_presentation} so the clone's UI mode matches the target "
        f"harness, got {fork_call['presentation_labels']!r}."
    )


@pytest.mark.asyncio
async def test_fork_no_switch_native_source_carries_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-agent fork of a native source still marks native carry.

    Without an ``agent_id`` the fork keeps the source's (native) agent, so
    the runner must still rebuild the native transcript — otherwise a plain
    clone of a Claude-Code session would resume with no history. Model
    settings always copy on a same-agent fork.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"087b7cb7ac30abf4debfaa578d052ec6": "claude-native"}),
    )
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["copy_model_settings"] is True
    assert fork_call["copy_terminal_launch_args"] is True, (
        "A same-agent fork keeps the same CLI, so its launch args stay valid and must carry over."
    )
    assert fork_call["carry_history_into_native"] is True, (
        "A same-agent fork of a native source must mark native carry so the "
        "runner rebuilds the transcript instead of resuming blank."
    )
    # Not switching → keep the source's copied UI labels untouched.
    assert fork_call["presentation_labels"] is None, (
        "A same-agent fork must not recompute presentation labels (None); "
        "the copied source labels are already correct."
    )


@pytest.mark.parametrize(
    "harness,expect_carry",
    [
        # cursor carries history via a text preamble the runner replays on the
        # first message (its conversation is server-backed, so no local store to
        # seed for --resume) — so a same-agent fork DOES mark native carry.
        ("cursor-native", True),
        # pi rebuilds its JSONL session file from the copied Omnigent items
        # (it is in _FORK_HISTORY_NATIVE_HARNESSES), so a same-agent fork marks
        # native carry — parity with claude/codex.
        ("pi-native", True),
    ],
)
@pytest.mark.asyncio
async def test_fork_cursor_pi_native_carry_gating(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expect_carry: bool,
) -> None:
    """A same-agent fork marks native carry for both cursor and pi.

    cursor carries fork history via a text preamble (its conversation is
    server-backed, so no local store to seed for --resume); pi rebuilds its
    JSONL session file from the copied Omnigent items. Both therefore mark
    ``carry_history_into_native``.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"087b7cb7ac30abf4debfaa578d052ec6": harness}),
    )
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["carry_history_into_native"] is expect_carry, (
        f"A {harness} fork should set carry_history_into_native={expect_carry}."
    )


@pytest.mark.parametrize(
    "harness,expect_carry",
    [
        # Reversed native spellings ("native-claude" / "native-codex") are
        # valid harness ids that canonicalize_harness passes through unchanged,
        # so the carry gate must recognize them just like their canonical
        # spellings — otherwise an identically-behaving agent silently loses
        # fork history. cursor carries (preamble); ``native-pi`` IS aliased to
        # ``pi-native`` (which is in the set), so it carries too (rebuild).
        ("native-claude", True),
        ("native-codex", True),
        ("native-cursor", True),
        ("native-pi", True),
    ],
)
@pytest.mark.asyncio
async def test_fork_reversed_native_spelling_carry_gating(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expect_carry: bool,
) -> None:
    """The carry gate honors reversed native spellings like the canonical ones.

    ``canonicalize_harness`` aliases ``native-pi`` to ``pi-native``; the other
    reversed spellings pass through unchanged, so the predicate lists both
    forms explicitly. claude/codex/cursor/pi all carry fork history (claude /
    codex / pi via transcript rebuild, cursor via preamble).
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"087b7cb7ac30abf4debfaa578d052ec6": harness}),
    )
    app = _build_app(conv_store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["carry_history_into_native"] is expect_carry, (
        f"A {harness} fork should set carry_history_into_native={expect_carry}: "
        "reversed native spellings must be treated like their canonical form."
    )


# ── Managed-sandbox fork ─────────────────────────────────────────


def _arm_managed_app(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, *, provider: str = "modal"
) -> list[dict[str, Any]]:
    """
    Wire the app for managed forks and capture the scheduled launches.

    Supplies the three ``app.state`` pieces the managed guard requires
    (sandbox deployment, host store, launch tracker) and replaces the
    background launch with a recorder, so a managed fork exercises the
    real route path without provisioning anything.

    :param app: The app under test.
    :param monkeypatch: Fixture used to swap the background launch.
    :param provider: Sandbox provider to configure. ``"modal"`` (default) is
        single-repo; pass ``"kubernetes"`` for a multi-repo provider.
    :returns: The list the recorder appends each launch's kwargs to.
    """
    app.state.sandbox_config = parse_sandbox_config(
        {"provider": provider, "server_url": "https://managed-test.example.com"}
    )
    # Never dereferenced: the recorder replaces the only consumer.
    app.state.host_store = object()
    app.state.managed_launches = ManagedLaunchTracker()
    launches: list[dict[str, Any]] = []

    async def _record(**kwargs: Any) -> None:
        """Record the launch the route scheduled instead of provisioning."""
        launches.append(kwargs)

    monkeypatch.setattr(routes_core, "_run_managed_launch", _record)
    return launches


@pytest.mark.asyncio
async def test_fork_managed_schedules_sandbox_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A managed fork schedules the same background sandbox launch a create does.

    The clone is the launch's subject, not the source: binding the source's
    id here would provision a second sandbox for the session the user is
    cloning FROM and leave the clone unbound.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    launches = _arm_managed_app(app, monkeypatch)

    resp = await _post_fork_and_drain(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {"host_type": "managed", "sandbox_provider": "modal"},
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    assert len(launches) == 1, "a managed fork must schedule exactly one sandbox launch"
    launch = launches[0]
    assert launch["session_id"] == _FORK_ID
    assert launch["provider"] == "modal"
    # No repository on either side, so the clone gets an empty sandbox.
    assert launch["repos"] == []
    assert conv_store.label_writes == []


@pytest.mark.asyncio
async def test_fork_managed_launch_uses_session_scoped_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The managed launch is handed the fork's OWN session-scoped agent clone.

    Only a genuine built-in may classify a managed runner (the ``omnigent.ai/
    agent`` label an admission policy selects on to inject a privileged
    credential). A fork always binds a session-scoped clone, which fails that
    gate — so a forked runner carries no classifier and attracts no injected
    credential. Passing the built-in the clone derives from would re-stamp the
    label and hand a clone of anyone's session the built-in's credential.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    agent_store = _AgentStore(
        agents={
            "087b7cb7ac30abf4debfaa578d052ec6": Agent(
                id="087b7cb7ac30abf4debfaa578d052ec6",
                created_at=1,
                name="code-reviewer",
                bundle_location="087b7cb7ac30abf4debfaa578d052ec6/hash",
                version=1,
            ),
            # The built-in a "switch the fork's agent" pick would name.
            builtin_agent_id("code-reviewer"): Agent(
                id=builtin_agent_id("code-reviewer"),
                created_at=1,
                name="code-reviewer",
                bundle_location="builtin/hash",
                version=1,
            ),
        }
    )
    app = _build_app(conv_store, agent_store=agent_store)
    launches = _arm_managed_app(app, monkeypatch)

    resp = await _post_fork_and_drain(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {"host_type": "managed", "agent_id": builtin_agent_id("code-reviewer")},
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    fork_agent_id = conv_store._convs[_FORK_ID].agent_id
    launch_agent_id = launches[0]["agent_id"]
    assert launch_agent_id == fork_agent_id, (
        "the launch must classify on the fork's own bound agent, not the source's"
    )
    assert launch_agent_id != builtin_agent_id("code-reviewer"), (
        "a fork must never launch under the built-in id — that would restore the "
        "runner's agent classifier and with it the injected credential"
    )
    # The gate the classifier applies to that id: a fork's clone is
    # session-scoped, so it resolves to no label.
    assert (
        resolve_managed_agent_label(
            _AgentStore(
                agents={
                    fork_agent_id: Agent(
                        id=fork_agent_id,
                        created_at=1,
                        name="code-reviewer",
                        bundle_location="builtin/hash",
                        version=1,
                        session_id=_FORK_ID,
                    ),
                }
            ),  # type: ignore[arg-type]
            fork_agent_id,
            session_id=_FORK_ID,
        )
        is None
    ), "a forked session's runner must carry no omnigent.ai/agent classifier"


@pytest.mark.asyncio
async def test_fork_managed_registers_sandbox_to_forking_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fork's sandbox is owned by whoever forked, not by the source's owner.

    ``owner`` becomes the ``hosts`` row's ``user_id``, and that is the single
    identity ``GET /v1/hosts/{id}/credentials/{provider}`` resolves a vended
    credential from (see ``routes/host_credentials.py``, which reads
    ``resolve_launch_token(...).user_id``). Passing the source's owner here
    would hand the forker the source owner's GitHub token — a credential
    crossing from one user to another on a plain read-access fork.
    """
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store, auth_provider=UnifiedAuthProvider(source="header"))
    launches = _arm_managed_app(app, monkeypatch)

    resp = await _post_fork_and_drain_headers(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {"host_type": "managed"},
        {"X-Forwarded-Email": "forker@example.com"},
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    assert launches[0]["owner"] == "forker@example.com"


@pytest.mark.asyncio
async def test_fork_managed_inherits_source_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An omitted workspace clones the repository the SOURCE recorded.

    Cloning a sandbox session should land in the same checkout; without the
    inherit the clone would come up in an empty sandbox and the copied
    transcript's file references would resolve to nothing.
    """
    conv = _make_conversation(
        labels={MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo#release-1.2"}
    )
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    launches = _arm_managed_app(app, monkeypatch)

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"host_type": "managed"}
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    repos = launches[0]["repos"]
    assert len(repos) == 1
    assert repos[0].url == "https://github.com/org/repo"
    assert repos[0].branch == "release-1.2"
    # Recorded on the FORK too (one label per repo, plus the bare compat key),
    # so its own sandbox relaunch re-clones it. The source here uses the legacy
    # single-value label, exercising the read fallback.
    assert conv_store.label_writes == [
        (
            _FORK_ID,
            {
                f"{MANAGED_REPO_LABEL_KEY}.0": "https://github.com/org/repo#release-1.2",
                MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo#release-1.2",
            },
        )
    ]


@pytest.mark.asyncio
async def test_fork_managed_inherits_all_source_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-repo source hands the fork EVERY repository it recorded.

    The source records one label per repo; the fork reads them all back and
    re-stamps its own per-repo labels — cloning a multi-repo sandbox session
    lands the fork with all of its checkouts, not just the first.
    """
    conv = _make_conversation(
        labels={
            f"{MANAGED_REPO_LABEL_KEY}.0": "https://github.com/org/api#main",
            f"{MANAGED_REPO_LABEL_KEY}.1": "https://github.com/org/web",
        }
    )
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    # Multi-repo provider — inheriting several repos is only allowed where the
    # provider supports it (a single-repo provider rejects it; see the guard test).
    launches = _arm_managed_app(app, monkeypatch, provider="kubernetes")

    resp = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"host_type": "managed"}
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    repos = launches[0]["repos"]
    assert [(r.url, r.branch) for r in repos] == [
        ("https://github.com/org/api", "main"),
        ("https://github.com/org/web", None),
    ]
    # The fork re-stamps its own per-repo labels (plus the bare compat key) for
    # relaunch.
    assert conv_store.label_writes == [
        (
            _FORK_ID,
            {
                f"{MANAGED_REPO_LABEL_KEY}.0": "https://github.com/org/api#main",
                f"{MANAGED_REPO_LABEL_KEY}.1": "https://github.com/org/web",
                MANAGED_REPO_LABEL_KEY: "https://github.com/org/api#main https://github.com/org/web",
            },
        )
    ]


@pytest.mark.asyncio
async def test_fork_managed_explicit_workspace_overrides_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit workspace wins over the source's recorded repository.

    ``null`` is a real choice here (an empty sandbox), so the route must
    branch on the field being SENT, not on its value — otherwise a caller
    asking for an empty sandbox would silently get the source's repo.
    """
    conv = _make_conversation(labels={MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo"})
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    launches = _arm_managed_app(app, monkeypatch)

    chosen = await _post_fork_and_drain(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {"host_type": "managed", "workspace": "https://github.com/org/other"},
    )
    emptied = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"host_type": "managed", "workspace": None}
    )

    assert chosen.status_code == 202, f"got {chosen.status_code}: {chosen.text}"
    assert emptied.status_code == 202, f"got {emptied.status_code}: {emptied.text}"
    assert [r.url for r in launches[0]["repos"]] == ["https://github.com/org/other"]
    assert launches[1]["repos"] == [], "an explicit null workspace means an empty sandbox"


@pytest.mark.asyncio
async def test_fork_never_inherits_source_sandbox_repo_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork that cleared its repository does not keep the source's label.

    The label is per-session state a sandbox RELAUNCH re-clones from
    (``orchestration._maybe_relaunch_managed_sandbox``). The store copies
    source labels by default, so without the fork-only drop a fork that
    asked for an empty sandbox would boot empty and then have the source's
    repo re-cloned into it on the first relaunch — silently undoing the
    user's choice. An external fork of a sandbox source must not carry it
    either: the clone has no sandbox, and the stale label would seed the
    fork dialog's own repository prefill. The drop is unconditional in the
    store, so the route asks for nothing here; this covers the route's two
    entries into that path.
    """
    conv = _make_conversation(labels={MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo"})
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    _arm_managed_app(app, monkeypatch)

    emptied = await _post_fork_and_drain(
        app, "e9f8f58523cec9a57d3bdf93be543e8c", {"host_type": "managed", "workspace": None}
    )
    external = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert emptied.status_code == 202, f"got {emptied.status_code}: {emptied.text}"
    assert external.status_code == 202, f"got {external.status_code}: {external.text}"
    # Neither clone ends up carrying one, so no relaunch re-clones. Both POSTs
    # mint the same stub fork id, so the final materialized row reflects the
    # last (external) fork; both took the drop path.
    fork = conv_store.get_conversation(_FORK_ID)
    assert fork is not None
    assert MANAGED_REPO_LABEL_KEY not in fork.labels
    assert conv_store.label_writes == [], "no workspace resolved, so nothing to re-stamp"


@pytest.mark.asyncio
async def test_fork_managed_restamps_resolved_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repository the fork DID resolve lands back on its own label.

    The drop above is unconditional, so the managed launch re-stamping the
    resolved repository is the only thing that keeps a fork's own sandbox
    relaunchable into the same checkout.
    """
    conv = _make_conversation(labels={MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo"})
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    _arm_managed_app(app, monkeypatch)

    resp = await _post_fork_and_drain(
        app,
        "e9f8f58523cec9a57d3bdf93be543e8c",
        {"host_type": "managed", "workspace": "https://github.com/org/other#dev"},
    )

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    fork = conv_store.get_conversation(_FORK_ID)
    assert fork is not None
    assert fork.labels[f"{MANAGED_REPO_LABEL_KEY}.0"] == "https://github.com/org/other#dev"


@pytest.mark.asyncio
async def test_fork_external_schedules_no_sandbox_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (external) fork stays unbound — no sandbox is provisioned.

    A managed-capable server must not start spending on a sandbox for every
    clone; compute is opt-in.
    """
    conv = _make_conversation(labels={MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo"})
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    launches = _arm_managed_app(app, monkeypatch)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"got {resp.status_code}: {resp.text}"
    assert launches == [], "an external fork must not provision a sandbox"


# The managed pre-flight guards (server-not-configured / provider-not-offered /
# multi-repo-on-single-repo) run synchronously BEFORE the 202 accept, so a
# misconfigured managed fork fails fast with a 400 (naming the misconfiguration)
# instead of a clone stuck "provisioning" forever. These use the sync client:
# validation raises before any background task spawns.
@pytest.mark.asyncio
async def test_fork_managed_rejects_unconfigured_server() -> None:
    """A managed fork on a server with no ``sandbox:`` config is a synchronous 400."""
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    client = TestClient(_build_app(conv_store))

    resp = client.post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork", json={"host_type": "managed"}
    )

    assert resp.status_code == 400, f"got {resp.status_code}: {resp.text}"
    assert "managed hosts are not configured" in resp.text
    assert conv_store.fork_calls == [], "a rejected managed fork must not fork"


@pytest.mark.asyncio
async def test_fork_managed_rejects_unoffered_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider this server doesn't offer is a synchronous 400 naming what it has."""
    conv = _make_conversation()
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    launches = _arm_managed_app(app, monkeypatch)
    client = TestClient(app)

    resp = client.post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork",
        json={"host_type": "managed", "sandbox_provider": "daytona"},
    )

    assert resp.status_code == 400, f"got {resp.status_code}: {resp.text}"
    assert "is not configured on this server" in resp.text
    assert launches == []
    assert conv_store.fork_calls == []


@pytest.mark.asyncio
async def test_fork_managed_rejects_multiple_repos_on_single_repo_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-repo source forked onto a single-repo provider is a synchronous 400
    (modal is exec-model → single-repo). The web picker caps this per provider;
    this guards the API."""
    conv = _make_conversation(
        labels={
            f"{MANAGED_REPO_LABEL_KEY}.0": "https://github.com/org/api#main",
            f"{MANAGED_REPO_LABEL_KEY}.1": "https://github.com/org/web",
        }
    )
    conv_store = _ConversationStore(conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv})
    app = _build_app(conv_store)
    launches = _arm_managed_app(app, monkeypatch)  # provider "modal" (single-repo)
    client = TestClient(app)

    resp = client.post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork", json={"host_type": "managed"}
    )

    assert resp.status_code == 400, f"got {resp.status_code}: {resp.text}"
    assert "clones only one repository" in resp.text
    assert launches == [], "a rejected multi-repo fork must schedule no launch"
    assert conv_store.fork_calls == []


@pytest.mark.asyncio
async def test_fork_clone_reuses_source_agent_name_verbatim() -> None:
    """The fork clone reuses the source agent's name as-is — no suffix added."""
    conv = _make_conversation(agent_id="30f9aa4d441e344d3eb273f8cc13e4a5")
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    agent_store = _AgentStore(
        agents={
            "30f9aa4d441e344d3eb273f8cc13e4a5": Agent(
                id="30f9aa4d441e344d3eb273f8cc13e4a5",
                created_at=1,
                name="claude-native-ui",
                bundle_location="3a9725fd4de1720e83e53a632da41da8/hash",
                version=1,
            ),
        }
    )
    app = _build_app(conv_store, agent_store=agent_store)

    resp = await _post_fork_and_drain(app, "e9f8f58523cec9a57d3bdf93be543e8c", {})

    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    assert conv_store.fork_calls[0]["cloned_agent_name"] == "claude-native-ui", (
        "Fork clone should reuse the source name verbatim, no '(fork …)' suffix"
    )
