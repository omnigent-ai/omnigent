"""
``build_policy_engine`` — construct a :class:`PolicyEngine` for a
workflow.

Called at the top of ``_run_agent_loop``. Seeds any
``LabelDef.initial`` values that are not already present in
``conversation_labels`` using an
``INSERT ... ON CONFLICT DO NOTHING`` semantic so that two
concurrent workflows on the same conversation (the v2 case
tracked in POLICIES.md Open Q #6) never clobber each other's
view of a label's first value.

Phase 2 scope: zero-policy and declared-policy paths both work;
concrete Policy subclasses land in Phases 3+, and this builder
will start instantiating them as those phases ship.
"""

from __future__ import annotations

from typing import Any

import cachetools

from omnigent.entities import Conversation
from omnigent.entities import Policy as StoredPolicy
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.context_window import fetch_model_pricing
from omnigent.policies.base import Policy
from omnigent.policies.function import resolve_function_policy
from omnigent.policies.schema import SESSION_COST_ASK_APPROVED_STATE_KEY
from omnigent.policies.types import PolicyLLMClient
from omnigent.runtime.credentials.databricks import resolve_databricks_workspace
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    LabelDef,
    LLMConfig,
    PolicySpec,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.policy_store import PolicyStore

# Dotted path of the per-user daily cost-budget factory. The engine is
# seeded with the session owner's daily-cost rollup ONLY when a policy
# set includes this handler — otherwise the owner + daily-cost lookups
# are skipped entirely, so sessions/deployments that don't use it pay
# nothing extra per evaluation.
_USER_DAILY_COST_POLICY_PATH = "omnigent.policies.builtins.cost.user_daily_cost_budget"

# Hardcoded policy that always ASKs before sys_add_policy executes.
# Injected unconditionally into every engine so agents cannot add
# policies without user approval.
_ASK_ON_ADD_POLICY_SPEC = FunctionPolicySpec(
    name="__ask_on_add_policy",
    on=None,
    function=FunctionRef(
        path="omnigent.policies.builtins.safety.ask_on_add_policy",
        arguments=None,
    ),
)

# Bounded cache of ``conversation_id -> session owner``. The owner
# (``LEVEL_OWNER`` grantee) is immutable for a session's lifetime, so
# caching it avoids a ``session_permissions`` lookup on every
# per-tool-call engine build. Only non-``None`` owners are cached (a
# session is granted its owner atomically at creation, so ``None`` is a
# transient single-user/pre-grant state, not worth caching).
_SESSION_OWNER_CACHE: cachetools.LRUCache[str, str] = cachetools.LRUCache(maxsize=4096)


def _needs_user_daily_cost(specs: list[PolicySpec]) -> bool:
    """
    Return whether any policy in *specs* is the per-user daily cost-budget.

    Drives the conditional injection: only when this returns ``True``
    does :func:`build_policy_engine` resolve the owner and read the
    daily-cost rollup.

    :param specs: The merged policy specs for the engine.
    :returns: ``True`` when a :class:`FunctionPolicySpec` references the
        ``user_daily_cost_budget`` factory.
    """
    return any(
        isinstance(s, FunctionPolicySpec)
        and s.function is not None
        and s.function.path == _USER_DAILY_COST_POLICY_PATH
        for s in specs
    )


