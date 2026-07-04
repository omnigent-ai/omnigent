"""
``PolicyEngine`` — per-workflow owner of policies + label state.

The engine is a plain local constructed at the top of
``_run_agent_loop`` and passed explicitly to the enforcement
sites. No ContextVar, no container class (see POLICIES.md §4
for the rationale).

The engine dispatches to registered :class:`Policy` instances
(:class:`FunctionPolicy` and :class:`PromptPolicy`).
The orchestration here handles composition.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from omnigent.llms.context_window import ModelPricing, compute_llm_cost
from omnigent.policies.base import Policy
from omnigent.policies.types import EvaluationContext, PolicyResult
from omnigent.spec.types import (
    DEFAULT_ASK_TIMEOUT,
    LabelDef,
    PolicyAction,
    PolicySpec,
    StateUpdate,
    StateUpdateAction,
)
from omnigent.stores.conversation_store import ConversationStore

# Number of recent conversation items the engine fetches from
# the conversation store and threads onto :class:`EvaluationContext`
# before dispatching to each policy. ``PromptPolicy``'s classifier
# sees these items so its emitted ``reason`` text can be
# situational ("agent ran ``pip install foo``" → reason mentions
# ``foo``) rather than generic. Tunable later if needed; pinned
# here so it surfaces in grep across the engine + prompt layers.
# See designs/LIVE_POLICIES.md §4.1.
_TRAJECTORY_WINDOW = 10


class PolicyEngine:
    """
    Owns policies + label state for one workflow execution.

    Constructed once at the top of ``_run_agent_loop`` via
    :func:`build_policy_engine` and passed explicitly to the
    four enforcement sites (§5). Labels are hot-cached on the
    engine for the life of the workflow and written through to
    ``conversation_labels`` via the conversation store on every
    ``apply_label_writes`` call.

    :param policies: Per-workflow :class:`Policy` instances
        in YAML declaration order. The engine iterates this
        list in order on every ``evaluate`` call; DENY
        short-circuits, ASK accumulates, ALLOW continues.
    :param label_defs: Per-key ``LabelDef`` schemas from the
        agent spec. Used by ``apply_label_writes`` to validate
        ``values`` + ``monotonic`` constraints. Empty dict
        when no labels were declared.
    :param ask_timeout: Spec-wide default approval timeout in
        seconds (POLICIES.md §7). Per-policy overrides live on
        :class:`PolicySpec` and are looked up via
        :meth:`spec_for`.
    :param conversation_id: The conversation this engine owns
        label state for.
    :param initial_labels: Labels already persisted for the
        conversation at workflow-start (the hot cache seed).
    :param initial_session_state: Seed state for the hot cache.
        Policies can read the current state via
        ``event["session_state"]`` and request updates via
        :attr:`PolicyResult.state_updates`. State is in-memory only
        for the engine's lifetime; cross-turn persistence is the
        caller's responsibility. Empty dict when no seed is provided.
    :param initial_usage: Cumulative LLM token usage seed, e.g.
        ``{"input_tokens": 500, "output_tokens": 200,
        "total_tokens": 700, "total_cost_usd": 0.012}``. Loaded
        from the conversation's persisted ``session_usage`` at
        engine-build time so counters survive across turns.
        Defaults to all-zeros when no prior usage exists.
    :param token_pricing: Full per-token pricing from the MLflow
        catalog, including cache-read and cache-write rates.
        ``None`` when pricing is unavailable — ``total_cost_usd``
        stays at ``0.0`` in that case.
    :param initial_model: The model the session is currently
        using — the conversation's ``model_override`` when set,
        else the agent spec's ``llm.model``, e.g.
        ``"databricks-claude-opus-4-8"`` or the native tier alias
        ``"opus"``. Resolved at engine-build time and injected
        into every policy dispatch as ``event["context"]["model"]``
        so callables can gate on the active model. ``None`` when
        no override and no spec ``llm`` (model undeterminable).
    :param conversation_store: Write-through target for label
        mutations. Held by reference so every
        ``apply_label_writes`` call goes to the same backing store.
    :param llm_client: A shared
        :class:`~omnigent.policies.types.PolicyLLMClient`
        instance for policy LLM calls. Built from the server-level
        ``llm:`` config at engine build time. ``None`` when the
        server has no ``llm:`` config — function policies that
        need an LLM will see ``None`` in ``event["llm_client"]``.
    """

    def __init__(
        self,
        *,
        policies: list[Policy],
        label_defs: dict[str, LabelDef],
        ask_timeout: int,
        conversation_id: str,
        initial_labels: dict[str, str],
        initial_session_state: dict[str, Any] | None = None,
        initial_usage: dict[str, float] | None = None,
        initial_subtree_usage: dict[str, float] | None = None,
        initial_user_daily_cost: dict[str, float | str] | None = None,
        token_pricing: ModelPricing | None = None,
        initial_model: str | None = None,
        conversation_store: ConversationStore,
        root_conversation_id: str | None = None,
        llm_client: Any = None,
    ) -> None:
        self.policies = policies
        self.label_defs = label_defs
        self.ask_timeout = ask_timeout
        self._conversation_id = conversation_id
        # Root of this conversation's spawn tree. The per-session cost-budget
        # approval is routed here (not the per-conversation session_state) so
        # approving once covers the whole tree — a sub-agent runs as its own
        # conversation. Defaults to ``conversation_id`` for a top-level session.
        self._root_conversation_id = root_conversation_id or conversation_id
        self._labels = dict(initial_labels)
        self._session_state: dict[str, Any] = dict(initial_session_state or {})
        self._usage: dict[str, float] = dict(
            initial_usage
            or {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_cost_usd": 0.0,
            }
        )
        # Ensure cache-token keys exist even when seeded from old
        # persisted usage that predates cache-token tracking.
        self._usage.setdefault("cache_read_input_tokens", 0)
        self._usage.setdefault("cache_creation_input_tokens", 0)
        # Subtree-scoped usage seed (this conversation + its descendants
        # only, not the whole session tree). Seeded at build time ONLY when
        # a ``subagent_cost_budget`` policy is configured — ``None``
        # otherwise, so sessions without that policy pay no subtree lookup.
        self._subtree_usage: dict[str, float] | None = (
            dict(initial_subtree_usage) if initial_subtree_usage is not None else None
        )
        # The session owner's per-UTC-day cost rollup
        # ({"cost_usd", "ask_approved_usd"}), seeded at build time ONLY
        # when a policy needs it (per-user daily cost-budget configured).
        # ``None`` → not needed → never injected, so no owner/daily lookup
        # cost for sessions that don't use the daily policy.
        self._user_daily_cost = initial_user_daily_cost
        self._token_pricing = token_pricing
        self._model = initial_model
        self._store = conversation_store
        self._llm_client = llm_client

    @property
    def labels(self) -> dict[str, str]:
        """
        Read-only snapshot of the hot label cache.

        Returns a defensive copy so callers that mutate the
        dict do not corrupt engine state. Policies read labels
        through the ``context`` passed into their ``evaluate``
        method; this property is for introspection (tests, UI).

        :returns: Mapping from label key to value.
        """
        return dict(self._labels)

    @property
    def session_state(self) -> dict[str, Any]:
        """
        Read-only snapshot of the hot session-state cache.

        Returns a defensive copy. Policies read the state
        through ``event["session_state"]`` during dispatch;
        this property is for introspection and tests.

        :returns: Current key/value session state.
        """
        return dict(self._session_state)

    @property
    def usage(self) -> dict[str, float]:
        """
        Read-only snapshot of the cumulative LLM token usage.

        Returns a defensive copy so callers cannot corrupt
        engine state. Policies read usage through
        ``event["context"]["usage"]`` during dispatch; this
        property is for introspection and tests.

        :returns: Mapping with keys ``input_tokens``,
            ``output_tokens``, ``total_tokens``,
            ``cache_read_input_tokens``,
            ``cache_creation_input_tokens``, and
            ``total_cost_usd``.
        """
        return dict(self._usage)

    @property
    def model(self) -> str | None:
        """
        Read-only snapshot of the session's resolved active model.

        Policies read the model through ``event["context"]["model"]``
        during dispatch; this property is for introspection and tests.

        :returns: The model id resolved at build time (``model_override``
            or the spec ``llm.model``), or ``None`` when undeterminable.
        """
        return self._model

    @property
    def conversation_id(self) -> str:
        """:returns: The conversation this engine owns."""
        return self._conversation_id

    async def evaluate(
        self,
        ctx: EvaluationContext,
        *,
        read_only: bool = False,
    ) -> PolicyResult:
        """
        Evaluate the composed policy decision, recording a span.

        Thin tracing wrapper over :meth:`_evaluate_composed`. Opens a
        ``policy.evaluate`` span tagged with the phase, resolved tool
        name, and read-only flag, then records the resulting decision
        (and reason / deciding policies on a non-ALLOW). This is the one
        in-process policy choke point — every enforcement site routes
        through it — so the span makes policy decisions visible inline
        in the request trace.

        :param ctx: The current evaluation context
            (phase + content + resolved tool_name).
        :param read_only: When ``True``, skip persistent side effects;
            see :meth:`_evaluate_composed`.
        :returns: The composed :class:`PolicyResult`.
        """
        from omnigent.runtime import telemetry

        with telemetry.span(
            "policy.evaluate",
            attributes={
                "policy.phase": getattr(ctx.phase, "name", str(ctx.phase)),
                "policy.tool_name": ctx.tool_name or "",
                "policy.read_only": read_only,
            },
        ) as evaluate_span:
            if self._conversation_id:
                evaluate_span.set_attribute("session.id", self._conversation_id)
            # The content under evaluation (tool args / prompt) is recorded
            # only when content capture is on (redacted + capped).
            telemetry.record_message_payload(
                {"content": ctx.content},
                span=evaluate_span,
                key="policy.content",
            )
            result = await self._evaluate_composed(ctx, read_only=read_only)
            evaluate_span.set_attribute(
                "policy.decision",
                getattr(result.action, "name", str(result.action)),
            )
            if result.reason:
                evaluate_span.set_attribute("policy.reason", result.reason)
            deciding = getattr(result, "deciding_policies", None)
            if deciding:
                evaluate_span.set_attribute("policy.deciding_policies", list(deciding))
            return result

    async def _evaluate_composed(
        self,
        ctx: EvaluationContext,
        *,
        read_only: bool = False,
    ) -> PolicyResult:
        """
        Evaluate the composed policy decision for one phase.

        Runs the pipeline from POLICIES.md §4:

        1. For each policy in YAML order:
           a. Skip if no :class:`PhaseSelector` matches.
           b. Skip if the policy's ``condition`` label-gate
              does not match the current hot-cache snapshot.
           c. Dispatch to ``policy.evaluate``.
           d. Accumulate any policy result.
           e. Accumulate ``set_labels`` writes.
           f. If the policy returned ``data``, feed it back
              as ``ctx.content`` so the next policy transforms
              the already-transformed payload (sequential
              chaining across the pipeline).
        2. On DENY: short-circuit. Apply accumulated writes
           from any ALLOWing predecessors, then return the
           DENY result (with ``deciding_policy`` set).
        3. After the loop, if any policy ASKed: return an ASK
           result carrying accumulated (but unapplied)
           writes and the full ``deciding_policies`` list —
           the caller applies them only on approve
           (POLICIES.md §7.2).
        4. Otherwise: apply writes, return ALLOW.

        :param ctx: The current evaluation context
            (phase + content + resolved tool_name).
        :param read_only: When ``True``, skip all persistent
            side effects (label writes and state updates) on
            both ALLOW and DENY paths. The returned
            :class:`PolicyResult` still carries ``set_labels``
            and ``state_updates`` so the caller can see what
            *would* have been written, but nothing is persisted
            to the store or the engine's hot cache. Used by the
            ``POST /sessions/{id}/policies/evaluate`` route for
            callers with read-only access, e.g. ``LEVEL_READ``
            collaborators.
        :returns: The composed :class:`PolicyResult`. Single-
            policy results are always wrapped into a composed
            result here — callers receive ALLOW / ASK / DENY
            directly.
        """
        accumulated: dict[str, str] = {}
        accumulated_state: list[StateUpdate] = []
        ask_reasons: list[str] = []
        deciding_ask_policies: list[str] = []
        # Sequentially accumulated data: each policy that returns data
        # has its output fed back into ctx.content so the next policy
        # in the chain transforms the already-transformed payload rather
        # than the original. The final value is the fully-composed result.
        composed_data: Any = None
        context = self._context()

        # Populate trajectory and session_state once per evaluate so
        # PromptPolicy classifiers see situational context and function
        # policies can read accumulated state. Both queries are bounded
        # to avoid scanning large conversations. See §4.1.
        ctx = self._populate_trajectory(ctx)
        ctx = self._inject_session_state(ctx)
        ctx = self._inject_usage(ctx)
        ctx = self._inject_subtree_usage(ctx)
        ctx = self._inject_user_daily_cost(ctx)
        ctx = self._inject_model(ctx)
        ctx = self._inject_labels(ctx)
        ctx = self._inject_llm_client(ctx)

        for policy in self.policies:
            if not self._should_fire(policy.spec, ctx):
                continue
            result = await _dispatch_policy(policy, ctx, context)
            # Filter set_labels through the spec's whitelist
            # (when declared) — keys outside the whitelist
            # silently dropped per POLICIES.md §9.
            filtered_labels = _filter_writable_labels(result.set_labels, policy.spec)
            if filtered_labels:
                accumulated.update(filtered_labels)
            if result.state_updates:
                accumulated_state.extend(result.state_updates)
            if result.action == PolicyAction.DENY:
                return self._compose_deny(
                    policy.spec.name,
                    result.reason,
                    accumulated,
                    accumulated_state,
                    read_only=read_only,
                )
            if result.data is not None:
                composed_data = result.data
                # Feed the transformed payload forward so the next policy
                # in the chain sees this policy's output, not the original.
                ctx = replace(ctx, content=composed_data)
            if result.action == PolicyAction.ASK:
                ask_reasons.append(
                    f"{policy.spec.name}: {result.reason or 'approval required'}",
                )
                deciding_ask_policies.append(policy.spec.name)

        if ask_reasons:
            # DO NOT apply label writes or state updates here — the ASK
            # verdict is pending user approval. Both are withheld and
            # carried in the result so the caller can apply them only on
            # approve (POLICIES.md §7.2). On deny/timeout they are dropped,
            # preserving the "no side effects from a denied ASK" invariant.
            return PolicyResult(
                action=PolicyAction.ASK,
                reason="; ".join(ask_reasons),
                set_labels=dict(accumulated) if accumulated else None,
                state_updates=list(accumulated_state) if accumulated_state else None,
                deciding_policies=deciding_ask_policies,
                data=composed_data,
            )
        if not read_only:
            self.apply_label_writes(accumulated)
            self.apply_state_updates(accumulated_state)
        return PolicyResult(
            action=PolicyAction.ALLOW,
            reason=None,
            set_labels=dict(accumulated) if accumulated else None,
            state_updates=list(accumulated_state) if accumulated_state else None,
            data=composed_data,
        )

    def _compose_deny(
        self,
        deciding_policy: str,
        reason: str | None,
        accumulated: dict[str, str],
        accumulated_state: list[StateUpdate],
        *,
        read_only: bool = False,
    ) -> PolicyResult:
        """
        Build the DENY short-circuit result.

        Applies accumulated label writes and session-state
        updates from earlier ALLOWing policies (plus the
        DENYing policy's own writes) before returning — per
        POLICIES.md §4. Extracted from ``evaluate`` to keep
        that method under the 40-line limit.

        :param deciding_policy: Name of the policy whose DENY
            short-circuited the chain.
        :param reason: Reason carried on the DENYing result.
        :param accumulated: Label writes gathered across
            every policy up to and including the DENYing one.
        :param accumulated_state: :class:`StateUpdate` operations
            gathered across every policy up to and including
            the DENYing one.
        :param read_only: When ``True``, skip persistent side
            effects (label writes and state updates). The
            returned result still carries ``set_labels`` so the
            caller can see what *would* have been written.
        :returns: Composed DENY :class:`PolicyResult`.
        """
        if not read_only:
            self.apply_state_updates(accumulated_state)
            self.apply_label_writes(accumulated)
        return PolicyResult(
            action=PolicyAction.DENY,
            reason=reason,
            set_labels=dict(accumulated) if accumulated else None,
            state_updates=list(accumulated_state) if accumulated_state else None,
            deciding_policies=[deciding_policy],
        )

    def _should_fire(
        self,
        spec: PolicySpec,
        ctx: EvaluationContext,
    ) -> bool:
        """
        Check whether a policy's selector + condition gates
        pass for the current context.

        Two stages, short-circuited in order per §4 key
        semantics:

        1. :class:`PhaseSelector` match — cheap, no label
           reads.
        2. ``condition`` label-gate — AND across keys; list
           values = OR within the key.

        :param spec: The policy's spec.
        :param ctx: The current evaluation context.
        :returns: ``True`` when the engine should dispatch to
            ``policy.evaluate``; ``False`` when the policy is
            skipped entirely for this context.
        """
        # on=None means the callable self-selects (type: function policies).
        if spec.on is not None and not any(sel.matches(ctx) for sel in spec.on):
            return False
        if spec.condition is not None and not _condition_matches(
            spec.condition,
            self._labels,
        ):
            return False
        return True

    def apply_label_writes(self, set_labels: dict[str, str]) -> None:
        """
        Validate and persist label writes.

        Per POLICIES.md §10, writes are silently dropped when:

        - The key has a declared ``LabelDef.values`` list and
          the new value is not in it.

        Keys with no ``LabelDef`` are set freely (omnigent
        parity — "unschema'd labels set freely"). The engine
        applies the filtered dict in a single UPSERT through
        the store so either every surviving write lands or
        none do.

        :param set_labels: Mapping of label key to new value.
            No-op on empty dict. Writes update both the hot
            cache on this engine and the persistent row in
            ``conversation_labels`` in one UPSERT transaction.
        """
        if not set_labels:
            return
        filtered = self._filter_schema_valid(set_labels)
        if not filtered:
            return
        self._store.set_labels(self._conversation_id, filtered)
        self._labels.update(filtered)

    def apply_state_updates(self, updates: list[StateUpdate]) -> None:
        """
        Apply structured :class:`StateUpdate` operations to the
        hot session-state cache and persist the result.

        Each operation is applied in list order:

        - ``SET``: ``state[key] = value``
        - ``INCREMENT``: ``state[key] = state.get(key, 0) + value``
        - ``DELETE``: ``del state[key]`` (no-op if absent)
        - ``APPEND``: append ``value`` to the list at ``key``
          (creates a new single-element list if absent)

        After applying all operations, the resulting snapshot is
        persisted to the conversation store so session state
        survives across turns.

        :param updates: Ordered list of :class:`StateUpdate`
            operations, e.g.
            ``[StateUpdate(key="call_count", action=StateUpdateAction.INCREMENT, value=1)]``.
            Empty list returns immediately.
        """
        if not updates:
            return
        from omnigent.policies.schema import (
            SESSION_COST_ASK_APPROVED_STATE_KEY,
            SESSION_COST_UNPRICED_APPROVED_KEY,
            USER_DAILY_ASK_APPROVED_STATE_KEY,
        )

        # Two reserved keys are routed off this conversation's session_state:
        # the per-user daily approval goes to the user+day store column (so it
        # persists across the user's sessions), and the per-SESSION cost
        # approval goes to the ROOT conversation (so approving once covers the
        # whole spawn tree — a sub-agent runs as its own conversation, and
        # build_policy_engine seeds the approval from the root). Every other
        # update lands in this conversation's session_state as usual.
        session_ops = []
        for op in updates:
            if op.key == USER_DAILY_ASK_APPROVED_STATE_KEY:
                self._record_user_daily_ask_approved(op.value)
            elif (
                op.key in (SESSION_COST_ASK_APPROVED_STATE_KEY, SESSION_COST_UNPRICED_APPROVED_KEY)
                and self._root_conversation_id != self._conversation_id
            ):
                self._record_root_cost_ask_approved(op)
            else:
                session_ops.append(op)
        if session_ops:
            for op in session_ops:
                _apply_one(self._session_state, op)
            self._store.set_session_state(self._conversation_id, self._session_state)

    def _record_root_cost_ask_approved(self, op: StateUpdate) -> None:
        """
        Persist a per-session cost-budget ASK approval to the ROOT conversation.

        The cost budget spans the whole spawn tree, but a sub-agent runs as its
        own conversation, so its approval must land on the root — where
        :func:`build_policy_engine` seeds the checkpoint from — otherwise the
        parent and sibling sub-agents would re-prompt at the same threshold.
        Only reached when ``root_conversation_id != conversation_id`` (a
        sub-agent); a top-level session writes through the normal
        per-conversation path (root == self).

        :param op: The ``SET`` op carrying the approved checkpoint value, e.g.
            ``StateUpdate(key=..., action=StateUpdateAction.SET, value=0.05)``.
        """
        root_conv = self._store.get_conversation(self._root_conversation_id)
        root_state = dict(root_conv.session_state) if root_conv is not None else {}
        _apply_one(root_state, op)
        self._store.set_session_state(self._root_conversation_id, root_state)
        # Also mirror into this engine's hot in-memory state so a subsequent
        # evaluate() within the same sub-agent turn sees the approval (its
        # session_state was seeded from the root at construction, but a fresh
        # approval lands on the root store, not here) and doesn't re-ASK.
        _apply_one(self._session_state, op)

    def _record_user_daily_ask_approved(self, value: Any) -> None:
        """
        Persist a per-user daily cost-budget ASK approval.

        Writes the approved soft-checkpoint value to the session
        owner's ``user_daily_cost.ask_approved_usd`` for the current UTC
        day, so the same checkpoint won't re-prompt that user again
        today (including from other sessions). A no-op when the session
        has no owner grant (single-user mode) or *value* is not numeric.

        :param value: The crossed checkpoint value (USD) the user
            approved, e.g. ``0.05``.
        """
        if value is None:
            return
        try:
            approved = float(value)
        except (TypeError, ValueError):
            return
        owner = self._store.get_session_owner(self._conversation_id)
        if owner is None:
            return
        from omnigent.db.utils import now_epoch, utc_day

        self._store.set_daily_ask_approved(owner, utc_day(now_epoch()), approved)
        # Keep the in-memory snapshot current so any later evaluate() on
        # this engine sees the approval and doesn't re-ASK the checkpoint
        # the user just approved — mirroring how the session policy's
        # approval stays current via _apply_one(self._session_state, ...).
        if self._user_daily_cost is not None:
            self._user_daily_cost["ask_approved_usd"] = approved

    def record_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        """
        Add token counts to the cumulative usage counters.

        Called by the workflow after each LLM call. The counts are
        additive — each call increments the running totals. After
        updating the in-memory counters, persists the new totals
        to the conversation's ``session_usage`` column so they
        survive across turns.

        Cost is computed via :func:`compute_llm_cost` when
        :attr:`_token_pricing` is set, pricing cache-read and
        cache-write tokens at their own (typically cheaper/pricier)
        rates.

        :param input_tokens: Number of non-cached input tokens
            consumed in this LLM call, e.g. ``1500``.
        :param output_tokens: Number of output tokens produced in
            this LLM call, e.g. ``350``.
        :param total_tokens: Total tokens for this LLM call
            (typically ``input_tokens + output_tokens``), e.g.
            ``1850``.
        :param cache_read_input_tokens: Number of cache-read
            (cache-hit) input tokens in this LLM call, e.g.
            ``5000``. Defaults to ``0`` for providers that don't
            report cache breakdowns.
        :param cache_creation_input_tokens: Number of cache-write
            (cache-creation) input tokens in this LLM call, e.g.
            ``2000``. Defaults to ``0`` for providers that don't
            report cache breakdowns.
        """
        self._usage["input_tokens"] += input_tokens
        self._usage["output_tokens"] += output_tokens
        self._usage["total_tokens"] += total_tokens
        self._usage["cache_read_input_tokens"] += cache_read_input_tokens
        self._usage["cache_creation_input_tokens"] += cache_creation_input_tokens
        if self._token_pricing is not None:
            delta_usage = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
            }
            delta_cost = compute_llm_cost(delta_usage, self._token_pricing)
            self._usage["total_cost_usd"] += delta_cost
        else:
            delta_cost = 0.0
        if self._subtree_usage is not None:
            self._subtree_usage["input_tokens"] += input_tokens
            self._subtree_usage["output_tokens"] += output_tokens
            self._subtree_usage["total_tokens"] += total_tokens
            self._subtree_usage["cache_read_input_tokens"] += cache_read_input_tokens
            self._subtree_usage["cache_creation_input_tokens"] += cache_creation_input_tokens
            self._subtree_usage["total_cost_usd"] += delta_cost
        self._store.set_session_usage(self._conversation_id, dict(self._usage))

    def _inject_usage(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of *ctx* with ``usage`` populated.

        Injects the engine's current cumulative token counts so
        function policy callables can read usage via
        ``event["context"]["usage"]`` without re-querying the
        store.

        :param ctx: Original :class:`EvaluationContext` from the
            caller.
        :returns: A new :class:`EvaluationContext` with ``usage``
            set to a defensive copy of the cumulative counters.
        """
        return replace(ctx, usage=dict(self._usage))

    def _inject_subtree_usage(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of *ctx* with ``subtree_usage`` populated, when seeded.

        Injects the engine's subtree-scoped cumulative cost so the
        ``subagent_cost_budget`` policy can gate on the child's own
        subtree spend rather than the whole session total. When the
        engine was built without it (``None`` — no policy needs it),
        *ctx* is returned unchanged.

        :param ctx: Original :class:`EvaluationContext` from the
            caller.
        :returns: *ctx* unchanged when no subtree usage was seeded,
            else a copy with ``subtree_usage`` set to a defensive copy.
        """
        if self._subtree_usage is None:
            return ctx
        return replace(ctx, subtree_usage=dict(self._subtree_usage))

    def _inject_user_daily_cost(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of *ctx* with ``user_daily_cost`` populated, when seeded.

        Injects the session owner's per-UTC-day cost rollup (read once at
        engine-build time) so the per-user daily cost-budget policy can
        read it via ``event["context"]["user_daily_cost"]`` without
        re-querying the store. When the engine was built without it
        (``None`` — no policy needs it), *ctx* is returned unchanged so
        sessions that don't use the daily policy never carry it.

        :param ctx: Original :class:`EvaluationContext` from the caller.
        :returns: *ctx* unchanged when no daily-cost was seeded, else a
            copy with ``user_daily_cost`` set to a defensive copy.
        """
        if self._user_daily_cost is None:
            return ctx
        return replace(ctx, user_daily_cost=dict(self._user_daily_cost))

    def _inject_model(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of *ctx* with ``model`` populated.

        When the caller already supplied a model on *ctx* (a native hook
        that read the harness's live source of truth — e.g. the codex hook
        reading ``config.toml`` at gate time), that wins: it reflects the
        user's current ``/model`` selection without the lag/race of the
        async ``model_override`` mirror. Otherwise injects the session's
        model resolved at engine-build time (from ``model_override`` or the
        spec ``llm.model``) so function policy callables can read it via
        ``event["context"]["model"]`` without re-querying the store.

        :param ctx: Original :class:`EvaluationContext` from the
            caller.
        :returns: *ctx* unchanged when it already carries a model, else a
            copy with ``model`` set to the engine's resolved model
            (``None`` when undeterminable).
        """
        if ctx.model is not None:
            return ctx
        return replace(ctx, model=self._model)

    def _inject_labels(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of *ctx* with ``labels`` populated.

        Injects the engine's label hot cache — the same snapshot
        ``condition:`` gates read — so function policy callables can
        gate on persisted label state via
        ``event["context"]["labels"]`` (e.g. the advisor cost-plan
        guard reading ``cost_control.plan``).

        :param ctx: Original :class:`EvaluationContext` from the
            caller.
        :returns: A new :class:`EvaluationContext` with ``labels`` set
            to a defensive copy of the hot cache.
        """
        return replace(ctx, labels=dict(self._labels))

    def _inject_llm_client(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of *ctx* with ``llm_client`` populated.

        Injects the engine's server-level LLM client so function
        policy callables can make LLM calls via
        ``event["llm_client"]`` without constructing their own.

        :param ctx: Original :class:`EvaluationContext` from the
            caller.
        :returns: A new :class:`EvaluationContext` with
            ``llm_client`` set.
        """
        return replace(ctx, llm_client=self._llm_client)

    def _inject_session_state(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of *ctx* with ``session_state`` populated.

        Injects the engine's current hot cache so function policy
        callables can read accumulated state via
        ``event["session_state"]`` without re-querying the store.

        :param ctx: Original :class:`EvaluationContext` from the
            caller.
        :returns: A new :class:`EvaluationContext` with
            ``session_state`` set to a defensive copy of the
            hot cache.
        """
        return replace(ctx, session_state=dict(self._session_state))

    def _filter_schema_valid(
        self,
        set_labels: dict[str, str],
    ) -> dict[str, str]:
        """
        Drop writes that violate a declared :class:`LabelDef`.

        Called before persistence. Silent-drop semantics match
        POLICIES.md §10 / §13 — runtime label failures don't
        nuke the whole evaluation; they just fail to land.

        :param set_labels: Caller's requested writes.
        :returns: Subset of *set_labels* that pass every
            applicable schema check. Keys with no LabelDef
            pass through unchanged.
        """
        result: dict[str, str] = {}
        for key, value in set_labels.items():
            ldef = self.label_defs.get(key)
            if ldef is None:
                result[key] = value
                continue
            if ldef.values is not None and value not in ldef.values:
                continue
            result[key] = value
        return result

    def spec_for(self, policy_name: str | None) -> PolicySpec | None:
        """
        Look up a :class:`PolicySpec` by name.

        Used by ``_await_policy_approval`` (Phase 8) to resolve
        the per-policy ``ask_timeout`` override off the
        deciding policy's spec. ``None`` input returns ``None``
        to keep the caller's null-handling terse.

        :param policy_name: Name of the policy to look up,
            e.g. ``"block_canada_input"``. ``None`` returns
            ``None`` directly.
        :returns: The matching spec, or ``None`` when no policy
            with that name exists (or *policy_name* was
            ``None``).
        """
        if policy_name is None:
            return None
        for policy in self.policies:
            if policy.spec.name == policy_name:
                return policy.spec
        return None

    def reset_turn(self) -> None:
        """
        Reset per-turn state on every policy in this engine.

        Called once per "turn" (one user prompt → terminal
        assistant response cycle, i.e. once per
        ``_run_agent_loop`` invocation). Stateful policies
        with per-turn accumulators clear them here.
        Stateless policies — the default — no-op.

        Mirrors the omnigent-native semantics in
        :meth:`omnigent.runtime.policies.engine.PolicyEngine.reset_turn`.
        Without this hook, legacy ``max_tool_calls_per_turn``
        callables silently degrade to per-session limits under
        Omnigent mode.
        """
        for policy in self.policies:
            policy.reset_turn()

    def _context(self) -> dict[str, Any]:
        """
        Build the context bundle passed to each Policy.evaluate().

        Exposes a read-only snapshot of the hot label cache
        plus identity fields. Used by Phase 3+ when concrete
        Policy subclasses need to inspect labels for
        condition evaluation. Phase 2 never calls this because
        ``evaluate`` returns early; it's defined here so the
        API is stable across phases.

        :returns: Context dict with keys ``labels``
            (defensive copy) and ``conversation_id``.
        """
        return {
            "labels": dict(self._labels),
            "conversation_id": self._conversation_id,
            "session_state": dict(self._session_state),
        }

    def _populate_trajectory(self, ctx: EvaluationContext) -> EvaluationContext:
        """
        Return a copy of ``ctx`` with ``trajectory`` populated.

        Queries the conversation store for the last
        ``_TRAJECTORY_WINDOW`` items in chronological order. If
        the conversation has fewer than the window size, returns
        whatever exists (down to an empty list for brand-new
        conversations). The store lookup is order=``"desc"`` +
        slice + reverse so the engine asks for the *tail* without
        first scanning the entire conversation.

        :param ctx: Original :class:`EvaluationContext` from the
            caller. ``ctx.trajectory`` is overwritten.
        :returns: A new :class:`EvaluationContext` with
            ``trajectory`` set to the recent items list (oldest
            first).
        """
        page = self._store.list_items(
            self._conversation_id,
            limit=_TRAJECTORY_WINDOW,
            order="desc",
        )
        # ``order="desc"`` returns most-recent first; reverse so the
        # classifier sees items chronologically (oldest first), which
        # matches how a human reads a conversation top-down.
        trajectory = list(reversed(page.data))
        return replace(ctx, trajectory=trajectory)


def _apply_one(state: dict[str, Any], op: StateUpdate) -> None:
    """
    Apply a single :class:`StateUpdate` operation to *state*
    in place.

    :param state: The mutable session-state dict.
    :param op: The operation to apply.
    :raises TypeError: If ``INCREMENT`` is used on a non-numeric
        existing value or with a non-numeric delta.
    :raises TypeError: If ``APPEND`` targets a key whose current
        value is not a list.
    """
    if op.action == StateUpdateAction.SET:
        state[op.key] = op.value
    elif op.action == StateUpdateAction.INCREMENT:
        current = state.get(op.key, 0)
        state[op.key] = current + op.value
    elif op.action == StateUpdateAction.DELETE:
        state.pop(op.key, None)
    elif op.action == StateUpdateAction.APPEND:
        existing = state.get(op.key)
        if existing is None:
            state[op.key] = [op.value]
        else:
            if not isinstance(existing, list):
                raise TypeError(
                    f"APPEND on key {op.key!r}: expected list, got {type(existing).__name__}",
                )
            existing.append(op.value)


async def _dispatch_policy(
    policy: Policy,
    ctx: EvaluationContext,
    context: dict[str, Any],
) -> PolicyResult:
    """
    Run a single policy's ``evaluate`` with full safety net.

    Applies the POLICIES.md §4 contract:

    - Any exception raised by the policy is converted to a
      fail-closed DENY result.
    - ``set_labels`` from a failing evaluation are dropped;
      a partial/broken policy does not get to write labels.

    :param policy: The concrete :class:`Policy` instance.
    :param ctx: Current evaluation context.
    :param context: Engine-provided context bundle.
    :returns: A normalized result — safe for the engine to
        compose without further validation.
    """
    try:
        result = await policy.evaluate(ctx, context)
    except Exception as exc:
        return PolicyResult(
            action=PolicyAction.DENY,
            reason=f"policy {policy.spec.name!r} failed: {exc}",
            set_labels=None,
        )
    return result


def _filter_writable_labels(
    set_labels: dict[str, str] | None,
    spec: PolicySpec,
) -> dict[str, str] | None:
    """
    Filter a policy's label writes through its whitelist.

    When ``spec.set_labels`` is a list (FunctionPolicy /
    PromptPolicy), any key in the returned dict that is NOT
    in the list is silently dropped. When ``spec.set_labels``
    is absent or not a list, every key passes through.

    :param set_labels: The label writes the policy returned.
    :param spec: The policy's spec.
    :returns: Filtered mapping, or ``None`` / empty if all
        writes were dropped.
    """
    if not set_labels:
        return None
    whitelist = getattr(spec, "set_labels", None)
    if not isinstance(whitelist, list):
        return dict(set_labels)
    return {k: v for k, v in set_labels.items() if k in whitelist}


def _condition_matches(
    condition: dict[str, str | list[str]],
    labels: dict[str, str],
) -> bool:
    """
    Evaluate a policy's ``condition:`` block against the
    current label snapshot.

    Semantics (POLICIES.md §4, §10):

    - AND across keys: every key in *condition* must match
      for the policy to fire.
    - Within a key, a scalar value is an equality check; a
      list is an OR — the stored value must appear in the
      list.
    - A key present in *condition* but absent from *labels*
      never matches (the policy did not set that label, so
      the gate stays closed).

    :param condition: Declarative condition from the spec.
        Values are already string-coerced at spec load.
    :param labels: Current hot-cache snapshot.
    :returns: ``True`` if every key's check passes.
    """
    for key, expected in condition.items():
        actual = labels.get(key)
        if actual is None:
            return False
        if isinstance(expected, list):
            if actual not in expected:
                return False
        else:
            if actual != expected:
                return False
    return True


# Re-export the defaults for callers that need them without
# importing from spec.types directly.
__all__ = ["DEFAULT_ASK_TIMEOUT", "PolicyEngine"]
