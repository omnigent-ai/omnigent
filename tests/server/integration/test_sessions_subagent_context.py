"""Integration tests for sub-agent context inheritance and scoping.

A sub-agent (child) session is created via ``POST /v1/sessions`` with
``parent_session_id`` set. These tests pin down three guarantees the
"child operates with the parent's context without re-pasting it" flow
relies on, all at the persistence layer (in-process ASGI ``client``,
no runner bound, no LLM):

- **Runner co-location** — a child inherits the parent's ``runner_id``
  so it lands on the same runner and therefore shares the same on-disk
  workspace. That shared filesystem (not a transcript copy) is how a
  child reads the files the parent produced without them being re-sent.
- **Transcript isolation** — a child does NOT inherit the parent's
  conversation items (contrast with fork, which copies them). Context
  reaches the child only via the explicit seed message, never by
  implicit history bleed.
- **Per-child scoping** — a message seeded on one child lands only on
  that child, and ``child_sessions`` lists only direct children, so the
  Agents surface targets and enumerates each sub-agent independently.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities.conversation import (
    Conversation,
    MessageData,
    NewConversationItem,
)
from omnigent.host.frames import HostHelloFrame
from omnigent.runtime import set_runner_router
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes._sessions.common import (
    _CLAUDE_NATIVE_WRAPPER_LABEL_KEY,
    _CODEX_NATIVE_WRAPPER_LABEL_VALUE,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.budgets import budget
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import build_agent_bundle, create_test_agent
from tests.server.integration.test_sessions_tunnel_three_layer import (
    _connect_runner_tunnel,
)
from tests.server.integration.test_sessions_tunnel_three_layer import (
    _send_hello_and_wait as _send_runner_hello_and_wait,
)

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────


async def _create_parent_session(
    client: httpx.AsyncClient,
    *,
    agent_name: str,
) -> dict[str, Any]:
    """Create a top-level parent session bound to a fresh agent.

    :param client: The test HTTP client.
    :param agent_name: Unique agent name (agent_store is unique-by-name).
    :returns: The ``POST /v1/sessions`` response body.
    """
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, f"parent session create failed: {resp.text}"
    return resp.json()


async def _create_child_session(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    agent_name: str,
    title: str = "worker:child",
    initial_message: str | None = None,
) -> dict[str, Any]:
    """Create a child session under ``parent_session_id``.

    Uses the current ``POST /v1/sessions`` child-session contract:
    ``parent_session_id`` set, the child bound to its own agent.

    :param client: The test HTTP client.
    :param parent_session_id: Existing parent session id.
    :param agent_name: Unique name for the child's agent.
    :param title: Child title in ``"{tool}:{name}"`` form.
    :param initial_message: Optional user message to seed the child with.
    :returns: The ``POST /v1/sessions`` response body for the child.
    """
    agent = await create_test_agent(client, name=agent_name)
    payload: dict[str, Any] = {
        "agent_id": agent["id"],
        "parent_session_id": parent_session_id,
        "title": title,
    }
    if initial_message is not None:
        payload["initial_items"] = [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": initial_message}],
                },
            },
        ]
    resp = await client.post("/v1/sessions", json=payload)
    assert resp.status_code == 201, f"child session create failed: {resp.text}"
    return resp.json()


# ── Runner co-location (shared workspace) ────────────────


@pytest.mark.parametrize(
    "parent_runner_id",
    ["runner_colocated_7c2a", None],
    ids=["parent-has-runner", "parent-has-no-runner"],
)
async def test_child_inherits_parent_runner_affinity(
    client: httpx.AsyncClient,
    db_uri: str,
    parent_runner_id: str | None,
) -> None:
    """A child inherits whatever ``runner_id`` the parent is pinned to.

    Co-locating the child on the parent's runner is what gives the child
    the parent's on-disk workspace, so it can read parent-produced files
    without them being re-pasted. The two cases prove the child copies
    the parent's *actual* value: a pinned runner propagates verbatim, and
    an unpinned parent yields ``None`` (no spurious default).
    """
    parent = await _create_parent_session(client, agent_name=f"ctx-parent-{parent_runner_id}")
    if parent_runner_id is not None:
        conv_store = SqlAlchemyConversationStore(db_uri)
        # Fresh parent starts with runner_id NULL, so the NULL-guarded
        # pin must win. A False here means the parent was already pinned
        # (test setup drift), which would invalidate the assertion below.
        assert conv_store.set_runner_id(parent["id"], parent_runner_id) is True

    child = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name=f"ctx-child-{parent_runner_id}",
    )

    snap = await client.get(f"/v1/sessions/{child['id']}")
    assert snap.status_code == 200, snap.text
    # The child's runner_id must equal the parent's. If it diverges, the
    # inherit-runner-affinity branch in the create handler regressed and
    # the child would be dispatched to a different runner (a different
    # workspace), breaking file sharing. None must stay None — never a
    # fabricated default.
    assert snap.json()["runner_id"] == parent_runner_id
    assert snap.json()["parent_session_id"] == parent["id"]


# ── Transcript isolation (no implicit history bleed) ─────


async def test_child_does_not_inherit_parent_transcript(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child starts with an empty transcript — parent items don't bleed in.

    Unlike fork (which copies the source transcript), a sub-agent child
    is isolated: the parent's prior conversation never appears in the
    child's history. This is the boundary the "without re-pasting"
    contract sits on — context must be handed to the child explicitly,
    not inherited implicitly.
    """
    parent_marker = "PARENT-DESIGN-DISCUSSION [marker-a91f]"
    parent = await _create_parent_session(client, agent_name="ctx-iso-parent")

    # Seed the parent with a design-discussion item directly in the store
    # (no runner is bound, so a posted message event would 503 before
    # persisting). This is the context a naive "inheritance" would copy.
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        parent["id"],
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": parent_marker}],
                ),
            ),
        ],
    )

    child = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name="ctx-iso-child",
    )

    child_items = (await client.get(f"/v1/sessions/{child['id']}/items")).json()["data"]
    # The child has no inherited history. A non-empty list here means the
    # create path leaked the parent transcript into the child (an
    # accidental fork), which is exactly what isolation forbids.
    assert child_items == []

    # Sanity: the parent still owns its item — isolation is one-directional
    # absence on the child, not deletion from the parent.
    parent_items = (await client.get(f"/v1/sessions/{parent['id']}/items")).json()["data"]
    assert parent_marker in json.dumps(parent_items)