def _resolve_session_owner_cached(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> str | None:
    """
    Resolve a session's owner, caching the immutable result.

    :param conversation_id: The session, e.g. ``"conv_abc123"``.
    :param conversation_store: Store for the owner lookup.
    :returns: The owner user id, or ``None`` when the session has no
        owner grant (single-user mode).
    """
    owner = _SESSION_OWNER_CACHE.get(conversation_id)
    if owner is not None:
        return owner
    owner = conversation_store.get_session_owner(conversation_id)
    if owner is not None:
        _SESSION_OWNER_CACHE[conversation_id] = owner
    return owner


def _load_user_daily_cost(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, float | str]:
    """
    Read the session owner's per-UTC-day cost rollup as the engine seed.

    Resolves the owner (cached) and reads ``{cost_usd, ask_approved_usd}``
    for today (UTC), tagged with the owner's ``user_id`` so the budget
    policy can name whose spend tripped the gate. When the session has no
    owner grant (single-user mode), returns zeros (and no ``user_id``) so
    the per-user daily budget never trips — consistent with the write
    path, which also no-ops without an owner.

    :param conversation_id: The session, e.g. ``"conv_abc123"``.
    :param conversation_store: Store for the owner + daily-cost lookups.
    :returns: ``{"cost_usd": <float>, "ask_approved_usd": <float>,
        "user_id": <owner>}``; ``user_id`` omitted in single-user mode.
    """
    from omnigent.db.utils import now_epoch, utc_day

    owner = _resolve_session_owner_cached(conversation_id, conversation_store)
    if owner is None:
        return {"cost_usd": 0.0, "ask_approved_usd": 0.0}
    state: dict[str, float | str] = dict(
        conversation_store.get_daily_cost_state(owner, utc_day(now_epoch()))
    )
    state["user_id"] = owner
    return state


def build_policy_engine(
    *,
    spec: AgentSpec,
    conversation_id: str,
    conversation_store: ConversationStore,
    connection_override: dict[str, str] | None = None,
    default_policies: list[PolicySpec] | None = None,
    policy_store: PolicyStore | None = None,
    server_llm: LLMConfig | None = None,
    host_connection: dict[str, str] | None = None,
) -> PolicyEngine:
    """
    Construct the :class:`PolicyEngine` for one workflow.

    When ``spec.guardrails`` is ``None`` (no guardrails
    declared), *default_policies* is empty, and no session
    policies are stored, returns a no-op engine with empty
    policies and labels — the four enforcement sites still
    call through, they just always ALLOW.

    When declared labels have an ``initial`` value and no row
    exists yet in ``conversation_labels``, seeds via
    ``ConversationStore.set_labels`` — but only for keys not
    already persisted, so existing label state is never
    clobbered. The hot cache is built from the freshly seeded
    snapshot.

    Policy run order: session policies (from the CRUD API)
    first, then agent spec policies, then *default_policies*
    (server-wide admin policies). This lets user-configured
    session policies short-circuit on DENY before agent or
    admin policies run, and gives admin policies the last
    word on ALLOW/ASK decisions.

    For sub-agent conversations, session policies from the
    root (top-level) conversation are inherited and prepended
    before any child-specific session policies. This ensures
    guardrails set on the parent session (e.g. via
    ``sys_add_policy``) also govern spawned sub-agents.
    Policies with the same ``name`` on both root and child
    are deduplicated (child wins).

    :param spec: The parsed agent spec.
    :param conversation_id: The conversation this workflow is
        running on, e.g. ``"conv_abc123"``.
    :param conversation_store: The store used for label reads
        and writes. Held by the engine for the life of the
        workflow.
    :param connection_override: Fallback ``{"base_url", "api_key"}``
        used by prompt policies whose spec declares no
        ``llm.connection``. Explicit policy / agent connections
        still win.
    :param default_policies: Server-wide policies appended after
        per-agent policies. Sourced from ``RuntimeCaps.default_policies``
        (parsed from the server ``--config`` YAML at startup).
        ``None`` and ``[]`` both mean no server-wide policies.
    :param policy_store: Session-scoped policy store. When
        provided, enabled policies for ``conversation_id`` are
        loaded and inserted between agent and admin policies in
        the evaluation order.
    :param server_llm: Server-level LLM configuration from
        ``RuntimeCaps.llm``. When provided, a
        :class:`~omnigent.policies.types.PolicyLLMClient` is
        constructed and injected into every function policy's
        ``event["llm_client"]``. ``None`` means no server-level
        LLM — function policies see ``None``.
    :param host_connection: Per-request ``{"base_url", "api_key"}``
        dict resolved from the caller's auth token (e.g. via
        :attr:`RuntimeCaps.policy_llm_connection_factory`). When
        provided, takes precedence over any connection derived from
        ``server_llm.connection`` / ``server_llm.profile``, so LLM
        calls are billed to the request caller rather than a static
        service credential. ``None`` falls back to the server-level
        connection.
    :returns: A :class:`PolicyEngine` ready for evaluation.
    """
    guardrails = spec.guardrails
    agent_policy_specs: list[PolicySpec] = list(guardrails.policies or []) if guardrails else []
    session_policy_specs = _load_session_policy_specs(conversation_id, policy_store)
    # Session policies are per-conversation, but sub-agents must inherit
    # the root conversation's policies so that guardrails set on the
    # top-level session (e.g. via sys_add_policy) also govern spawned
    # children. Load root policies and prepend them (root policies run
    # first, then any child-specific overrides, matching the cost-budget
    # root-seeding pattern below).
    conv = conversation_store.get_conversation(conversation_id)
    root_conversation_id = conv.root_conversation_id if conv is not None else conversation_id
    if root_conversation_id != conversation_id:
        root_policy_specs = _load_session_policy_specs(root_conversation_id, policy_store)
        # Deduplicate: skip root policies already present on the child
        # (keyed by policy name) to avoid double-evaluation.
        child_names = {p.name for p in session_policy_specs}
        root_policy_specs = [p for p in root_policy_specs if p.name not in child_names]
        session_policy_specs = root_policy_specs + session_policy_specs
    admin_policy_specs: list[PolicySpec] = list(default_policies or [])
    all_policy_specs = session_policy_specs + agent_policy_specs + admin_policy_specs

    # Always require user approval before sys_add_policy executes.
    # Appended unconditionally so the guard is present even when
    # no other guardrails are declared (the noop-engine path below
    # is no longer reachable since all_policy_specs is never empty).
    all_policy_specs.append(_ASK_ON_ADD_POLICY_SPEC)

    label_defs = (guardrails.labels or {}) if guardrails else {}
    initial_labels = _seed_and_load_labels(
        conversation_id=conversation_id,
        label_defs=label_defs,
        conversation_store=conversation_store,
    )
    initial_session_state = _load_session_state(conversation_id, conversation_store)
    # The cost-budget approval is per-SESSION: the whole spawn tree shares one
    # soft-threshold gate. A sub-agent runs as its own conversation, so seed its
    # approved-checkpoint from the ROOT conversation — otherwise approving on the
    # parent wouldn't carry to the sub-agent and it would re-ask at the same
    # threshold. Other session_state stays per-conversation; the matching
    # write-back is routed to the root by PolicyEngine.apply_state_updates.
    # (conv and root_conversation_id already resolved above for policy
    # inheritance — reuse them here.)
    if root_conversation_id != conversation_id:
        root_state = _load_session_state(root_conversation_id, conversation_store)
        if SESSION_COST_ASK_APPROVED_STATE_KEY in root_state:
            initial_session_state[SESSION_COST_ASK_APPROVED_STATE_KEY] = root_state[
                SESSION_COST_ASK_APPROVED_STATE_KEY
            ]
    # Gating is SESSION-wide: seed from the whole spawn-tree total so a
    # sub-agent gates against the session's full spend (parent + siblings),
    # not just its own subtree. The cost read is the enforcement total
    # (in-flight sub-agent spend); see _policy_usage_seed.
    initial_usage = _policy_usage_seed(conversation_id, conversation_store)
    # Conditional injection (#1): only pay the owner + daily-cost lookups
    # when a per-user daily cost-budget policy is actually present.
    initial_user_daily_cost = (
        _load_user_daily_cost(conversation_id, conversation_store)
        if _needs_user_daily_cost(all_policy_specs)
        else None
    )
    initial_model = _resolve_session_model(conversation_id, conversation_store, spec)
    # Pass the full ModelPricing so the engine can price cache-read and
    # cache-write tokens at their own rates via compute_llm_cost().
    token_pricing = fetch_model_pricing(spec.llm.model) if spec.llm else None
    server_connection = _resolve_server_llm_connection(server_llm)
    # host_connection carries the per-request caller token (billed to
    # the caller). It takes precedence over the static server-level
    # connection so policy LLM calls are attributed to the right
    # identity. Falls back to server_connection when absent.
    policy_connection = host_connection or server_connection
    llm_client = _build_policy_llm_client(server_llm, policy_connection)
    # Fall back to the server's gateway connection for prompt-policy
    # classifiers (else they default to api.openai.com).
    effective_connection_override = connection_override or server_connection
    return PolicyEngine(
        policies=[
            _instantiate_policy(
                s,
                agent_llm=spec.llm,
                connection_override=effective_connection_override,
            )
            for s in all_policy_specs
        ],
        label_defs=label_defs,
        ask_timeout=guardrails.ask_timeout if guardrails else DEFAULT_ASK_TIMEOUT,
        conversation_id=conversation_id,
        initial_labels=initial_labels,
        initial_session_state=initial_session_state,
        initial_usage=initial_usage,
        initial_user_daily_cost=initial_user_daily_cost,
        token_pricing=token_pricing,
        initial_model=initial_model,
        conversation_store=conversation_store,
        root_conversation_id=root_conversation_id,
        llm_client=llm_client,
    )


def _resolve_server_llm_connection(
    server_llm: LLMConfig | None,
) -> dict[str, str] | None:
    """
    Resolve the server-level LLM connection dict.

    Returns ``server_llm.connection`` directly when present;
    otherwise resolves ``server_llm.profile`` to a Databricks
    workspace connection. ``None`` when no server LLM is
    configured or it declares neither a connection nor a profile.

    :param server_llm: The server-level :class:`LLMConfig` from
        ``RuntimeCaps.llm``, or ``None``.
    :returns: A ``{"base_url", "api_key"}`` dict, or ``None``.
    :raises OSError: When ``profile`` is set but cannot be resolved.
    """
    if server_llm is None:
        return None
    if server_llm.connection is not None:
        return server_llm.connection
    if server_llm.profile is not None:
        return _resolve_databricks_connection(server_llm.profile)
    return None


def _build_policy_llm_client(
    server_llm: LLMConfig | None,
    connection: dict[str, str] | None,
) -> PolicyLLMClient | None:
    """
    Construct a :class:`PolicyLLMClient` from server-level LLM config.

    Returns ``None`` when no server-level ``llm:`` config is present.
    The :class:`~omnigent.llms.client.Client` is instantiated lazily
    here (no constructor args — auth routes per-call via
    ``connection_params``).

    :param server_llm: The server-level :class:`LLMConfig` from
        ``RuntimeCaps.llm``. ``None`` when the server config has no
        ``llm:`` block.
    :param connection: The connection dict already resolved by
        :func:`_resolve_server_llm_connection` (shared with the
        classifier connection-override fallback).
    :returns: A :class:`PolicyLLMClient` wrapping the client with
        pre-bound model/connection/timeout, or ``None``.
    """
    if server_llm is None:
        return None
    from omnigent.llms.client import Client

    # Models prefixed with ``databricks-`` (e.g.
    # ``databricks-claude-sonnet-4-6``) need the ``databricks/``
    # provider prefix so the LLM adapter routes through
    # DatabricksAdapter (Chat Completions) rather than
    # OpenAIAdapter (Responses API). Without this, the request
    # hits ``/responses`` on the Databricks gateway → 400.
    model = server_llm.model
    if "/" not in model and model.startswith("databricks-"):
        model = f"databricks/{model}"

    return PolicyLLMClient(
        _client=Client(),
        _model=model,
        _connection=connection,
        _request_timeout=server_llm.request_timeout,
    )


def _resolve_databricks_connection(profile: str) -> dict[str, str]:
    """
    Resolve a Databricks CLI profile to a connection dict.

    Uses
    :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`
    to resolve the profile to workspace host + bearer token, then
    builds the ``{"base_url": ..., "api_key": ...}`` dict that the
    LLM adapter expects.

    :param profile: The Databricks CLI profile name,
        e.g. ``"my-workspace"``.
    :returns: A connection dict with ``base_url`` (workspace host
        + ``/serving-endpoints``) and ``api_key`` (bearer token),
        e.g. ``{"base_url": "https://host/serving-endpoints",
        "api_key": "dapi..."}``.
    :raises OSError: When the profile cannot be resolved.
    """
    creds = resolve_databricks_workspace(profile)
    return {
        "base_url": creds.host + "/serving-endpoints",
        "api_key": creds.token,
    }


def _instantiate_policy(
    spec: PolicySpec,
    *,
    agent_llm: LLMConfig | None,
    connection_override: dict[str, str] | None = None,
) -> Policy:
    """
    Dispatch a :class:`PolicySpec` to the matching runtime
    :class:`Policy` subclass.

    :param spec: The declarative spec.
    :param agent_llm: The agent-level ``llm:`` config. Used
        as the default backend for :class:`PromptPolicy`
        when the policy didn't declare its own ``llm:``
        override. Unused for Function policies.
    :param connection_override: Forwarded to the prompt classifier
        as a fallback when the policy / agent declare no connection.
    :returns: A :class:`Policy` subclass instance bound to
        the spec.
    :raises NotImplementedError: When ``spec`` is not a
        known :class:`PolicySpec` subclass — parser bug
        protection.
    """
    if isinstance(spec, FunctionPolicySpec):
        return resolve_function_policy(spec)
    raise NotImplementedError(
        f"Policy type {type(spec).__name__} for {spec.name!r} is not "
        f"a known subclass of PolicySpec (FunctionPolicySpec).",
    )


def _build_noop_engine(
    *,
    conversation_id: str,
    conversation_store: ConversationStore,
) -> PolicyEngine:
    """
    Build an engine for an agent with no guardrails declared.

    Kept as a named helper rather than inlined so the
    zero-policy path is grep-able ("why is every phase
    returning ALLOW?" → search for ``_build_noop_engine``).

    :param conversation_id: The conversation for the workflow.
    :param conversation_store: Writes from this engine still
        go through the store — useful if a later turn of the
        same conversation runs under an updated spec that
        does declare guardrails.
    :returns: An engine with zero policies and an empty label
        cache.
    """
    # We still read the persisted labels and session_state (if any)
    # so an engine upgrade mid-conversation sees state its
    # predecessor wrote.
    existing = _load_existing_labels(conversation_id, conversation_store)
    initial_session_state = _load_session_state(conversation_id, conversation_store)
    # Inert here (no policies read usage), but kept identical to the live
    # engine's seed so the "engine usage == session-wide total" invariant
    # holds uniformly across both builders.
    initial_usage = _policy_usage_seed(conversation_id, conversation_store)
    return PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=DEFAULT_ASK_TIMEOUT,
        conversation_id=conversation_id,
        initial_labels=existing,
        initial_session_state=initial_session_state,
        initial_usage=initial_usage,
        conversation_store=conversation_store,
    )


