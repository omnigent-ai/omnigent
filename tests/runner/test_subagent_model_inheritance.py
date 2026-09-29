"""Dispatch-gate tests for parent-model inheritance on sub-agent creation.

A ``sys_session_send`` that names no ``args.model`` must not silently land
the child on the worker/provider default when the user selected a model for
the parent session: the gate reads the parent's effective model
(``model_override`` falling back to ``llm_model``) and persists it as the
child's ``model_override``. Inheritance is best-effort and skips quietly
when the sub-agent spec pins its own model, the child harness has no
override plumbing, the parent model's family cannot run on the child
harness, the child is a multi-model harness (pi, opencode, …) unlike the
parent's harness (and no inference binding validates the id), or the
parent snapshot is unavailable.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest


def _spec_with_worker(
    harness: str,
    *,
    worker_model: str | None = None,
) -> SimpleNamespace:
    """
    Build a parent-spec stub declaring one ``worker`` sub-agent.

    :param harness: The sub-agent's declared harness, e.g. ``"claude-sdk"``.
    :param worker_model: Optional ``executor.model`` pin on the worker spec.
    :returns: A structural parent-spec stub for ``execute_tool``.
    """
    executor = SimpleNamespace(type="omnigent", config={"harness": harness})
    if worker_model is not None:
        executor.model = worker_model
    return SimpleNamespace(sub_agents=[SimpleNamespace(name="worker", executor=executor)])


def _stub_worker_launchable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a native-harness dispatch pass preflight in a CLI-less test env.

    The dispatch rejects a worker whose harness CLI is missing; stub that probe
    present so the inheritance gate — not the binary — is what the test drives.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.onboarding import harness_install

    monkeypatch.setattr(harness_install, "missing_harness_cli", lambda _harness: None)


async def _dispatch_without_model(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_spec: Any,
    conv_id: str,
    parent_snapshot: dict[str, Any] | None,
    explicit_model: str | None = None,
) -> list[dict[str, Any]]:
    """
    Drive one fresh-create ``sys_session_send`` and capture create bodies.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param agent_spec: The parent spec under test.
    :param conv_id: Unique parent conversation id per test.
    :param parent_snapshot: JSON the mock server returns for
        ``GET /v1/sessions/{conv_id}``; ``None`` serves a 404.
    :param explicit_model: Optional ``args.model`` for the dispatch.
    :returns: The captured ``POST /v1/sessions`` bodies.
    """
    from omnigent.runner import app as runner_app
    from omnigent.runner.tool_dispatch import execute_tool

    create_bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _sid: "ag_parent")
    monkeypatch.setattr(runner_app, "register_child_session", lambda *a, **k: None)
    session_inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def _server_handler(request: httpx.Request) -> httpx.Response:
        """Serve the parent snapshot, child lookup, create, and message POSTs."""
        if request.method == "GET" and request.url.path == f"/v1/sessions/{conv_id}":
            if parent_snapshot is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=parent_snapshot)
        if (
            request.method == "GET"
            and request.url.path == f"/v1/sessions/{conv_id}/child_sessions"
        ):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            create_bodies.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "conv_child_inherit"})
        if (
            request.method == "POST"
            and request.url.path == "/v1/sessions/conv_child_inherit/events"
        ):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404, json={"error": str(request.url)})

    args: dict[str, Any] = {"input": "do the task"}
    if explicit_model is not None:
        args["model"] = explicit_model
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_server_handler),
        base_url="http://server",
    ) as server_client:
        try:
            output = await execute_tool(
                tool_name="sys_session_send",
                arguments=json.dumps({"agent": "worker", "title": "task", "args": args}),
                server_client=server_client,
                conversation_id=conv_id,
                agent_spec=agent_spec,
                session_inbox=session_inbox,
            )
        finally:
            runner_app.unregister_subagent_work("conv_child_inherit")
            runner_app._session_inboxes_ref.pop(conv_id, None)
    payload = json.loads(output)
    assert payload["status"] == "launching", output
    assert len(create_bodies) == 1, "fresh named send must create exactly one child"
    return create_bodies


@pytest.mark.asyncio
async def test_child_inherits_parent_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A no-model dispatch persists the parent's ``model_override`` on the child.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_parent_inherit_override",
        parent_snapshot={
            "id": "conv_parent_inherit_override",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": "databricks-claude-sonnet-4-6",
            "llm_model": "databricks-claude-opus-4-8",
        },
    )
    assert bodies[0]["model_override"] == "databricks-claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_child_inherits_parent_llm_model_when_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without a per-session override, the parent's effective ``llm_model``
    (e.g. the CLI ``--model`` selection baked into the spec) is inherited.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_parent_inherit_llm",
        parent_snapshot={
            "id": "conv_parent_inherit_llm",
            "agent_id": "ag_parent",
            "harness": "claude-sdk",
            "model_override": None,
            "llm_model": "databricks-claude-sonnet-4-6",
        },
    )
    assert bodies[0]["model_override"] == "databricks-claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_explicit_dispatch_model_wins_over_parent_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An explicit ``args.model`` is persisted verbatim; the parent's own
    selection never overrides the caller's request.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_parent_explicit_wins",
        parent_snapshot={
            "id": "conv_parent_explicit_wins",
            "agent_id": "ag_parent",
            "model_override": "databricks-claude-sonnet-4-6",
            "llm_model": None,
        },
        explicit_model="databricks-claude-haiku-4-5",
    )
    assert bodies[0]["model_override"] == "databricks-claude-haiku-4-5"


@pytest.mark.asyncio
async def test_worker_spec_model_pin_blocks_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A worker whose spec pins ``executor.model`` keeps its own model: the
    create body carries no ``model_override`` and the pinned spec model
    resolves at child boot.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk", worker_model="databricks-claude-haiku-4-5"),
        conv_id="conv_parent_spec_pin",
        parent_snapshot={
            "id": "conv_parent_spec_pin",
            "agent_id": "ag_parent",
            "model_override": "databricks-claude-sonnet-4-6",
            "llm_model": None,
        },
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_family_mismatch_blocks_inheritance_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A Claude parent selection is not forced onto a codex-family worker: the
    dispatch still succeeds, with no ``model_override`` on the child.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("codex"),
        conv_id="conv_parent_family_mismatch",
        parent_snapshot={
            "id": "conv_parent_family_mismatch",
            "agent_id": "ag_parent",
            "model_override": "databricks-claude-sonnet-4-6",
            "llm_model": None,
        },
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_unplumbed_harness_blocks_inheritance_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child harness without model-override plumbing skips inheritance
    (a persisted value would be silently ignored) without failing the
    dispatch — unlike an explicit ``args.model``, which fails loud.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("unknown-harness"),
        conv_id="conv_parent_unplumbed_inherit",
        parent_snapshot={
            "id": "conv_parent_unplumbed_inherit",
            "agent_id": "ag_parent",
            "model_override": "databricks-claude-sonnet-4-6",
            "llm_model": None,
        },
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_no_parent_model_leaves_child_on_worker_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A parent with no model selection dispatches a child with no override —
    the worker resolves its own default, exactly as before.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_parent_no_model",
        parent_snapshot={
            "id": "conv_parent_no_model",
            "agent_id": "ag_parent",
            "model_override": None,
            "llm_model": None,
        },
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_unreachable_parent_snapshot_skips_inheritance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed parent-snapshot read degrades to no inheritance — the dispatch
    itself must not fail on the best-effort lookup.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_parent_snapshot_404",
        parent_snapshot=None,
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_child_of_different_parent_harness_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A child on a different harness vendor (opencode) than the parent (claude)
    runs its own default: opencode resolves the parent's Claude id against its
    own provider, where it need not be servable. The child is created with no
    ``model_override``.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _stub_worker_launchable(monkeypatch)
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("opencode-native"),
        conv_id="conv_parent_opencode_foreign",
        parent_snapshot={
            "id": "conv_parent_opencode_foreign",
            "agent_id": "ag_parent",
            "harness": "claude-native",
            "model_override": "claude-opus-5",
            "llm_model": None,
        },
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_multi_model_child_of_different_multi_parent_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A multi-model child (pi) dispatched from a *different* multi-model parent
    (opencode) still runs its own default — "accepts any id" does not mean the
    parent's id is servable on pi's configured provider.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _stub_worker_launchable(monkeypatch)
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("pi"),
        conv_id="conv_parent_pi_foreign",
        parent_snapshot={
            "id": "conv_parent_pi_foreign",
            "agent_id": "ag_parent",
            "harness": "opencode-native",
            "model_override": "anthropic/claude-sonnet-4-5",
            "llm_model": None,
        },
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_multi_model_child_same_harness_as_parent_inherits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A multi-model child inherits when it is the *same* harness as the parent:
    a pi parent's selection shares pi's provider vocabulary, so the pi child
    keeps it.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _stub_worker_launchable(monkeypatch)
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("pi"),
        conv_id="conv_parent_pi_same",
        parent_snapshot={
            "id": "conv_parent_pi_same",
            "agent_id": "ag_parent",
            "harness": "pi",
            "model_override": "databricks-claude-opus-5",
            "llm_model": None,
        },
    )
    assert bodies[0]["model_override"] == "databricks-claude-opus-5"


@pytest.mark.asyncio
async def test_multi_model_child_native_variant_of_parent_inherits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Same vendor across the native/SDK split still inherits: a ``pi-native``
    parent and a ``pi`` child share Pi's provider vocabulary, so the ``-native``
    distinction must not make them count as different harnesses.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _stub_worker_launchable(monkeypatch)
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("pi"),
        conv_id="conv_parent_pi_native",
        parent_snapshot={
            "id": "conv_parent_pi_native",
            "agent_id": "ag_parent",
            "harness": "pi-native",
            "model_override": "databricks-claude-opus-5",
            "llm_model": None,
        },
    )
    assert bodies[0]["model_override"] == "databricks-claude-opus-5"


@pytest.mark.asyncio
async def test_child_shares_parent_vendor_across_sdk_native_split_inherits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``claude-sdk`` child of a ``claude-native`` parent inherits: the SDK and
    native spellings of one vendor share a provider vocabulary, so the ``-native``
    vs ``-sdk`` difference must not count them as foreign harnesses.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("claude-sdk"),
        conv_id="conv_parent_claude_variants",
        parent_snapshot={
            "id": "conv_parent_claude_variants",
            "agent_id": "ag_parent",
            "harness": "claude-native",
            "model_override": "databricks-claude-opus-5",
            "llm_model": None,
        },
    )
    assert bodies[0]["model_override"] == "databricks-claude-opus-5"


@pytest.mark.asyncio
async def test_opencode_worker_with_binding_inherits_bare_bound_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binding-validated bare id is inherited without a prefix or profile.

    An inference binding owns the harness's model namespace — launch resolves
    ids through ``resolve_bound_model`` — so the prefix/profile restriction
    does not apply. The inherited model deliberately differs from the
    binding's default: inheritance must carry the parent's selection rather
    than fall back to the worker default.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _stub_worker_launchable(monkeypatch)
    config: dict[str, Any] = {
        "providers": {"gw": {}},
        "inference": {
            "harnesses": {
                "opencode-native": {
                    "provider": "gw",
                    "default_model": "claude-sonnet-4-6",
                    "model_allowlist": ["claude-sonnet-4-6", "claude-opus-4-8"],
                }
            }
        },
    }
    monkeypatch.setattr(
        "omnigent.inference_config.load_runtime_inference_config",
        lambda base_config=None: config,
    )
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("opencode-native"),
        conv_id="conv_parent_opencode_binding",
        parent_snapshot={
            "id": "conv_parent_opencode_binding",
            "agent_id": "ag_parent",
            "model_override": "claude-opus-4-8",
            "llm_model": None,
        },
    )
    assert bodies[0]["model_override"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_opencode_worker_with_binding_skips_unlisted_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent model outside the binding's allowlist is not inherited.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _stub_worker_launchable(monkeypatch)
    config: dict[str, Any] = {
        "providers": {"gw": {}},
        "inference": {
            "harnesses": {
                "opencode-native": {
                    "provider": "gw",
                    "default_model": "claude-sonnet-4-6",
                    "model_allowlist": ["claude-sonnet-4-6"],
                }
            }
        },
    }
    monkeypatch.setattr(
        "omnigent.inference_config.load_runtime_inference_config",
        lambda base_config=None: config,
    )
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("opencode-native"),
        conv_id="conv_parent_opencode_unlisted",
        parent_snapshot={
            "id": "conv_parent_opencode_unlisted",
            "agent_id": "ag_parent",
            "model_override": "claude-opus-4-8",
            "llm_model": None,
        },
    )
    assert "model_override" not in bodies[0]


@pytest.mark.asyncio
async def test_binding_lets_foreign_multi_model_child_inherit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An inference binding overrides the foreign-multi-model skip: it owns the
    harness's model namespace (launch resolves ids through ``resolve_bound_model``
    and ``_dispatch_model_mismatch`` has vetted the id against the allowlist), so
    a binding-validated id is inherited even onto a different-harness pi child.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    _stub_worker_launchable(monkeypatch)
    config: dict[str, Any] = {
        "providers": {"gw": {}},
        "inference": {
            "harnesses": {
                "pi": {
                    "provider": "gw",
                    "default_model": "databricks-claude-sonnet-4-6",
                    "model_allowlist": [
                        "databricks-claude-sonnet-4-6",
                        "databricks-claude-opus-5",
                    ],
                }
            }
        },
    }
    monkeypatch.setattr(
        "omnigent.inference_config.load_runtime_inference_config",
        lambda base_config=None: config,
    )
    bodies = await _dispatch_without_model(
        monkeypatch,
        agent_spec=_spec_with_worker("pi"),
        conv_id="conv_parent_pi_binding",
        parent_snapshot={
            "id": "conv_parent_pi_binding",
            "agent_id": "ag_parent",
            "harness": "claude-native",
            "model_override": "databricks-claude-opus-5",
            "llm_model": None,
        },
    )
    assert bodies[0]["model_override"] == "databricks-claude-opus-5"