# ── Per-child message scoping ────────────────────────────


async def test_sibling_children_seed_context_is_isolated(
    client: httpx.AsyncClient,
) -> None:
    """A message seeded on one child reaches only that child, not its sibling.

    Two children under one parent each get a distinct seed. Each child's
    transcript must contain its own marker and neither the sibling's nor
    leak onto the parent — proving a targeted message addresses a single
    sub-agent rather than fanning out across the tree.
    """
    marker_a = "TASK-FOR-CHILD-A [marker-aaa1]"
    marker_b = "TASK-FOR-CHILD-B [marker-bbb2]"
    parent = await _create_parent_session(client, agent_name="ctx-sib-parent")

    child_a = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name="ctx-sib-child-a",
        title="worker:a",
        initial_message=marker_a,
    )
    child_b = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name="ctx-sib-child-b",
        title="worker:b",
        initial_message=marker_b,
    )

    items_a = json.dumps((await client.get(f"/v1/sessions/{child_a['id']}/items")).json())
    items_b = json.dumps((await client.get(f"/v1/sessions/{child_b['id']}/items")).json())
    items_parent = json.dumps((await client.get(f"/v1/sessions/{parent['id']}/items")).json())

    # Each child sees only its own seed. Cross-presence would mean the
    # seed routed to the wrong (or both) child sessions.
    assert marker_a in items_a and marker_b not in items_a
    assert marker_b in items_b and marker_a not in items_b
    # Neither seed leaks onto the parent transcript.
    assert marker_a not in items_parent
    assert marker_b not in items_parent