def _seed_and_load_labels(
    *,
    conversation_id: str,
    label_defs: dict[str, LabelDef],
    conversation_store: ConversationStore,
) -> dict[str, str]:
    """
    Seed declared initial values and return the current snapshot.

    Race-safe across concurrent workflows: only writes keys
    that are missing from the persisted state. If two
    workflows seed simultaneously, the dialect-specific UPSERT
    guarantees one writer wins per (conversation, key) pair
    and the other no-ops.

    :param conversation_id: The conversation to seed.
    :param label_defs: Per-key declarations from the spec.
        Keys with ``initial is None`` are skipped (those
        labels start unset until a policy writes them).
    :param conversation_store: Target for both the read and
        the seed UPSERT.
    :returns: Full post-seed snapshot of the conversation's
        labels.
    """
    existing = _load_existing_labels(conversation_id, conversation_store)
    to_seed = {
        key: ldef.initial
        for key, ldef in label_defs.items()
        if ldef.initial is not None and key not in existing
    }
    if to_seed:
        conversation_store.set_labels(conversation_id, to_seed)
        # Re-read to pick up the freshly seeded values plus any
        # writes that landed concurrently from another workflow.
        existing = _load_existing_labels(conversation_id, conversation_store)
    return existing


def _load_existing_labels(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, str]:
    """
    Load the current persisted label state.

    Empty dict when the conversation has no labels yet (or
    when the conversation itself does not exist yet — the
    caller is responsible for ordering conversation creation
    before engine build).

    :param conversation_id: Conversation to load.
    :param conversation_store: Store to read from.
    :returns: ``{key: value}`` map. Empty when nothing
        persisted.
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    return dict(conv.labels)


def _load_session_state(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Load the current persisted session state.

    Empty dict when the conversation has no session state yet
    (or when the conversation itself does not exist yet — the
    caller is responsible for ordering conversation creation
    before engine build).

    :param conversation_id: Conversation to load,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read from.
    :returns: Session state dict. Empty when nothing persisted.
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    return dict(conv.session_state)


def _resolve_session_model(
    conversation_id: str,
    conversation_store: ConversationStore,
    spec: AgentSpec,
) -> str | None:
    """
    Resolve the model the session is currently using.

    Prefers the conversation's ``model_override`` (set when a user
    picks a model mid-session via ``/model`` or the web model picker)
    and falls back to the agent spec's ``llm.model``. ``None`` when
    neither is available — the conversation does not exist yet, has no
    override, and the spec declares no ``llm`` block — in which case
    cost policies treat the model as undeterminable.

    :param conversation_id: Conversation to read the override from,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read the conversation from.
    :param spec: The parsed agent spec (its ``llm.model`` is the
        fallback when no override is set).
    :returns: The active model id, e.g. ``"databricks-claude-opus-4-8"``
        or the native tier alias ``"opus"``; ``None`` when
        undeterminable.
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is not None and conv.model_override:
        return conv.model_override
    return spec.llm.model if spec.llm else None


