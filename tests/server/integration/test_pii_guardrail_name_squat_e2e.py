"""E2E: a same-named policy created on a sub-agent must not neutralize an
enforced PII guardrail inherited from its parent.

An operator enforces a PII guardrail on a top-level (root) session by
attaching the built-in ``deny_pii_in_llm_request`` policy through the
session-policy create endpoint (the same path ``sys_add_policy`` and the
Policies UI drive). Sub-agents spawned under that session inherit the root's
session policies by design, so the guardrail governs the whole tree: a PII
request evaluated on a child session is DENIED.

The bug: the session-policy **create** endpoint has no protected-name
enforcement. A tenant with edit access to a spawned sub-agent (child) session
can ``POST /v1/sessions/{child}/policies`` a *benign* policy whose ``name``
collides with the inherited guardrail (here ``max_tool_calls_per_session``
reusing the name ``pii_guard``). The engine's root->child inheritance
deduplicates by name with "child wins" semantics
(``omnigent/runtime/policies/builder.py``: ``root_policy_specs = [p for p in
root_policy_specs if p.name not in child_names]``), so the child's benign
policy *drops* the inherited guardrail. The create endpoint should reject the
name collision; because it does not, PII now flows through the child
unblocked.

Journey (per the create path):

1. create a root session and attach the enforced ``deny_pii_in_llm_request``
   guardrail as a session policy (``name="pii_guard"``);
2. spawn a sub-agent child session under it -> evaluating a PII request on the
   child is DENIED (the guardrail is inherited) -- the control leg;
3. create a same-named benign session policy on the child via the create
   endpoint (the squat the endpoint fails to reject);
4. evaluating the same PII request on the child is now ALLOWED -- the enforced
   guardrail has been neutralized.

The reproduction assertion is the security invariant: after the squat, PII
must still be DENIED on the child. On the buggy build it is ALLOWED. The fix
(protected-name enforcement on create) keeps the guardrail in force, whether
by rejecting the squatting create or by never dropping a protected name in the
inheritance merge -- either way the child verdict returns to DENY and this
test passes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.policies.types import EvaluationContext
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies import build_policy_engine
from omnigent.runtime.policies import builder as policy_builder
from omnigent.server.app import create_app
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, Phase, PolicyAction
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from tests.server.conftest import ControllableMockClient

pytestmark = pytest.mark.asyncio

# A request that trips the built-in PII patterns (email + US SSN).
_PII_REQUEST = "please email me at victim@example.com and my ssn is 123-45-6789"
# The enforced guardrail: DENY any LLM request / user message carrying PII.
_PII_GUARD_HANDLER = "omnigent.policies.builtins.safety.deny_pii_in_llm_request"
# A benign, registry-allowlisted policy used to squat the guardrail's name.
# A very high limit means it never fires, so it silently replaces the guard.
_BENIGN_HANDLER = "omnigent.policies.builtins.safety.max_tool_calls_per_session"
# The shared name: the child squats it to shadow the inherited guardrail.
_GUARD_NAME = "pii_guard"


@pytest.fixture()
def policy_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """App with the session-policy CRUD endpoints wired (no auth).

    ASGITransport skips lifespan, which normally loads the policy registry;
    load it here so the create endpoint's handler allowlist is populated.

    :param runtime_init: Runtime + mock-LLM initialization fixture.
    :param db_uri: Per-test SQLite URI.
    :param tmp_path: Pytest temp dir for artifacts / cache.
    :param monkeypatch: Isolate the registry from other tests.
    :returns: A :class:`FastAPI` app exposing the session-policy routes.
    """
    from omnigent.policies import registry as policy_registry

    monkeypatch.setattr(policy_registry, "_registry", [])
    monkeypatch.setattr(policy_registry, "_registry_by_handler", {})
    policy_registry.load_registry()

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )


@pytest_asyncio.fixture()
async def policy_client(
    policy_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to :func:`policy_app` (in-process, no server).

    :param policy_app: FastAPI app with policy CRUD routes.
    :param mock_llm: Controllable mock LLM -- released on teardown.
    :param tmp_path: Pytest temp dir for the harness process manager.
    :yields: A ready-to-use :class:`httpx.AsyncClient`.
    """
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=policy_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


def _minimal_spec(tmp_path: Path) -> AgentSpec:
    """Parse a minimal guardrail-free agent spec.

    The enforced guardrail is a *session* policy (persisted via the create
    endpoint), not a bundle guardrail, so the spec itself declares none.

    :param tmp_path: Directory to write the throwaway ``config.yaml`` into.
    :returns: The parsed :class:`AgentSpec`.
    """
    (tmp_path / "config.yaml").write_text(
        "spec_version: 1\nname: pii-guard-agent\nprompt: test agent\n"
    )
    return parse(tmp_path)