# ── Agents-surface tree scoping ──────────────────────────


async def test_child_sessions_lists_only_direct_children(
    client: httpx.AsyncClient,
) -> None:
    """``child_sessions`` returns direct children only, not grandchildren.

    The Agents surface enumerates one level of the spawn tree per
    session. A root → child → grandchild chain must show the child under
    the root and the grandchild under the child — never the grandchild
    flattened onto the root.
    """
    root = await _create_parent_session(client, agent_name="ctx-tree-root")
    child = await _create_child_session(
        client,
        parent_session_id=root["id"],
        agent_name="ctx-tree-child",
        title="worker:child",
    )
    grandchild = await _create_child_session(
        client,
        parent_session_id=child["id"],
        agent_name="ctx-tree-grandchild",
        title="worker:grandchild",
    )

    root_children = (await client.get(f"/v1/sessions/{root['id']}/child_sessions")).json()
    root_ids = {row["id"] for row in root_children["data"]}
    # The direct child is listed; the grandchild is one level deeper and
    # must not appear under the root. If it does, the listing query
    # widened from parent_conversation_id to a whole-tree scan.
    assert child["id"] in root_ids
    assert grandchild["id"] not in root_ids

    child_children = (await client.get(f"/v1/sessions/{child['id']}/child_sessions")).json()
    child_ids = {row["id"] for row in child_children["data"]}
    # The grandchild surfaces under its direct parent, confirming the
    # chain is intact rather than the grandchild being orphaned.
    assert grandchild["id"] in child_ids
    assert root["id"] not in child_ids


# ── Named sub-agent sends from a bundled agent (OMNI-1611) ────
#
# End-user journey: someone uploads an agent bundle, which creates a
# session-scoped agent plus the session it was minted for, and that agent
# then delegates work with named ``sys_session_send`` calls. Each named send
# is a ``POST /v1/sessions`` carrying the parent's ``agent_id``, its
# ``parent_session_id``, and the declared ``sub_agent_name``.
#
# The reported failure: the first named send succeeded and every later one
# returned ``404 {"code": "not_found", "message": "Conversation not found"}``.
#
# Two production conditions are required, so the tests below reproduce both
# rather than asserting on internals:
#
# 1. **Auth is on.** ``validate_session_agent`` authorizes the caller against
#    the agent's owning session. With no permission store that check returns
#    immediately, which is why this never reproduced on a default local server.
# 2. **The auth read lags.** Named children are bound to the *same* agent_id
#    as their mint, so the reverse lookup can pick a just-written child row;
#    on a deployment whose reads go to a replica, that row is not visible yet
#    and the lookup's caller reads it as "conversation gone" -> 404.
#
# ``_ReplicaLagConversationStore`` models condition 2 by hiding specific
# freshly-written rows from reads, the same way a replica that has not caught
# up does. Nothing about the failure is simulated: the request, the auth path
# and the 404 are all real.


_OWNER = "alice@example.com"

# A pinned, low-sorting id for the first named child. The pre-fix lookup was an
# unordered ``LIMIT 1`` served by ``ix_conversations_agent_id``
# (workspace_id, agent_id, id), so it effectively returned the lowest id among
# the rows sharing the agent_id. Pinning one child below the mint's random uuid
# makes the pre-fix path deterministically pick the child; left to chance it
# picks the mint about half the time, which is exactly why the production
# symptom looked intermittent.
_LAGGING_CHILD_ID = "00000000000000000000000000000001"


