"""Cross-workspace default-policy leak.

Reproduces the enterprise multi-tenant symptom: one workspace's *default*
(server-wide) policy is enforced on another workspace's sessions for up to
30 s, while the enforcing policy is not listable in the affected workspace.

The enterprise integration binds ``workspace_scope()`` only around store
calls (the ``dual_store_router`` seam); request middleware records the
tenant in its own context, not omnigent's. The enforcement path in
``omnigent/runtime/policies/builder.py`` keys its TTL caches by
``current_workspace_id()``, which is therefore always 0 there — one global
cache slot shared by every tenant. This test emulates exactly that
contract around the real app:

1. A tenant-header ASGI wrapper records the request's workspace in its
   OWN ContextVar (never touching ``workspace_scope``).
2. The policy store is wrapped so each store CALL runs inside
   ``workspace_scope(<request workspace>)`` — mirroring the enterprise
   store router — while the enforcement path stays unscoped.

Journey (the leak this guards against):

1. Workspace A's admin creates a default DENY policy.
2. A turn runs in a workspace A session — denied by A's own policy
   (correct), priming the shared cache slot.
3. Within the 30 s TTL window, a user in workspace B sends a message in
   their own session — it must NOT be denied by A's policy, yet it is.
4. Workspace B's policy list stays empty throughout (CRUD reads are
   correctly scoped), so the enforcing policy is invisible to B.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.db_models import workspace_scope
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies import builder as policy_builder
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.policy_store import PolicyStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

WS_A_HEADERS = {"X-Workspace-Id": "101"}
WS_B_HEADERS = {"X-Workspace-Id": "202"}
DENY_SENTINEL = "WS-A-DEFAULT-POLICY-SENTINEL"

# The emulated enterprise middleware's OWN request-tenant ContextVar.
# Deliberately distinct from omnigent's ``workspace_scope`` var: in the
# enterprise integration nothing binds ``workspace_scope`` around the
# enforcement path — only store calls are routed through it.
_REQ_WS: contextvars.ContextVar[int] = contextvars.ContextVar("emulated_tenant_ws", default=0)


class TenantHeaderASGI:
    """Emulates enterprise middleware: records the tenant in its own var."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        ws = 0
        if scope["type"] == "http":
            for key, value in scope.get("headers", []):
                if key == b"x-workspace-id":
                    ws = int(value)
        token = _REQ_WS.set(ws)
        try:
            await self.app(scope, receive, send)
        finally:
            _REQ_WS.reset(token)


class WorkspaceRoutedStore:
    """Emulates the enterprise store router: each store CALL is scoped.

    Every method call runs inside ``workspace_scope(<request workspace>)``,
    so CRUD reads/writes are correctly tenant-scoped — while callers
    outside a store call (the policy-builder cache keys) still observe
    ``current_workspace_id() == 0``.
    """

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        if inspect.iscoroutinefunction(attr):

            @functools.wraps(attr)
            async def routed_async(*args: Any, **kwargs: Any) -> Any:
                with workspace_scope(_REQ_WS.get()):
                    return await attr(*args, **kwargs)

            return routed_async

        @functools.wraps(attr)
        def routed(*args: Any, **kwargs: Any) -> Any:
            with workspace_scope(_REQ_WS.get()):
                return attr(*args, **kwargs)

        return routed


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture()
def _cold_policy_caches() -> Iterator[None]:
    """Start and finish with cold policy-builder caches.

    The buggy cache slot is module-global (key 0 shared by every tenant),
    so clear it on both sides to keep other tests in the worker isolated.
    """
    policy_builder._DEFAULT_POLICY_SPECS_CACHE.clear()
    policy_builder._SESSION_POLICY_SPECS_CACHE.clear()
    yield
    policy_builder._DEFAULT_POLICY_SPECS_CACHE.clear()
    policy_builder._SESSION_POLICY_SPECS_CACHE.clear()


