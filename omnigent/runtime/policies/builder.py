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

import logging
from dataclasses import dataclass
from typing import Any

import cachetools

from omnigent.entities import Conversation
from omnigent.entities import Policy as StoredPolicy
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.llms.context_window import fetch_model_pricing_with_provider
from omnigent.policies.base import Policy
from omnigent.policies.function import resolve_function_policy
from omnigent.policies.schema import (
    SESSION_COST_ASK_APPROVED_STATE_KEY,
    SESSION_COST_UNPRICED_APPROVED_KEY,
)
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
    Phase,
    PolicySpec,
)
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.policy_store import PolicyStore

_logger = logging.getLogger(__name__)

# Dotted path of the per-user daily cost-budget factory. The engine is
# seeded with the session owner's daily-cost rollup ONLY when a policy
# set includes this handler — otherwise the owner + daily-cost lookups
# are skipped entirely, so sessions/deployments that don't use it pay
# nothing extra per evaluation.
_USER_DAILY_COST_POLICY_PATH = "omnigent.policies.builtins.cost.user_daily_cost_budget"

# Dotted path of the per-subagent cost-budget factory. The engine is
# seeded with the subtree-scoped usage ONLY when a policy set includes
# this handler — otherwise the subtree usage lookup is skipped.
_SUBAGENT_COST_POLICY_PATH = "omnigent.policies.builtins.cost.subagent_cost_budget"

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

# TTL cache of ``workspace_id -> list[PolicySpec]`` for DB-stored default
# policies. Default policies are admin-managed and change infrequently, so
# a short TTL (30 s) avoids one ``list_defaults()`` DB query per tool-call
# evaluation while still propagating changes within half a minute.
_DEFAULT_POLICY_SPECS_CACHE: cachetools.TTLCache[int, list[PolicySpec]] = cachetools.TTLCache(
    maxsize=256, ttl=30
)

# Invalidation-based LRU cache of ``(workspace_id, conversation_id) -> list[PolicySpec]``
# for session-scoped policies. Unlike defaults, session policies can be added
# mid-session (via sys_add_policy), so a TTL would delay enforcement. Instead,
# the cache is explicitly invalidated whenever a session policy is mutated via
# the CRUD routes. Keyed by workspace to prevent cross-tenant leakage.
# Bounded (LRU, 4096 entries) to match _SESSION_OWNER_CACHE and prevent unbounded
# growth — LRU eviction handles sessions that end without any policy mutation.
_SESSION_POLICY_SPECS_CACHE: cachetools.LRUCache[tuple[int, str], list[PolicySpec]] = (
    cachetools.LRUCache(maxsize=4096)
)


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


def _needs_subtree_usage(specs: list[PolicySpec]) -> bool:
    """
    Return whether any policy in *specs* is the per-subagent cost-budget.

    Drives the conditional injection: only when this returns ``True``
    does :func:`build_policy_engine` compute the subtree usage seed.

    :param specs: The merged policy specs for the engine.
    :returns: ``True`` when a :class:`FunctionPolicySpec` references the
        ``subagent_cost_budget`` factory.
    """
    return any(
        isinstance(s, FunctionPolicySpec)
        and s.function is not None
        and s.function.path == _SUBAGENT_COST_POLICY_PATH
        for s in specs
    )


def _normalize_usage_for_engine(usage: dict[str, float]) -> dict[str, float]:
    """
    Normalize a usage dict for injection into the policy engine.

    Removes display-only fields (``by_model``) and converts the
    enforcement-cost field (``policy_cost_usd``) to the engine's
    canonical ``total_cost_usd`` key. Both operations are idempotent:
    if a field is absent, the operation is a no-op.

    :param usage: The usage dict to normalize (modified in-place).
    :returns: The normalized dict (same object, for chaining).
    """
    usage.pop("by_model", None)
    policy_cost = usage.pop("policy_cost_usd", None)
    if policy_cost is not None:
        usage["total_cost_usd"] = policy_cost
    return usage


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
    owner: str | None = _SESSION_OWNER_CACHE.get(conversation_id)
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