# Page size for walking a spawn tree when summing sub-agent usage.
# Sub-agent trees are small in practice, but we still paginate so a
# large tree is not silently truncated (see load_session_usage).
_SUBTREE_USAGE_PAGE_SIZE = 100

# Usage counters summed across a conversation subtree. Restricted to the
# known numeric keys the PolicyEngine reads so an unexpected key in one
# conversation's persisted usage can't leak into the aggregate.
_SUMMABLE_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "total_cost_usd",
)


def _merge_by_model(
    aggregate: dict[str, dict[str, float]],
    per_conv: dict[str, Any],
) -> None:
    """
    Deep-merge one conversation's ``by_model`` sub-dict into the subtree aggregate.

    Unions model keys and sums each numeric per-bucket value (the token
    counters and ``total_cost_usd``) within each model, so a parent's
    per-model view folds in sub-agents that ran a different model. Mutates
    ``aggregate`` in place.

    :param aggregate: The running subtree ``by_model`` map being built, keyed
        by raw harness model id, e.g.
        ``{"claude-sonnet-4-6": {"input_tokens": 1200}}``.
    :param per_conv: One conversation's ``session_usage["by_model"]`` dict.
        Non-dict model buckets (malformed persisted data) are skipped.
    """
    for model, bucket in per_conv.items():
        if not isinstance(bucket, dict):
            continue
        agg_bucket = aggregate.setdefault(model, {})
        for key, value in bucket.items():
            # Only sum genuine numerics; ``bool`` is an ``int`` subclass so
            # exclude it explicitly to avoid summing a stray flag as 1.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                agg_bucket[key] = agg_bucket.get(key, 0.0) + value