@pytest.fixture()
def tenant_policy_app(
    runtime_init: None,
    _cold_policy_caches: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """App with a workspace-routed ``policy_store`` (enterprise contract).

    Also patches the runtime-global ``_policy_store`` so the enforcement
    path (``get_policy_store()`` inside the policy builder) reads through
    the same workspace-routed wrapper, and widens the default-policy CRUD
    allowlist to accept the fixed-action test factory.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    policy_store = cast(PolicyStore, WorkspaceRoutedStore(SqlAlchemyPolicyStore(db_uri)))

    monkeypatch.setattr("omnigent.runtime._globals._policy_store", policy_store)

    from omnigent.server.routes import default_policies as _dp_mod

    _original_is_registered = _dp_mod.is_registered_handler
    monkeypatch.setattr(
        _dp_mod,
        "is_registered_handler",
        lambda handler: (
            handler == "omnigent.policies.function.make_fixed_action_callable"
            or _original_is_registered(handler)
        ),
    )

    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        policy_store=policy_store,
        comment_store=SqlAlchemyCommentStore(db_uri),
    )


@pytest_asyncio.fixture()
async def tenant_client(
    tenant_policy_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async client whose requests carry an emulated tenant context."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=cast(Any, TenantHeaderASGI(tenant_policy_app)))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── Helpers ─────────────────────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient, agent_id: str, headers: dict[str, str]
) -> str:
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id}, headers=headers)
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _send_user_message(
    client: httpx.AsyncClient,
    session_id: str,
    text: str,
    headers: dict[str, str],
) -> httpx.Response:
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        headers=headers,
    )


async def _create_ws_a_default_deny_policy(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/v1/policies",
        json={
            "name": "ws_a_default_guard",
            "type": "python",
            "handler": "omnigent.policies.function.make_fixed_action_callable",
            "factory_params": {"action": "deny", "reason": DENY_SENTINEL},
        },
        headers=WS_A_HEADERS,
    )
    assert resp.status_code in {200, 201}, (
        f"workspace A default-policy create failed: {resp.status_code} {resp.text}"
    )


# ── Tests ───────────────────────────────────────────────────────────────


async def test_ws_b_turn_not_denied_by_ws_a_default_policy(
    tenant_client: httpx.AsyncClient,
    mock_llm: ControllableMockClient,
) -> None:
    """Workspace B's turn must not be denied by workspace A's default policy.

    1. Workspace A's admin creates a default DENY policy.
    2. A workspace A turn is denied by it (correct — and it primes the
       policy-builder default-spec cache slot).
    3. Within the 30 s TTL window, a workspace B turn runs. It must pass
       the policy layer: 202-not-denied (queued) or 503 (no runner) are
       both fine; a synchronous DENY carrying A's sentinel reason is the
       cross-workspace leak.
    """
    await _create_ws_a_default_deny_policy(tenant_client)

    agent = await create_test_agent(tenant_client)
    sess_a = await _create_session(tenant_client, agent["id"], WS_A_HEADERS)
    sess_b = await _create_session(tenant_client, agent["id"], WS_B_HEADERS)

    # Workspace A's own turn: denied by A's own default policy (correct),
    # and primes the module-global default-policy cache.
    resp_a = await _send_user_message(tenant_client, sess_a, "hello from A", WS_A_HEADERS)
    assert resp_a.status_code == 202, (
        f"expected 202 verdict for A's turn; got {resp_a.status_code} {resp_a.text}"
    )
    verdict_a = resp_a.json()
    assert verdict_a.get("denied") is True and DENY_SENTINEL in verdict_a.get("reason", ""), (
        f"precondition: A's own turn should be denied by A's default policy; got {verdict_a}"
    )

    # Workspace B's turn, inside the 30 s TTL window: must NOT be denied.
    resp_b = await _send_user_message(tenant_client, sess_b, "hello from B", WS_B_HEADERS)
    assert resp_b.status_code in {202, 503}, (
        f"expected 202 or 503 for B's turn; got {resp_b.status_code} {resp_b.text}"
    )
    body_b = resp_b.json()
    assert body_b.get("denied") is not True, (
        "cross-workspace policy leak: workspace B's turn was denied by workspace A's "
        f"default policy; verdict={body_b}; default-policy cache keys="
        f"{list(policy_builder._DEFAULT_POLICY_SPECS_CACHE.keys())}"
    )


async def test_ws_b_policy_list_never_shows_ws_a_default_policy(
    tenant_client: httpx.AsyncClient,
    mock_llm: ControllableMockClient,
) -> None:
    """Workspace B's Policies list must stay empty (the 'not listable' facet).

    The enforcing policy row is stamped with workspace A, so B's
    Settings > Policies (GET /v1/policies) shows nothing — the compound
    symptom is B being denied by a policy it cannot see. CRUD scoping is
    correct today and must stay correct after the enforcement fix.
    """
    await _create_ws_a_default_deny_policy(tenant_client)

    list_a = await tenant_client.get("/v1/policies", headers=WS_A_HEADERS)
    assert list_a.status_code == 200, f"A list failed: {list_a.status_code} {list_a.text}"
    names_a = [p.get("name") for p in list_a.json().get("data", [])]
    assert "ws_a_default_guard" in names_a, (
        f"precondition: A must see its own default policy; got {names_a}"
    )

    list_b = await tenant_client.get("/v1/policies", headers=WS_B_HEADERS)
    assert list_b.status_code == 200, f"B list failed: {list_b.status_code} {list_b.text}"
    leaked = [p for p in list_b.json().get("data", []) if p.get("name") == "ws_a_default_guard"]
    assert leaked == [], f"workspace A's default policy is visible in workspace B's list: {leaked}"