def any_policies_apply(
    *,
    spec: AgentSpec,
    conversation_id: str,
    default_policies: list[PolicySpec] | None,
    policy_store: PolicyStore | None,
    phase: Phase | None = None,
    tool_name: str | None = None,
) -> bool:
    """Return ``True`` when at least one policy would run for this evaluation.

    Cheaper than building a full :class:`PolicyEngine`: only checks whether
    the combined policy list is non-empty. Used as a fast-path guard in
    ``POST /policies/evaluate`` to skip the engine build (and the associated
    conversation-store reads for labels/state/usage) when nothing would fire.

    Reads from the same caches as :func:`build_policy_engine`, so the check
    is O(1) for warm cache hits.

    :param spec: The agent's parsed spec.
    :param conversation_id: Conversation id, e.g. ``"conv_abc123"``.
    :param default_policies: Server-wide policies from ``RuntimeCaps``.
    :param policy_store: Session-scoped policy store; ``None`` means no DB
        policies are configured.
    :param phase: The evaluation phase, if known.
    :param tool_name: The tool being called (for ``PHASE_TOOL_CALL`` events).
    :returns: ``False`` when the engine would have an empty policy list and
        ``evaluate()`` would unconditionally return ALLOW/UNSPECIFIED.
    """
    # The engine unconditionally injects _ASK_ON_ADD_POLICY_SPEC so agents
    # cannot silently install session policies. Never fast-path sys_add_policy
    # TOOL_CALL events — they must always reach the engine for that gate.
    if phase == Phase.TOOL_CALL and tool_name == "sys_add_policy":
        return True
    if spec.guardrails and spec.guardrails.policies:
        return True
    if default_policies:
        return True
    if _load_default_policy_specs(policy_store):
        return True
    # Session policies are LRU-cached per (workspace_id, conversation_id) —
    # this is a cache hit on any call after the first for this session.
    if _load_session_policy_specs(conversation_id, policy_store):
        return True
    return False