class _ReplicaLagConversationStore(SqlAlchemyConversationStore):
    """Conversation store whose reads model a lagging read replica.

    Ids added to :attr:`not_yet_replicated` read as missing, standing in for
    rows the primary has written but the replica has not received yet.
    """

    def __init__(self, uri: str) -> None:
        super().__init__(uri)
        self.not_yet_replicated: set[str] = set()

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Return the conversation unless it is still awaiting replication."""
        if conversation_id in self.not_yet_replicated:
            return None
        return super().get_conversation(conversation_id)


@pytest.fixture()
def replica_lag_store(db_uri: str) -> _ReplicaLagConversationStore:
    """:returns: A conversation store that can hide un-replicated rows."""
    return _ReplicaLagConversationStore(db_uri)


@pytest.fixture()
def replica_lag_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    replica_lag_store: _ReplicaLagConversationStore,
) -> FastAPI:
    """App with auth enabled and reads served by the lagging store.

    Auth must be on for the owning-session check in
    ``validate_session_agent`` to run at all.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=replica_lag_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def replica_lag_client(
    replica_lag_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """:returns: HTTP client wired to the auth-enabled, lagging-read app."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=replica_lag_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


async def _upload_bundled_agent_session(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    sub_agents: list[str],
) -> tuple[str, str]:
    """Upload a bundle, creating a session-scoped agent and its mint session.

    :param client: The test HTTP client.
    :param headers: Caller identity headers.
    :param sub_agents: Names the bundle declares, e.g. ``["worker_a"]``.
    :returns: ``(mint_session_id, agent_id)``.
    """
    bundle = build_agent_bundle(
        name="orchestrator",
        sub_agents=[{"name": name} for name in sub_agents],
    )
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=headers,
    )
    assert resp.status_code == 201, f"bundle upload failed: {resp.text}"
    mint_id = str(resp.json()["session_id"])
    agent_resp = await client.get(f"/v1/sessions/{mint_id}/agent", headers=headers)
    assert agent_resp.status_code == 200, agent_resp.text
    return mint_id, str(agent_resp.json()["id"])


async def _named_send(
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str],
    agent_id: str,
    parent_session_id: str,
    sub_agent_name: str,
    title: str,
) -> httpx.Response:
    """Issue the create a named ``sys_session_send`` performs.

    :returns: The raw response so the caller can assert on the status.
    """
    return await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_session_id,
            "sub_agent_name": sub_agent_name,
            "title": title,
        },
        headers=headers,
    )


async def test_later_named_sends_survive_unreplicated_sibling_rows(
    replica_lag_client: httpx.AsyncClient,
    replica_lag_store: _ReplicaLagConversationStore,
) -> None:
    """A bundled agent can keep delegating after its first sub-agent session.

    Reproduces OMNI-1611. Named children share the mint's ``agent_id``, so the
    agent's owning-session lookup could return a just-written child instead of
    the mint. While that child is still absent from the read replica, the
    owning-session authorization reads it as missing and the user's second
    delegation fails with ``404 Conversation not found`` -- even though they
    own every session involved.
    """
    headers = {"X-Forwarded-Email": _OWNER}
    mint_id, agent_id = await _upload_bundled_agent_session(
        replica_lag_client,
        headers=headers,
        sub_agents=["worker_a", "worker_b"],
    )

    # First delegation: this always worked, and still must.
    first = await _named_send(
        replica_lag_client,
        headers=headers,
        agent_id=agent_id,
        parent_session_id=mint_id,
        sub_agent_name="worker_a",
        title="worker_a:task-one",
    )
    assert first.status_code == 201, f"first named send failed: {first.text}"

    # A sibling sub-agent row bound to the same agent, pinned low so the
    # pre-fix lookup selects it over the mint (see _LAGGING_CHILD_ID).
    sibling = replica_lag_store.create_conversation(
        kind="sub_agent",
        title="worker_a:task-zero",
        parent_conversation_id=mint_id,
        agent_id=agent_id,
        sub_agent_name="worker_a",
        conversation_id=_LAGGING_CHILD_ID,
    )
    # Every child of the mint belongs to the same spawn tree, which is what
    # makes resolving the agent to that tree's root safe.
    assert sibling.root_conversation_id == mint_id

    # Those freshly-written child rows have not reached the replica yet. The
    # mint is older and already replicated.
    replica_lag_store.not_yet_replicated.update({sibling.id, str(first.json()["id"])})

    # Second delegation: the user-visible regression.
    second = await _named_send(
        replica_lag_client,
        headers=headers,
        agent_id=agent_id,
        parent_session_id=mint_id,
        sub_agent_name="worker_b",
        title="worker_b:task-two",
    )
    assert second.status_code == 201, (
        f"second named send from a bundled agent returned {second.status_code}: {second.text}"
    )
    body = second.json()
    assert body["parent_session_id"] == mint_id
    assert body["sub_agent_name"] == "worker_b"


async def test_bundled_agent_uploaded_as_child_stays_private(
    replica_lag_client: httpx.AsyncClient,
) -> None:
    """Someone else cannot run a bundled agent they have no access to.

    A bundle can be uploaded *into* an existing session, so the resulting
    session-scoped agent is minted on a conversation that already has a
    parent. ``validate_session_agent`` only enforces the owning-session check
    when it can resolve which session owns the agent; if that resolution comes
    back empty the check is skipped and anyone who learns the raw agent id can
    bind a session to it. This asserts the outsider is turned away, which is
    the user-facing shape of that gap.
    """
    owner_headers = {"X-Forwarded-Email": _OWNER}
    outsider_headers = {"X-Forwarded-Email": "mallory@example.com"}

    # The owner's existing session, then a bundle uploaded into it.
    parent_id, _ = await _upload_bundled_agent_session(
        replica_lag_client,
        headers=owner_headers,
        sub_agents=["worker_a"],
    )
    bundle = build_agent_bundle(
        name="nested-orchestrator",
        sub_agents=[{"name": "worker_a"}],
    )
    nested = await replica_lag_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": parent_id})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=owner_headers,
    )
    assert nested.status_code == 201, f"nested bundle upload failed: {nested.text}"
    nested_id = str(nested.json()["session_id"])
    agent_resp = await replica_lag_client.get(
        f"/v1/sessions/{nested_id}/agent", headers=owner_headers
    )
    assert agent_resp.status_code == 200, agent_resp.text
    nested_agent_id = str(agent_resp.json()["id"])

    # The owner can still delegate with it.
    owner_send = await _named_send(
        replica_lag_client,
        headers=owner_headers,
        agent_id=nested_agent_id,
        parent_session_id=nested_id,
        sub_agent_name="worker_a",
        title="worker_a:owner-task",
    )
    assert owner_send.status_code == 201, f"owner named send failed: {owner_send.text}"

    # Someone with no access to any of it must not be able to bind to the
    # agent by id. 404 rather than 403 so the agent's existence stays hidden.
    intruder = await replica_lag_client.post(
        "/v1/sessions",
        json={"agent_id": nested_agent_id},
        headers=outsider_headers,
    )
    assert intruder.status_code == 404, (
        "an outsider bound a session to a private bundled agent: "
        f"{intruder.status_code} {intruder.text}"
    )


# ── Replica routing: a child must route like its parent ──────────


_ROUTING_HOST_ID = "5c7f0d3a8b1e4f6a9c2d7e8f0a1b2c3d"
_ROUTING_RUNNER_ID = "runner_token_two_replica_routing"


@dataclass
class _TwoReplicaStack:
    """Two server replicas over one database.

    :param tunnel_replica: The replica holding the host and runner tunnels.
    :param tunnel_client: HTTP client bound to ``tunnel_replica``.
    :param keyless_replica: A replica that shares the database but holds no
        tunnels — where an unkeyed request lands.
    :param keyless_client: HTTP client bound to ``keyless_replica``.
    :param conv_store: Conversation store over the shared database.
    """

    tunnel_replica: FastAPI
    tunnel_client: httpx.AsyncClient
    keyless_replica: FastAPI
    keyless_client: httpx.AsyncClient
    conv_store: SqlAlchemyConversationStore


@pytest_asyncio.fixture()
async def two_replica_stack(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> AsyncIterator[_TwoReplicaStack]:
    """Model a host-sharded deployment with two replicas.

    A host's control tunnel and its runners' tunnels register on ONE replica
    (keyed by ``host_id``). Every replica shares the database, so a request
    that lands elsewhere sees the same rows but an empty tunnel registry —
    the routing miss the ``WRONG_REPLICA`` re-address exists for.

    Resource routes resolve the runner through the runtime-global router,
    which in production is per-process. Point it at the keyless replica whose
    resource routes this suite exercises; the tunnel replica's router is
    consulted directly through its app state.
    """
    host_store = HostStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))

    def _replica(name: str) -> FastAPI:
        return create_app(
            agent_store=SqlAlchemyAgentStore(db_uri),
            file_store=SqlAlchemyFileStore(db_uri),
            conversation_store=SqlAlchemyConversationStore(db_uri),
            artifact_store=artifact_store,
            agent_cache=AgentCache(
                artifact_store=artifact_store,
                cache_dir=tmp_path / name / "cache",
            ),
            comment_store=SqlAlchemyCommentStore(db_uri),
            host_store=host_store,
        )

    tunnel_replica = _replica("tunnel-replica")
    keyless_replica = _replica("keyless-replica")
    set_runner_router(keyless_replica.state.runner_router)
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=tunnel_replica),
                base_url="http://tunnel-replica",
            ) as tunnel_client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=keyless_replica),
                base_url="http://keyless-replica",
            ) as keyless_client,
        ):
            yield _TwoReplicaStack(
                tunnel_replica=tunnel_replica,
                tunnel_client=tunnel_client,
                keyless_replica=keyless_replica,
                keyless_client=keyless_client,
                conv_store=SqlAlchemyConversationStore(db_uri),
            )
    finally:
        with contextlib.suppress(Exception):
            await tunnel_replica.state.runner_router.aclose()
        with contextlib.suppress(Exception):
            await keyless_replica.state.runner_router.aclose()
        set_runner_router(None)


def _status_and_error_code(resp: httpx.Response) -> tuple[int, str]:
    """Reduce a response to ``(status, error code)`` for routing assertions."""
    body = resp.json()
    return resp.status_code, str(body.get("error", {}).get("code"))


async def test_codex_subagent_child_routes_like_its_parent_across_replicas(
    two_replica_stack: _TwoReplicaStack,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """A hostless codex sub-agent child must classify a routing miss as its parent does.

    Reproduces the managed-deployment report where every Codex sub-agent
    child opened from the web said "runner offline" while the parent kept
    working: the child copies the parent's ``runner_id`` but no ``host_id``,
    so a request that lands on a replica without the tunnel cannot tell
    "wrong replica" (re-address) from "runner gone" (503). The parent, being
    host-bound, gets the re-addressable ``wrong_replica``; the child must too,
    for resource reads, a message send, and a retry — and the misrouted send
    must not be recorded as a failed turn.
    """
    stack = two_replica_stack
    conv_store = stack.conv_store

    # A codex-native parent bound to a host and its runner — the row a
    # ``omnigent codex`` session on a devbox produces.
    agent = await create_test_agent(stack.tunnel_client, name="codex-native-parent")
    parent_id = agent["_session_id"]
    conv_store.set_host_id(parent_id, _ROUTING_HOST_ID, workspace=str(tmp_path / "ws"))
    conv_store.replace_runner_id(parent_id, _ROUTING_RUNNER_ID)
    conv_store.set_labels(
        parent_id, {_CLAUDE_NATIVE_WRAPPER_LABEL_KEY: _CODEX_NATIVE_WRAPPER_LABEL_VALUE}
    )

    # The host and the runner tunnel register on the tunnel replica only.
    HostStore(db_uri).upsert_on_connect(_ROUTING_HOST_ID, "devbox", RESERVED_USER_LOCAL)
    stack.tunnel_replica.state.host_registry.register(
        _ROUTING_HOST_ID,
        AsyncMock(),
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name="devbox",
            runners=[_ROUTING_RUNNER_ID],
        ),
        owner=RESERVED_USER_LOCAL,
    )
    communicator = await _connect_runner_tunnel(stack.tunnel_replica, _ROUTING_RUNNER_ID)
    try:
        await _send_runner_hello_and_wait(
            communicator,
            stack.tunnel_replica,
            _ROUTING_RUNNER_ID,
            harnesses=["codex-native"],
        )

        # Codex spawns a sub-agent thread; the codex-native forwarder registers
        # it with exactly this event.
        start = await stack.tunnel_client.post(
            f"/v1/sessions/{parent_id}/events",
            json={
                "type": "external_codex_subagent_start",
                "data": {
                    "thread_id": "thr_worker_1",
                    "agent_nickname": "Codex",
                    "agent_role": "worker",
                    "prompt": "Investigate the flaky test",
                },
            },
        )
        assert start.status_code == 202, f"codex sub-agent start failed: {start.text}"
        child_id = start.json()["child_session_id"]
        child = conv_store.get_conversation(child_id)
        assert child is not None
        assert child.kind == "sub_agent"
        assert child.runner_id == _ROUTING_RUNNER_ID, "child must share the parent's runner"

        # The runner is online: the replica holding its tunnel resolves the
        # child's runner client. Whatever the other replica says next is a
        # routing artifact, not an outage.
        routed = stack.tunnel_replica.state.runner_router.client_for_session_resources(child_id)
        assert routed.runner_id == _ROUTING_RUNNER_ID

        # An unkeyed request lands on the other replica. The host-bound
        # parent is classified as re-addressable ...
        parent_terminals = await stack.keyless_client.get(
            f"/v1/sessions/{parent_id}/resources/terminals"
        )
        assert _status_and_error_code(parent_terminals) == (400, "wrong_replica"), (
            f"parent baseline drifted: {parent_terminals.status_code} {parent_terminals.text}"
        )

        # ... and its child must be classified exactly the same way: it runs
        # on the parent's runner, on the parent's host's replica.
        child_terminals = await stack.keyless_client.get(
            f"/v1/sessions/{child_id}/resources/terminals"
        )
        assert _status_and_error_code(child_terminals) == (400, "wrong_replica"), (
            "a hostless sub-agent child on the wrong replica must re-address like its "
            f"parent, not report the runner offline: {child_terminals.status_code} "
            f"{child_terminals.text}"
        )

        send = await stack.keyless_client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "status?"}],
                },
            },
        )
        assert _status_and_error_code(send) == (400, "wrong_replica"), (
            "a message to a hostless sub-agent child on the wrong replica must "
            f"re-address, not fail the turn: {send.status_code} {send.text}"
        )

        retry = await stack.keyless_client.post(
            f"/v1/sessions/{child_id}/events",
            json={"type": "retry_session", "data": {}},
        )
        assert _status_and_error_code(retry) == (400, "wrong_replica"), (
            f"retry on the wrong replica must re-address: {retry.status_code} {retry.text}"
        )

        # The misrouted send left no failed turn behind on the child.
        snapshot = await stack.keyless_client.get(f"/v1/sessions/{child_id}")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["status"] != "failed", (
            "a misrouted send must not be recorded as a failed turn on the child"
        )
        items = await stack.keyless_client.get(f"/v1/sessions/{child_id}/items")
        assert items.status_code == 200, items.text
        assert items.json()["data"] == [], (
            f"a misrouted send must not persist items on the child: {items.json()['data']!r}"
        )
    finally:
        with contextlib.suppress(Exception):
            await communicator.send_input({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(Exception):
            await communicator.wait(timeout=budget(2.0))