def load_session_usage(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, Any]:
    """
    Load cumulative session usage for a conversation **plus all of its
    sub-agent descendants** (the subtree total).

    A cost-ask policy on a parent must see what its sub-agents spent,
    but each conversation persists only its own usage, so this sums the
    conversation and every conversation it transitively spawned (all
    share one ``root_conversation_id``). Read-only; the per-conversation
    write path is server-side ``_accumulate_session_usage`` /
    ``_persist_native_cumulative_usage``, not this function.

    Public because the server's session snapshot / ``session.usage`` SSE
    use this per-node subtree total to DISPLAY a node's own cost (a
    sub-agent's badge shows only its subtree). Cost GATING does NOT use
    this per-node view — it seeds from the whole-tree total via
    :func:`_policy_usage_seed` (which calls this with the tree root), so a
    sub-agent gates against the full session spend rather than just its own
    subtree.

    :param conversation_id: Conversation to load,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read from.
    :returns: Summed usage dict with keys ``input_tokens``,
        ``output_tokens``, ``total_tokens``, ``total_cost_usd`` (the
        DISPLAY cost sum — statusLine ``S`` for claude-native), and
        ``policy_cost_usd`` (the ENFORCEMENT cost sum — see below; only
        keys present in at least one conversation appear). When any
        conversation in the subtree recorded a per-model breakdown, a
        nested ``by_model`` key maps each raw harness model id to its
        own summed token/cost buckets (folding in sub-agents that ran a
        different model). Empty when the conversation does not exist or
        no usage is recorded. Display callers read ``total_cost_usd``;
        the policy seed (:func:`_policy_usage_seed`) reads
        ``policy_cost_usd`` (both unaffected by ``by_model``).
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    tree = _load_tree_conversations(conv.root_conversation_id, conversation_store)
    subtree_ids = _subtree_conversation_ids(tree, conversation_id)
    totals: dict[str, Any] = {}
    # Per-model breakdown summed across the subtree, parallel to the flat sums.
    by_model_totals: dict[str, dict[str, float]] = {}
    # Enforcement cost total, accumulated alongside the display sums so the
    # policy seed can pick it without a second tree pass.
    policy_cost_total = 0.0
    any_policy_cost = False
    for tree_conv in tree:
        if tree_conv.id not in subtree_ids:
            continue
        session_usage = tree_conv.session_usage
        for key in _SUMMABLE_USAGE_KEYS:
            value = session_usage.get(key)
            if value is not None:
                totals[key] = totals.get(key, 0.0) + value
        # Per-model sub-dict (nested ``by_model`` key) is ignored by the flat
        # ``_SUMMABLE_USAGE_KEYS`` loop above; merge it separately so the flat
        # sum (used by policy gating) stays unchanged and backward-compatible.
        per_conv_by_model = session_usage.get("by_model")
        if isinstance(per_conv_by_model, dict):
            _merge_by_model(by_model_totals, per_conv_by_model)
        # Enforcement cost: prefer this conversation's ``policy_cost_usd``
        # (claude-native's real-time figure incl. in-flight sub-agent spend),
        # else its displayed ``total_cost_usd`` (codex-native / relay don't
        # post the split). Kept separate from the ``total_cost_usd`` sum
        # above so the badge keeps the authoritative statusLine total.
        per_conv_policy_cost = session_usage.get("policy_cost_usd")
        if per_conv_policy_cost is None:
            per_conv_policy_cost = session_usage.get("total_cost_usd")
        if per_conv_policy_cost is not None:
            policy_cost_total += per_conv_policy_cost
            any_policy_cost = True
    if any_policy_cost:
        totals["policy_cost_usd"] = policy_cost_total
    if by_model_totals:
        totals["by_model"] = by_model_totals
    return totals


def _policy_usage_seed(
    conversation_id: str,
    conversation_store: ConversationStore,
) -> dict[str, float]:
    """
    SESSION-WIDE usage seed for the :class:`PolicyEngine`; cost = ENFORCEMENT total.

    Cost gating caps the **session** (the whole spawn tree), so this seeds
    from the tree-wide total — the spend rooted at ``root_conversation_id``
    — not just the subtree rooted at the node being evaluated. A sub-agent
    gated on its own subtree would miss its parent's and siblings' spend, so
    the session could overshoot its budget while the orchestrator parent is
    parked (it makes no tool calls, so its own gate never fires). For the
    root conversation this equals the per-node subtree (its subtree IS the
    whole tree), so only sub-agents change behavior.

    The cost the gate reads (``total_cost_usd`` in the returned seed) is the
    ENFORCEMENT total — ``policy_cost_usd`` when present (claude-native's
    real-time figure that reflects in-flight sub-agent spend while the
    displayed statusLine ``S`` is frozen), falling back to the displayed
    ``total_cost_usd`` for harnesses that don't post the split (codex-native,
    relay). The ``policy_cost_usd`` key is then dropped so the engine's usage
    context carries only the standard counters. Display callers use
    :func:`load_session_usage` directly (per-node subtree, authoritative
    ``total_cost_usd`` = ``S``), which is why the cost-budget gate can read a
    higher in-flight / session-wide total than a node's badge shows mid-turn.

    :param conversation_id: Conversation to seed the engine for, e.g.
        ``"conv_child"`` (a sub-agent) or ``"conv_root"`` (the session root).
    :param conversation_store: Store to read the tree usage from.
    :returns: Whole-tree usage seed dict; when an enforcement cost exists its
        ``total_cost_usd`` is the enforcement total and no ``policy_cost_usd``
        key remains. Empty when the conversation is absent or no usage is
        recorded.
    """
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return {}
    usage = load_session_usage(conv.root_conversation_id, conversation_store)
    # ``by_model`` is a display-only breakdown; drop it so the engine's usage
    # context carries only the flat numeric counters the gate reads.
    usage.pop("by_model", None)
    policy_cost = usage.pop("policy_cost_usd", None)
    if policy_cost is not None:
        usage["total_cost_usd"] = policy_cost
    return usage


def _load_tree_conversations(
    root_conversation_id: str,
    conversation_store: ConversationStore,
) -> list[Conversation]:
    """
    Page through every conversation in one spawn tree.

    Returns all conversations sharing ``root_conversation_id`` (the
    root plus every sub-agent, any ``kind``), paginating so a large
    tree is not silently truncated. The ``root_conversation_id`` column
    is indexed, so this is a bounded indexed scan per page.

    :param root_conversation_id: The tree's root conversation id (every
        conversation in a spawn tree shares it), e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read from.
    :returns: All conversations in the tree, in store order.
    """
    convs: list[Conversation] = []
    after: str | None = None
    while True:
        page = conversation_store.list_conversations(
            limit=_SUBTREE_USAGE_PAGE_SIZE,
            after=after,
            # None disables the kind filter so sub_agent conversations
            # (not just "default") are included in the tree.
            kind=None,
            root_conversation_id=root_conversation_id,
        )
        convs.extend(page.data)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return convs


def _subtree_conversation_ids(
    tree: list[Conversation],
    conversation_id: str,
) -> set[str]:
    """
    Collect a conversation id plus all its transitive sub-agent
    descendants within a spawn tree.

    Walking the subtree (rather than summing the whole ``tree``) keeps
    the aggregate correct when the policy is evaluated on a mid-tree
    sub-agent: that node sees its own spend and its children's, but not
    its parent's or siblings'.

    :param tree: All conversations in the spawn tree (from
        :func:`_load_tree_conversations`); order-independent.
    :param conversation_id: The subtree root to walk from,
        e.g. ``"conv_abc123"``.
    :returns: Set of conversation ids in the subtree rooted at
        ``conversation_id`` (always includes ``conversation_id``).
    """
    children_by_parent: dict[str, list[str]] = {}
    for tree_conv in tree:
        if tree_conv.parent_conversation_id is not None:
            children_by_parent.setdefault(tree_conv.parent_conversation_id, []).append(
                tree_conv.id
            )
    subtree: set[str] = set()
    stack = [conversation_id]
    while stack:
        node = stack.pop()
        if node in subtree:
            continue
        subtree.add(node)
        stack.extend(children_by_parent.get(node, []))
    return subtree


def _load_session_policy_specs(
    conversation_id: str,
    policy_store: PolicyStore | None,
) -> list[PolicySpec]:
    """
    Load enabled session policies from the store and convert
    them to :class:`FunctionPolicySpec` instances.

    Only ``type="python"`` policies are instantiable today. An
    enabled policy of an unsupported type (e.g. ``type="url"``)
    raises :class:`OmnigentError` rather than being skipped, so a
    stored guardrail that never enforces fails loudly.

    :param conversation_id: The session whose policies to load,
        e.g. ``"conv_abc123"``.
    :param policy_store: The session-scoped policy store.
        ``None`` returns an empty list.
    :returns: List of :class:`FunctionPolicySpec` for enabled
        session policies, in ``created_at ASC`` order.
    :raises OmnigentError: If an enabled policy has an unsupported
        ``type`` (e.g. ``type="url"``).
    """
    if policy_store is None:
        return []
    stored = policy_store.list_for_session(conversation_id)
    specs: list[PolicySpec] = []
    for policy in stored:
        if not policy.enabled:
            continue
        specs.append(_stored_policy_to_spec(policy))
    return specs


def _stored_policy_to_spec(policy: StoredPolicy) -> PolicySpec:
    """
    Convert a stored :class:`Policy` entity to a
    :class:`FunctionPolicySpec`.

    For ``type="python"``, creates a :class:`FunctionPolicySpec`
    with a :class:`FunctionRef` pointing at the stored handler
    path and optional factory params. Session policies fire on
    all phases (``on=None``) — the callable itself decides
    whether to act by inspecting ``event["type"]``.

    For ``type="url"``, raises :class:`OmnigentError` (URL policy evaluation
    is unimplemented). A stored policy that never enforces is a silent
    safety hole, so converting an unsupported type fails loudly rather than
    returning ``None``.

    :param policy: The stored session policy entity.
    :returns: A :class:`FunctionPolicySpec`.
    :raises OmnigentError: If the policy ``type`` cannot be evaluated yet
        (e.g. ``type="url"``).
    """
    if policy.type == "python":
        return FunctionPolicySpec(
            name=policy.name,
            # Session policies self-select: on=None means the
            # engine skips phase filtering and always dispatches.
            on=None,
            function=FunctionRef(
                path=policy.handler,
                arguments=policy.factory_params,
            ),
        )
    # Any non-"python" type (today only "url") cannot be evaluated yet.
    # Reject loudly and fail closed: a stored policy that silently never
    # enforces is worse than a visible failure the operator can act on.
    raise OmnigentError(
        f"Session policy {policy.name!r} (id {policy.id!r}) has unsupported "
        f"type {policy.type!r}; only type='python' policies can be evaluated "
        f"today. URL policy evaluation is a future extension. Remove or "
        f"disable this policy, since storing it does not enforce anything.",
        code=ErrorCode.INVALID_INPUT,
    )


__all__ = ["build_policy_engine"]