async def _create_session_policy(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    name: str,
    handler: str,
    factory_params: dict[str, object] | None = None,
) -> httpx.Response:
    """POST a session policy through the real create endpoint.

    :param client: The in-process HTTP client.
    :param session_id: Owning session id.
    :param name: Policy name.
    :param handler: Registry-allowlisted policy handler path.
    :param factory_params: Optional factory parameters.
    :returns: The raw HTTP response (status not asserted here).
    """
    body: dict[str, object] = {"name": name, "type": "python", "handler": handler}
    if factory_params is not None:
        body["factory_params"] = factory_params
    return await client.post(
        f"/v1/sessions/{session_id}/policies",
        json=body,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )


async def _pii_verdict(
    spec: AgentSpec,
    session_id: str,
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
) -> PolicyAction:
    """Build the engine for *session_id* and evaluate a PII request.

    Builds through the same ``build_policy_engine`` the evaluate endpoint
    calls -- so the root->child session-policy inheritance runs for real.
    Caches are cleared first so each build reflects the latest stored
    policies (the create endpoint also invalidates them on write).

    :param spec: The agent spec bound to the engine.
    :param session_id: Session to evaluate against.
    :param conversation_store: Store the engine reads the session tree from.
    :param db_uri: URI for a fresh policy store read.
    :returns: The engine's :class:`PolicyAction` for the PII request.
    """
    policy_builder._SESSION_POLICY_SPECS_CACHE.clear()
    policy_builder._DEFAULT_POLICY_SPECS_CACHE.clear()
    engine = build_policy_engine(
        spec=spec,
        conversation_id=session_id,
        conversation_store=conversation_store,
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )
    result = await engine.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content=_PII_REQUEST, tool_name=None)
    )
    return result.action


@pytest.mark.timeout(120)
async def test_child_name_squat_cannot_neutralize_inherited_pii_guardrail(
    policy_client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
) -> None:
    """A same-named child policy must not disable an inherited PII guardrail.

    Controls prove the guardrail is real and inherited: it DENIES PII on the
    root, and DENIES PII on the freshly spawned sub-agent child (inheritance).
    Then a benign same-named policy is created on the child through the create
    endpoint. The security invariant -- PII still DENIED on the child -- must
    hold. On the buggy build the child verdict flips to ALLOW: the enforced
    guardrail has been neutralized by name-squatting a policy the create
    endpoint failed to reject.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    root = conv_store.create_conversation(kind="default")
    child = conv_store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)
    # Precondition: the child is a sub-agent of the root, so it inherits the
    # root's session policies (child.root == root, child.id != root.id).
    assert child.root_conversation_id == root.id
    assert child.id != root.id

    spec = _minimal_spec(tmp_path)

    # The operator enforces the PII guardrail on the root session.
    create_guard = await _create_session_policy(
        policy_client,
        root.id,
        name=_GUARD_NAME,
        handler=_PII_GUARD_HANDLER,
    )
    assert create_guard.status_code == 200, create_guard.text

    # Control 1: the guardrail fires on the root itself.
    assert await _pii_verdict(spec, root.id, conv_store, db_uri) == PolicyAction.DENY, (
        "PII guardrail did not DENY on the session it was attached to"
    )

    # Control 2: the sub-agent child inherits the guardrail, so PII is DENIED
    # there too -- before any squat. This is the state the squat must not undo.
    assert await _pii_verdict(spec, child.id, conv_store, db_uri) == PolicyAction.DENY, (
        "sub-agent did not inherit the parent's PII guardrail"
    )

    # The squat: a benign policy reusing the guardrail's name is created on the
    # child via the create endpoint. A build with protected-name enforcement
    # rejects this (4xx); the buggy build accepts it (200). Either outcome is
    # tolerated here -- the security invariant below is what must hold.
    squat = await _create_session_policy(
        policy_client,
        child.id,
        name=_GUARD_NAME,
        handler=_BENIGN_HANDLER,
        factory_params={"limit": 100000},
    )
    assert squat.status_code in (200, 201, 400, 403, 409), squat.text

    # The bug: after the squat, the inherited PII guardrail must still DENY the
    # child's PII request. On the buggy build it returns ALLOW -- the guardrail
    # has been silently neutralized by the name collision.
    verdict = await _pii_verdict(spec, child.id, conv_store, db_uri)
    assert verdict == PolicyAction.DENY, (
        f"child PII request was {verdict.name}, expected DENY: a same-named "
        f"benign policy created on the sub-agent (squat create status "
        f"{squat.status_code}) neutralized the inherited PII guardrail"
    )