def build_policy_engine(
    *,
    spec: AgentSpec,
    conversation_id: str,
    conversation_store: ConversationStore,
    conversation: Conversation | None = None,
    expected_agent_id: str | None = None,
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
    exists yet in ``conversation_labels``, seeds missing keys via
    :meth:`ConversationStore.set_labels`. The hot cache is built
    from the post-seed snapshot.

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
    :param conversation: The already-loaded conversation row for
        ``conversation_id``, when the caller holds a current one — skips
        the builder's own read.

        **Preload contract** (one rule, applied at every preload site):
        a preloaded row may supply only IMMUTABLE identity — its ``id``
        and ``root_conversation_id``. Every mutable field the engine
        depends on (labels, session_state, model_override) is re-derived
        from a fresh read taken here. A row that has since disappeared
        fails closed rather than authorizing from the snapshot. Callers
        that rebuild an engine specifically to observe concurrent writes
        (the native ASK gate's post-lock re-evaluation) pass ``None``.
    :param expected_agent_id: The ``agent_id`` the caller resolved *spec*
        from. ``agent_id`` is mutable (switch-agent) but selects the spec,
        so it must be read before the engine exists and cannot be
        re-derived here. Passing it lets the builder confirm it against
        the fresh row and fail closed on a mismatch, instead of
        authorizing an evaluation under the previous agent's guardrails.
        ``None`` skips the check (callers with no spec/agent coupling).
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
    if conversation is not None and conversation.id != conversation_id:
        # A misrouted preload would mix one session's labels, state, usage
        # and model into another session's authorization decision — fail
        # closed rather than build an engine from the wrong row.
        raise OmnigentError(
            f"preloaded conversation {conversation.id!r} does not match "
            f"conversation_id {conversation_id!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    conv = (
        conversation
        if conversation is not None
        else conversation_store.get_conversation(conversation_id)
    )
    # The row in hand only SUGGESTS a tree root. Loading the tree verifies it
    # and reports the root actually used, so everything downstream — the rows,
    # the root's own policies, the accounting sums — comes from one snapshot.
    # Deriving the root from the pre-refresh row while taking rows from a
    # corrected tree was the defect: a conversation deleted and recreated under
    # another root seeded the OLD tree's spend.
    verified = (
        load_verified_session_tree(
            conversation_id,
            conversation_store,
            conv.root_conversation_id if conv is not None else None,
        )
        if conv is not None
        else VerifiedSessionTree([], conversation_id, False)
    )
    tree = verified.rows
    root_conversation_id = verified.root_conversation_id
    db_default_policy_specs = _load_default_policy_specs(policy_store)
    admin_policy_specs: list[PolicySpec] = db_default_policy_specs + list(default_policies or [])
    all_policy_specs = session_policy_specs + agent_policy_specs + admin_policy_specs

    # Always require user approval before sys_add_policy executes.
    # Appended unconditionally so the guard is present even when
    # no other guardrails are declared (the noop-engine path below
    # is no longer reachable since all_policy_specs is never empty).
    all_policy_specs.append(_ASK_ON_ADD_POLICY_SPEC)

    label_defs = (guardrails.labels or {}) if guardrails else {}
    # One conversation read (``conv``, resolved above for policy
    # inheritance) and ONE spawn-tree load feed everything below: labels,
    # session state (own + inherited root keys), both usage seeds, and the
    # model override — on the single-page happy path, which is nearly every
    # build; a paged tree pays one further confirming read (see below). The
    # helpers each re-fetched the same rows before — ~4x conversation reads
    # plus two identical tree loads per build, the dominant cost of a
    # policies/evaluate call.
    # Freshness contract, applied to EVERY row this function decides from,
    # regardless of how it arrived: ONLY immutable identity (id,
    # root_conversation_id) survives from the row read above. Every mutable
    # field — labels, session_state, model_override, agent_id — is taken from
    # the tree load, which happened later and is therefore the newest read.
    #
    # The provenance of the earlier row does not change the hazard. A caller's
    # preload and this function's own ``get_conversation`` are both snapshots
    # taken before the tree scan, so both can be stale by the time a decision
    # is made; gating the refresh on ``conversation is not None`` closed the
    # window on one path and left the identical window open on the other.
    #
    # The tree load includes archived rows (archived conversations still hold
    # spend), so a row missing from the tree has genuinely been deleted.
    # Re-read once to confirm, then fail closed — never fall back to the
    # earlier copy, which would authorize from state captured before whatever
    # removed the row.
    if conv is not None:
        fresh_self = next((c for c in tree if c.id == conversation_id), None)
        if fresh_self is None:
            fresh_self = conversation_store.get_conversation(conversation_id)
        if fresh_self is None:
            # The row existed moments ago and is now gone (deleted
            # mid-request). Authorizing from the earlier copy would decide
            # against state that no longer exists; authorizing from empty
            # state would seed a $0 budget and ALLOW. Fail closed.
            raise OmnigentError(
                f"Conversation {conversation_id!r} disappeared while building its "
                f"policy engine; refusing to authorize from a stale snapshot.",
                code=ErrorCode.CONFLICT,
            )
        conv = fresh_self
    # Agent/spec confirmation — deliberately AFTER the refresh above, and
    # nowhere else. Comparing against the earlier row (as a previous revision
    # did) validated the very snapshot whose staleness is the hazard, so a
    # switch-agent in the window was accepted. Exact equality: a fresh row
    # whose binding is ``None``, or no fresh row at all, is a mismatch too —
    # not a reason to skip the check.
    if expected_agent_id is not None:
        fresh_agent_id = conv.agent_id if conv is not None else None
        if fresh_agent_id != expected_agent_id:
            raise OmnigentError(
                f"Session {conversation_id!r} no longer resolves to agent "
                f"{expected_agent_id!r} (now {fresh_agent_id!r}); the spec this "
                f"engine would enforce is stale. Re-resolve and retry.",
                code=ErrorCode.CONFLICT,
            )
    # A paged tree does not share one read instant: rows on page one were read
    # before page two, so "the tree read is newer than the caller's row" holds
    # for the tree but not for any row inside it. Confirm identity once when
    # that is actually the case — single-page trees, which is nearly all of
    # them, pay nothing.
    if conv is not None and verified.paged:
        confirmed = conversation_store.get_conversation(conversation_id)
        if (
            confirmed is None
            or confirmed.root_conversation_id != root_conversation_id
            or confirmed.agent_id != conv.agent_id
        ):
            raise OmnigentError(
                f"Conversation {conversation_id!r} moved while its spawn tree was "
                f"being paged; refusing to authorize against a tree assembled "
                f"across the change.",
                code=ErrorCode.CONFLICT,
            )
        conv = confirmed
    # Session policies are per-conversation, but sub-agents inherit the root
    # conversation's policies so guardrails set on the top-level session (e.g.
    # via sys_add_policy) also govern spawned children. Loaded from the
    # VERIFIED root, after the refresh: reading them from the caller's
    # suggested root inherited another tree's guardrails.
    if root_conversation_id != conversation_id:
        root_policy_specs = _load_session_policy_specs(root_conversation_id, policy_store)
        # Deduplicate: skip root policies already present on the child
        # (keyed by policy name) to avoid double-evaluation.
        child_names = {p.name for p in session_policy_specs}
        root_policy_specs = [p for p in root_policy_specs if p.name not in child_names]
        session_policy_specs = root_policy_specs + session_policy_specs
        all_policy_specs = (
            session_policy_specs
            + agent_policy_specs
            + admin_policy_specs
            + [_ASK_ON_ADD_POLICY_SPEC]
        )
    root_conv = (
        conv
        if root_conversation_id == conversation_id
        else next((c for c in tree if c.id == root_conversation_id), None)
    )
    if root_conv is None and conv is not None and root_conversation_id != conversation_id:
        # The tree now includes archived rows, so a missing root means the row
        # is genuinely gone (deleted mid-request). Read once to confirm before
        # deciding — never silently proceed on an absent root.
        root_conv = conversation_store.get_conversation(root_conversation_id)
    initial_labels = _seed_and_load_labels(
        conversation_id=conversation_id,
        label_defs=label_defs,
        conversation_store=conversation_store,
        existing=dict(conv.labels) if conv is not None else {},
    )
    initial_session_state = dict(conv.session_state) if conv is not None else {}
    # The cost-budget approval is per-SESSION: the whole spawn tree shares one
    # soft-threshold gate. A sub-agent runs as its own conversation, so seed its
    # approved-checkpoint from the ROOT conversation — otherwise approving on the
    # parent wouldn't carry to the sub-agent and it would re-ask at the same
    # threshold. Other session_state stays per-conversation; the matching
    # write-back is routed to the root by PolicyEngine.apply_state_updates.
    if root_conversation_id != conversation_id:
        root_state = dict(root_conv.session_state) if root_conv is not None else {}
        for _root_key in (
            SESSION_COST_ASK_APPROVED_STATE_KEY,
            SESSION_COST_UNPRICED_APPROVED_KEY,
        ):
            if _root_key in root_state:
                initial_session_state[_root_key] = root_state[_root_key]
    # Gating is SESSION-wide: seed from the whole spawn-tree total so a
    # sub-agent gates against the session's full spend (parent + siblings),
    # not just its own subtree. The cost read is the enforcement total
    # (in-flight sub-agent spend); see _policy_usage_seed, whose semantics
    # (including the empty seed when the root row is missing) this
    # preserves while reusing the single tree load.
    initial_usage = (
        _normalize_usage_for_engine(_sum_subtree_usage(tree, root_conversation_id))
        if conv is not None and root_conv is not None
        else {}
    )
    # Conditional injection (#1a): only compute subtree usage when a
    # subagent_cost_budget policy is present. Per-node DISPLAY-rooted seed:
    # same tree, rooted at the evaluated node instead of the root.
    initial_subtree_usage: dict[str, float] | None = None
    if _needs_subtree_usage(all_policy_specs):
        initial_subtree_usage = (
            _normalize_usage_for_engine(_sum_subtree_usage(tree, conversation_id))
            if conv is not None
            else {}
        )
    # Conditional injection (#1): only pay the owner + daily-cost lookups
    # when a per-user daily cost-budget policy is actually present.
    initial_user_daily_cost = (
        _load_user_daily_cost(conversation_id, conversation_store)
        if _needs_user_daily_cost(all_policy_specs)
        else None
    )
    # Session model: the conversation's model_override (set when a user
    # picks a model mid-session) wins over the spec's llm.model; None when
    # neither is available and cost policies treat it as undeterminable.
    initial_model = (
        conv.model_override
        if conv is not None and conv.model_override
        else (spec.llm.model if spec.llm else None)
    )
    # Pass the full ModelPricing so the engine can price cache-read and
    # cache-write tokens at their own rates via compute_llm_cost(). Check
    # provider config first for custom pricing (self-hosted models), then
    # fall back to catalog. Use the effective model from the session (honors
    # model_override) rather than the static spec model.
    token_pricing: Any = None
    if initial_model:
        # Load provider config to check for custom pricing
        from omnigent.onboarding.provider_config import load_config

        provider_config = load_config()
        # Match the harness running this session; an override wins over the
        # agent's configured executor harness.
        harness = (
            conv.harness_override
            if conv is not None and conv.harness_override
            else spec.executor.harness_kind
        )
        token_pricing = fetch_model_pricing_with_provider(
            initial_model, provider_config=provider_config, harness=harness
        )
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
        initial_subtree_usage=initial_subtree_usage,
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

    primary = _normalize_policy_model(server_llm.model)
    fallbacks = [_normalize_policy_model(m) for m in server_llm.fallback_models]

    # The resolved ``connection`` (api_key / profile creds) is shared
    # across the primary and every fallback. It is provider-specific,
    # so a fallback on a different provider would be handed the wrong
    # credentials. Warn at build time rather than failing mid-request.
    if connection is not None:
        primary_provider = _model_provider(primary)
        mismatched = sorted(
            {_model_provider(m) for m in fallbacks if _model_provider(m) != primary_provider}
        )
        if mismatched:
            _logger.warning(
                "Policy llm: connection is configured for provider %r but "
                "fallback_models target %s; the shared connection likely "
                "won't authenticate those providers. Use same-provider "
                "fallbacks, or rely on environment defaults (no connection).",
                primary_provider,
                mismatched,
            )

    return PolicyLLMClient(
        _client=Client(),
        _model=primary,
        _connection=connection,
        _request_timeout=server_llm.request_timeout,
        _fallback_models=fallbacks,
    )


def _normalize_policy_model(model: str) -> str:
    """
    Apply the ``databricks-`` → ``databricks/`` provider-prefix fixup.

    Models prefixed with ``databricks-`` (e.g.
    ``databricks-claude-sonnet-4-6``) need the ``databricks/``
    provider prefix so the LLM adapter routes through
    ``DatabricksAdapter`` (Chat Completions) rather than
    ``OpenAIAdapter`` (Responses API). Without this, the request
    hits ``/responses`` on the Databricks gateway → 400. Applied
    uniformly to the primary model and every fallback so the
    fallback path routes the same way as the primary.

    :param model: A model id from the server ``llm:`` config,
        possibly a bare ``databricks-`` name.
    :returns: The model id with the ``databricks/`` prefix applied
        when needed; otherwise unchanged.
    """
    if "/" not in model and model.startswith("databricks-"):
        return f"databricks/{model}"
    return model


def _model_provider(model: str) -> str:
    """
    Extract the provider prefix from a normalized model id.

    :param model: A provider-prefixed model id, e.g.
        ``"databricks/claude-sonnet-4"`` or ``"openai/gpt-4o-mini"``.
    :returns: The provider segment before the first ``/`` (e.g.
        ``"openai"``), or the whole string when unprefixed.
    """
    return model.split("/", 1)[0] if "/" in model else model


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
    existing: dict[str, str] | None = None,
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
    :param existing: Pre-loaded current label snapshot, passed by callers
        that already hold the conversation row (saves a re-read).
        ``None`` loads it here.
    :returns: Full post-seed snapshot of the conversation's
        labels.
    """
    if existing is None:
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
    *,
    root_conversation_id: str | None = None,
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
    :param root_conversation_id: The conversation's tree root, when the
        caller already holds the row. Skips the internal conversation read.
        The root binding is immutable **per row**, not per conversation id:
        a conversation deleted and recreated under the same id gets a new
        row, whose root may differ. A supplied root is therefore validated
        against the tree it produces — if this conversation is not in that
        tree, the caller's row is stale and the root is resolved here
        instead. ``None`` resolves it here from the start.
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
    tree = load_session_tree(conversation_id, conversation_store, root_conversation_id)
    return _sum_subtree_usage(tree, conversation_id)


def load_session_tree(
    conversation_id: str,
    conversation_store: ConversationStore,
    root_conversation_id: str | None = None,
) -> list[Conversation]:
    """
    Load the spawn tree *conversation_id* belongs to, verifying the root.

    One place owns the reuse rule for a caller-supplied tree root, so every
    consumer gets the same guarantee: the tree comes back containing this
    conversation, or the supplied root was stale and is resolved again.

    A caller's ``root_conversation_id`` is immutable **per row**. Deleting a
    conversation and recreating it under the same id produces a new row that
    may sit in a different tree, so a row read earlier in the request can
    name a root this conversation no longer belongs to. Rather than trust it
    or re-read unconditionally, the supplied root is checked against the
    tree it produced — a membership test on rows already in memory, so the
    happy path costs nothing and the stale path costs one read.

    :param conversation_id: The conversation whose tree is wanted,
        e.g. ``"conv_abc123"``.
    :param conversation_store: Store to read from.
    :param root_conversation_id: Caller-supplied tree root, validated as
        above. ``None`` resolves the root here.
    :returns: Every conversation in the tree (root plus all descendants,
        archived included). Empty when the conversation does not exist.
    """
    return load_verified_session_tree(
        conversation_id, conversation_store, root_conversation_id
    ).rows


@dataclass(frozen=True)
class VerifiedSessionTree:
    """A spawn tree together with what is known about how it was loaded.

    :param rows: Every conversation in the tree, archived included. Empty
        when the conversation does not exist.
    :param root_conversation_id: The root the rows were actually loaded
        from, which is not necessarily the one the caller suggested.
    :param paged: Whether the listing needed more than one page. Rows on an
        earlier page were read before rows on a later one, so for a paged
        tree "the tree read is newer than the caller's row" holds for the
        tree as a whole but not for any individual row in it.
    """

    rows: list[Conversation]
    root_conversation_id: str
    paged: bool


def load_verified_session_tree(
    conversation_id: str,
    conversation_store: ConversationStore,
    root_conversation_id: str | None = None,
) -> VerifiedSessionTree:
    """
    Load a spawn tree and report the root it came from.

    Same verification as :func:`load_session_tree`, but callers that derive
    more than the sums from a tree — the tree root itself, the policies
    attached to that root — need to know which root was used, because a
    supplied one may have been discarded. Deriving those from the caller's
    root while taking the rows from a corrected tree mixes two epochs.

    :param conversation_id: The conversation whose tree is wanted.
    :param conversation_store: Store to read from.
    :param root_conversation_id: Caller-supplied root, treated as a hint.
    :returns: The rows, the root they came from, and whether it paged.
    """
    if root_conversation_id is None:
        conv = conversation_store.get_conversation(conversation_id)
        if conv is None:
            return VerifiedSessionTree([], conversation_id, False)
        root_conversation_id = conv.root_conversation_id
        rows, paged = _load_tree_pages(root_conversation_id, conversation_store)
        return VerifiedSessionTree(rows, root_conversation_id, paged)

    rows, paged = _load_tree_pages(root_conversation_id, conversation_store)
    if any(c.id == conversation_id for c in rows):
        return VerifiedSessionTree(rows, root_conversation_id, paged)
    # Not in the tree the supplied root produced: either this conversation
    # is gone, or it now lives in a different tree. Resolve it once.
    conv = conversation_store.get_conversation(conversation_id)
    if conv is None:
        return VerifiedSessionTree([], root_conversation_id, paged)
    if conv.root_conversation_id == root_conversation_id:
        return VerifiedSessionTree(rows, root_conversation_id, paged)
    rows, paged = _load_tree_pages(conv.root_conversation_id, conversation_store)
    return VerifiedSessionTree(rows, conv.root_conversation_id, paged)


def _sum_subtree_usage(
    tree: list[Conversation],
    conversation_id: str,
) -> dict[str, Any]:
    """
    Sum usage across the subtree of *tree* rooted at *conversation_id*.

    Pure aggregation over an already-loaded spawn tree — no store reads.
    :func:`build_policy_engine` loads the tree once and derives both the
    session-wide gating seed (rooted at the tree root) and the per-node
    subtree seed (rooted at the evaluated node) from the same list;
    :func:`load_session_usage` wraps this for callers that start from a
    conversation id. See :func:`load_session_usage` for the shape of the
    returned dict.

    :param tree: All conversations in the spawn tree (from
        :func:`_load_tree_conversations`); order-independent.
    :param conversation_id: The subtree root to sum from.
    :returns: Summed usage dict (see :func:`load_session_usage`).
    """
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
    return _normalize_usage_for_engine(usage)


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
            # Archived conversations still hold spend, and archiving must not
            # move a budget gate: excluding them let an archive-after-preload
            # (or an archived mid-tree node, which orphaned its descendants
            # from the walk) seed the enforcement total as $0 and ALLOW a tool
            # call over budget. The tree is an accounting structure, not a
            # user-facing listing.
            include_archived=True,
        )
        convs.extend(page.data)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return convs


def _load_tree_pages(
    root_conversation_id: str,
    conversation_store: ConversationStore,
) -> tuple[list[Conversation], bool]:
    """
    Page through a spawn tree, reporting whether more than one page was read.

    :param root_conversation_id: The tree's root conversation id.
    :param conversation_store: Store to read from.
    :returns: ``(rows, paged)`` — ``paged`` is ``True`` when the listing
        needed a second page, which means the rows do not share one read
        instant.
    """
    convs: list[Conversation] = []
    after: str | None = None
    pages = 0
    while True:
        page = conversation_store.list_conversations(
            limit=_SUBTREE_USAGE_PAGE_SIZE,
            after=after,
            kind=None,
            root_conversation_id=root_conversation_id,
            include_archived=True,
        )
        pages += 1
        convs.extend(page.data)
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return convs, pages > 1


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


def ancestor_ids_from_tree(
    tree: list[Conversation],
    conversation_id: str,
) -> list[str]:
    """
    Walk a conversation's ancestor chain inside an already-loaded tree.

    The mirror of :func:`_subtree_conversation_ids`, and public for the
    same reason the tree loader is: the ancestor chain must come from the
    same freshly-read rows as the sums, not from a conversation row the
    caller read earlier. ``parent_conversation_id`` is immutable per row,
    but a conversation deleted and recreated under the same id gets a new
    row with a new parent, so a caller's copy can name a chain that no
    longer exists — and walking it publishes to the wrong sessions.

    Pure: no store reads, and no reads are needed, because a tree already
    contains every row on the chain by construction.

    :param tree: All conversations in the spawn tree (from
        :func:`load_session_tree`); order-independent.
    :param conversation_id: The node to walk upward from,
        e.g. ``"conv_child123"``.
    :returns: Ancestor ids nearest-parent-first. Empty when the node is
        top-level, absent from the tree, or its chain is cyclic.
    """
    by_id = {c.id: c for c in tree}
    ancestors: list[str] = []
    seen = {conversation_id}
    current = by_id.get(conversation_id)
    while current is not None and current.parent_conversation_id is not None:
        parent_id = current.parent_conversation_id
        if parent_id in seen:
            # A cycle makes the whole chain untrustworthy, not just the rest
            # of it: returning the part walked so far would publish a
            # descendant's usage to whichever ids happened to come first.
            return []
        parent = by_id.get(parent_id)
        if parent is None:
            # The parent link points outside this tree. Appending before
            # checking published to a conversation that is not there.
            return []
        ancestors.append(parent_id)
        seen.add(parent_id)
        current = parent
    return ancestors


def _load_default_policy_specs(
    policy_store: PolicyStore | None,
) -> list[PolicySpec]:
    """
    Load enabled server-wide default policies from the store.

    These are policies created via ``POST /v1/policies`` (``session_id IS
    NULL``). They run after agent-spec policies and before YAML-based
    admin policies in the evaluation order.

    Results are cached per workspace for 30 s (see
    :data:`_DEFAULT_POLICY_SPECS_CACHE`) to avoid a ``list_defaults()``
    DB round-trip on every tool-call evaluation. The cache is keyed by
    workspace id so multi-tenant deployments never share results across
    tenants. Call :func:`invalidate_default_policy_specs_cache` after any
    mutation to make changes visible before the TTL expires.

    :param policy_store: The policy store. ``None`` returns an empty list.
    :returns: List of :class:`FunctionPolicySpec` for enabled default
        policies, in ``created_at ASC`` order.
    :raises OmnigentError: If an enabled policy has an unsupported type.
    """
    if policy_store is None:
        return []
    from omnigent.db.db_models import current_workspace_id

    workspace_id = current_workspace_id()
    cached: list[PolicySpec] | None = _DEFAULT_POLICY_SPECS_CACHE.get(workspace_id)
    if cached is not None:
        return cached
    specs: list[PolicySpec] = []
    for policy in policy_store.list_defaults():
        if not policy.enabled:
            continue
        if policy.type != "python":
            # Skip unsupported types with a warning rather than raising.
            # A session-scoped policy of unsupported type fails loudly (blast
            # radius: one session); a default policy of unsupported type would
            # crash engine construction for every session server-wide. Log and
            # skip so a stale or manually-inserted row can't cause an outage.
            _logger.warning(
                "Skipping default policy %r (id=%r): unsupported type %r — "
                "only type='python' can be evaluated. Disable or delete this "
                "policy to suppress this warning.",
                policy.name,
                policy.id,
                policy.type,
            )
            continue
        specs.append(_stored_policy_to_spec(policy))
    _DEFAULT_POLICY_SPECS_CACHE[workspace_id] = specs
    return specs


def invalidate_default_policy_specs_cache() -> None:
    """
    Evict the current workspace's entry from the default-policy specs cache.

    Call this after any mutation (create, update, delete) of a default
    policy so the next :func:`build_policy_engine` call re-reads from the
    DB rather than serving a stale TTL entry. Scoped to the current
    workspace context via :func:`~omnigent.db.db_models.current_workspace_id`.
    """
    from omnigent.db.db_models import current_workspace_id

    _DEFAULT_POLICY_SPECS_CACHE.pop(current_workspace_id(), None)


def invalidate_session_policy_specs_cache(conversation_id: str) -> None:
    """
    Evict a conversation's entry from the session policy specs cache.

    Call this after any mutation (create, update, delete) of a session
    policy so the next :func:`build_policy_engine` call re-reads from
    the DB. Scoped to the current workspace context.

    :param conversation_id: The session whose cache entry to evict,
        e.g. ``"conv_abc123"``.
    """
    from omnigent.db.db_models import current_workspace_id

    _SESSION_POLICY_SPECS_CACHE.pop((current_workspace_id(), conversation_id), None)


def _load_session_policy_specs(
    conversation_id: str,
    policy_store: PolicyStore | None,
) -> list[PolicySpec]:
    """
    Load enabled session policies from the store and convert
    them to :class:`FunctionPolicySpec` instances.

    Results are cached per ``(workspace_id, conversation_id)`` and
    invalidated on any mutation via :func:`invalidate_session_policy_specs_cache`.
    There is no TTL — the cache entry is permanent until explicitly evicted,
    so session policy changes (including ``sys_add_policy``) take effect
    immediately on the next engine build.

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
    from omnigent.db.db_models import current_workspace_id

    key = (current_workspace_id(), conversation_id)
    cached: list[PolicySpec] | None = _SESSION_POLICY_SPECS_CACHE.get(key)
    if cached is not None:
        return cached
    stored = policy_store.list_for_session(conversation_id)
    specs: list[PolicySpec] = []
    for policy in stored:
        if not policy.enabled:
            continue
        specs.append(_stored_policy_to_spec(policy))
    _SESSION_POLICY_SPECS_CACHE[key] = specs
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


__all__ = [
    "build_policy_engine",
    "invalidate_default_policy_specs_cache",
    "invalidate_session_policy_specs_cache",
]
