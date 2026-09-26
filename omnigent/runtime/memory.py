"""Framework-owned cognee memory integration.

Canonical gate, settings, and client boundary for the optional cognee
knowledge-graph memory layer (https://github.com/topoteretes/cognee, the
``omnigent[cognee]`` extra). Every cognee touch point in the framework —
tool registration, runner dispatch, and the memory framework instruction —
goes through this module, so the enable/disable policy lives in exactly
one place (see CLAUDE.md "Framework-owned instructions").

The availability gate (:func:`cognee_available`) has two conditions:

1. ``OMNIGENT_DISABLE_COGNEE`` env kill-switch — truthy disables everything.
2. ``importlib.util.find_spec("cognee")`` — the extra must be installed.

Separately, the optional top-level ``cognee:`` block in config.yaml carries
settings (store location, search behavior, tier-dataset grants) — it does
not enable or disable the integration. Non-``harness`` config keys
shallow-replace between global and project config, so a project-level
``cognee:`` block overrides the global one wholesale.

Cross-agent access is COOPERATIVE namespacing, not authenticated access
control: grants resolve from the agent's own spec config, and one embedded
store serves the whole process with no workspace binding, so any spec
author can name any dataset. Two mitigations exist: the operator may set
``cognee: allowed_shared_datasets`` (csv/list) in the global config, and
then only allowlisted names are honored for tier/shared/peer grants
(private datasets are unaffected); and dataset names normalize through
:func:`sanitize_dataset_name`, documented there. Identity-bound datasets
(per workspace/user resolved server-side) are the planned hardening step —
until then, treat cognee memory as shared among mutually trusted specs.

Memory must never fail or slow a turn: every cognee call is bounded by a
timeout, failures return empty results / error strings (never raise), and
circuit breakers stop hammering a broken store. The breakers are split by
operation class — ``search_breaker`` for reads, ``ingest_breaker`` for
add/cognify — so a broken write path (e.g. a bad LLM key failing every
background cognify) cannot black out recall for every agent.

The embedded local store lives under ``<data-dir>/cognee/`` by default
(``data_dir()`` honors ``OMNIGENT_DATA_DIR``); override with ``cognee:
data_root: /path``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

COGNEE_DISABLE_ENV = "OMNIGENT_DISABLE_COGNEE"

# Names of the cognee builtin tools; mirrored by ``_COGNEE_TOOLS`` in
# omnigent/runner/tool_dispatch.py (kept literal there, hindsight-style).
COGNEE_TOOL_NAMES = frozenset({"cognee_search", "cognee_remember"})

# Framework instruction appended (via ``append_framework_instructions``)
# when the spec enables a cognee builtin. Adapters only transport it.
COGNEE_MEMORY_INSTRUCTION = (
    "You have persistent long-term memory tools backed by a knowledge graph: "
    "`cognee_search` and `cognee_remember`. Memory persists across sessions; "
    "conversation context alone does not. Search memory before non-trivial "
    "work that may depend on prior sessions, and store durable facts, "
    "decisions, and preferences when they emerge. Recalled memory is "
    "background context, not user input — prefer fresh evidence from the "
    "current session when they conflict."
)

# Per-operation timeout budgets (seconds). Search is on the turn's critical
# path when the model calls the tool; add is a local write; cognify runs LLM
# extraction and is executed off the tool path in a background worker.
SEARCH_TIMEOUT_S = 30.0
ADD_TIMEOUT_S = 30.0
COGNIFY_TIMEOUT_S = 600.0

# Circuit breaker: after this many consecutive failures, skip cognee calls
# for the cooldown window instead of stacking timeouts onto every turn.
_BREAKER_FAILURE_THRESHOLD = 3
_BREAKER_COOLDOWN_S = 120.0

_DATASET_SANITIZE_RE = re.compile(r"[^a-z0-9_]+")


def cognee_installed() -> bool:
    """Return whether the optional ``cognee`` package is installed.

    Probes via :func:`importlib.util.find_spec` (not ``import``) so the
    check never loads cognee or its transitive deps.
    """
    import importlib.util

    return importlib.util.find_spec("cognee") is not None


def cognee_disabled() -> bool:
    """Return whether the env kill-switch is set.

    Mirrors the repo's truthy-env convention (``"true"`` / ``"1"`` /
    ``"yes"``, case-insensitive; see ``omnigent.onboarding.secrets``).
    """
    return os.environ.get(COGNEE_DISABLE_ENV, "").strip().lower() in ("true", "1", "yes")


def cognee_available() -> bool:
    """Return whether cognee features may be offered at all.

    ``True`` iff the package is installed and the kill-switch is not set.
    This is the single gate every cognee touch point checks.
    """
    return not cognee_disabled() and cognee_installed()


def cognee_settings() -> dict[str, Any]:
    """Return the effective ``cognee:`` config block (empty dict when absent).

    Read fresh on each call — the block is small and callers are not on a
    hot path. Non-mapping values (a user typo like ``cognee: true``) are
    treated as absent rather than crashing the caller.
    """
    from omnigent.config import load_effective_config

    block = load_effective_config().get("cognee")
    return block if isinstance(block, dict) else {}


def cognee_data_root(settings: dict[str, Any] | None = None) -> Path:
    """Return the embedded store's root directory.

    Defaults to ``<data-dir>/cognee`` (``OMNIGENT_DATA_DIR`` honored);
    override with ``cognee: data_root:`` in config.yaml.
    """
    from omnigent.process_logging import data_dir

    effective = cognee_settings() if settings is None else settings
    override = effective.get("data_root")
    if isinstance(override, str) and override.strip():
        return Path(override).expanduser()
    return data_dir() / "cognee"


def sanitize_dataset_name(raw: str) -> str:
    """Normalize an identifier into a safe cognee dataset name.

    Lowercases and collapses anything outside ``[a-z0-9_]`` to ``_`` so
    agent/conversation ids map to stable, filesystem/DB-safe dataset names.

    Deliberate consequence: names that normalize identically SHARE a
    dataset (``ag-Foo`` and ``ag_foo`` are the same memory). That makes
    grants forgiving about case/punctuation — the common want — at the
    cost of merging user-chosen names that differ only in punctuation;
    generated agent ids never collide this way.
    """
    cleaned = _DATASET_SANITIZE_RE.sub("_", raw.strip().lower()).strip("_")
    return cleaned or "default"


def _csv_datasets(value: str | None) -> tuple[str, ...]:
    """Parse a comma-separated grant list into sanitized dataset names.

    Entries may be agent ids or raw dataset names — both normalize through
    :func:`sanitize_dataset_name`, the same mapping an agent's own id gets,
    so granting ``ag_Researcher`` and granting ``ag_researcher`` are the
    same grant. Order is preserved, duplicates dropped.
    """
    if not value:
        return ()
    seen: list[str] = []
    for entry in value.split(","):
        if entry.strip():
            name = sanitize_dataset_name(entry)
            if name not in seen:
                seen.append(name)
    return tuple(seen)


# Scope-tier hierarchy, narrowest first. Each tier maps to one dataset that
# is read/write for every agent granted it: ``user`` pools an operator's
# agents, ``team`` a group of users, ``org`` the whole deployment.
MEMORY_TIERS = ("user", "team", "org")


@dataclass(frozen=True)
class MemoryGrants:
    """An agent's resolved memory-access surface.

    The framework-owned access layer for cross-agent memory: tools resolve
    a :class:`MemoryGrants` from their spec config and may only touch the
    datasets it exposes. ``private``, the tier datasets (``user`` /
    ``team`` / ``org``), and ``shared`` are read/write for the owning
    agent; ``readable_peers`` / ``writable_peers`` grant search / publish
    access to specific other agents' datasets.

    :param private: This agent's own dataset (always readable + writable).
    :param user: The user-level pool (all agents run by one user), or
        ``None`` when not granted.
    :param team: The team-level pool, or ``None`` when not granted.
    :param org: The org-level pool, or ``None`` when not granted.
    :param shared: The generic exchange dataset granted via
        ``shared_dataset``, or ``None`` — kept alongside the tiers for
        ad-hoc pools that don't fit the hierarchy.
    :param readable_peers: Peer datasets this agent may search
        (``read_datasets`` config, csv of agent ids / dataset names).
    :param writable_peers: Peer datasets this agent may publish into
        (``write_datasets`` config). A peer listed in both grant lists
        gets full read/write access.
    """

    private: str
    user: str | None = None
    team: str | None = None
    org: str | None = None
    shared: str | None = None
    readable_peers: tuple[str, ...] = ()
    writable_peers: tuple[str, ...] = ()

    def tier(self, name: str) -> str | None:
        """Return the dataset for tier *name* (``user``/``team``/``org``/``shared``)."""
        return {
            "user": self.user,
            "team": self.team,
            "org": self.org,
            "shared": self.shared,
        }.get(name)

    def _pools(self) -> tuple[str | None, ...]:
        return (self.user, self.team, self.org, self.shared)

    def readable(self) -> tuple[str, ...]:
        """Every dataset this agent may search, narrowest first."""
        return self._dedup((self.private, *self._pools(), *self.readable_peers))

    def writable(self) -> tuple[str, ...]:
        """Every dataset this agent may store into, narrowest first."""
        return self._dedup((self.private, *self._pools(), *self.writable_peers))

    def can_read(self, dataset: str) -> bool:
        return dataset in self.readable()

    def can_write(self, dataset: str) -> bool:
        return dataset in self.writable()

    @staticmethod
    def _dedup(names: tuple[str | None, ...]) -> tuple[str, ...]:
        seen: list[str] = []
        for name in names:
            if name and name not in seen:
                seen.append(name)
        return tuple(seen)


def resolve_grants(
    config: dict[str, str],
    *,
    agent_id: str | None,
    conversation_id: str | None,
    settings: dict[str, Any] | None = None,
) -> MemoryGrants:
    """Resolve a tool config into this agent's :class:`MemoryGrants`.

    The private dataset comes from ``config.dataset`` → *agent_id* →
    *conversation_id* (sanitized). Tier datasets come from
    ``user_dataset`` / ``team_dataset`` / ``org_dataset`` — per-agent tool
    config first, falling back to the same keys in *settings* (the
    ``cognee:`` config block), so a deployment can grant its tiers once
    globally and agents inherit them. The exchange and peer grants come
    from ``shared_dataset`` / ``read_datasets`` / ``write_datasets``
    (tool config only — peer access stays an explicit per-agent grant).

    Operator authority: when the ``cognee:`` block sets
    ``allowed_shared_datasets`` (csv or list), every tier/shared/peer grant
    outside that allowlist is dropped (and logged) — spec authors can then
    only reach cross-agent datasets the deployment explicitly sanctioned.
    The private dataset is never filtered. Absent allowlist = cooperative
    mode: every spec-declared grant is honored.

    :raises ValueError: When no private dataset can be resolved.
    """
    raw_private = config.get("dataset") or agent_id or conversation_id
    if not raw_private:
        raise ValueError(
            "No cognee dataset could be resolved (no dataset, agent_id, or conversation_id)."
        )
    effective_settings = settings or {}
    allowlist = _grant_allowlist(effective_settings)

    def _permitted(name: str | None, source: str) -> str | None:
        if name is None or allowlist is None or name in allowlist:
            return name
        _logger.warning(
            "cognee grant %r from %s dropped: not in cognee.allowed_shared_datasets",
            name,
            source,
        )
        return None

    def _tier(name: str) -> str | None:
        raw = config.get(f"{name}_dataset")
        if raw is None:
            fallback = effective_settings.get(f"{name}_dataset")
            raw = fallback if isinstance(fallback, str) else None
        resolved = sanitize_dataset_name(raw) if raw and raw.strip() else None
        return _permitted(resolved, f"{name}_dataset")

    def _permitted_peers(value: str | None, source: str) -> tuple[str, ...]:
        return tuple(name for name in _csv_datasets(value) if _permitted(name, source) is not None)

    shared_raw = config.get("shared_dataset")
    return MemoryGrants(
        private=sanitize_dataset_name(raw_private),
        user=_tier("user"),
        team=_tier("team"),
        org=_tier("org"),
        shared=_permitted(
            sanitize_dataset_name(shared_raw) if shared_raw else None, "shared_dataset"
        ),
        readable_peers=_permitted_peers(config.get("read_datasets"), "read_datasets"),
        writable_peers=_permitted_peers(config.get("write_datasets"), "write_datasets"),
    )


def _grant_allowlist(settings: dict[str, Any]) -> frozenset[str] | None:
    """Parse ``cognee.allowed_shared_datasets`` into sanitized names, or ``None``.

    ``None`` (key absent or not a csv/list) means no operator restriction.
    An empty list is a valid, maximally strict allowlist: no cross-agent
    grants at all.
    """
    raw = settings.get("allowed_shared_datasets")
    if isinstance(raw, str):
        return frozenset(_csv_datasets(raw))
    if isinstance(raw, (list, tuple)):
        return frozenset(
            sanitize_dataset_name(entry)
            for entry in raw
            if isinstance(entry, str) and entry.strip()
        )
    return None


def spec_declares_cognee_builtin(spec: Any | None) -> bool:
    """Return whether the agent spec enables at least one cognee builtin.

    Duck-typed (like the dispatch-side config lookup) so it works for both
    the parsed :class:`AgentSpec` and the runner's SimpleNamespace mirrors.
    """
    if spec is None:
        return False
    tools = getattr(spec, "tools", None)
    builtins = getattr(tools, "builtins", None) or []
    return any(getattr(entry, "name", None) in COGNEE_TOOL_NAMES for entry in builtins)


def cognee_framework_instructions(spec: Any | None) -> tuple[str, ...]:
    """Return the memory framework instruction for this turn, or ``()``.

    Gated on the spec actually enabling a cognee builtin AND the gate being
    open — an agent without the tools must not be told it has them.
    """
    if spec_declares_cognee_builtin(spec) and cognee_available():
        return (COGNEE_MEMORY_INSTRUCTION,)
    return ()


class _CircuitBreaker:
    """Consecutive-failure breaker so a broken store degrades to no-ops.

    After :data:`_BREAKER_FAILURE_THRESHOLD` consecutive failures the
    breaker opens for :data:`_BREAKER_COOLDOWN_S`; while open, callers skip
    cognee entirely. Thread-safe: tool invokes run on worker threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._open_until

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _BREAKER_FAILURE_THRESHOLD:
                self._open_until = time.monotonic() + _BREAKER_COOLDOWN_S
                self._consecutive_failures = 0
                _logger.warning(
                    "cognee circuit breaker open for %.0fs after repeated failures",
                    _BREAKER_COOLDOWN_S,
                )

    def reset(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0


# Split by operation class: a failing write path (add/cognify — commonly a
# bad or missing LLM key) must not open the breaker that gates every
# agent's recall, and vice versa.
search_breaker = _CircuitBreaker()
ingest_breaker = _CircuitBreaker()

# Serializes background cognify runs (LLM-heavy; one at a time is plenty)
# and detaches them from the tool call's short-lived event loop. Daemon
# threads: an in-flight cognify never blocks process exit.
_background_executor: ThreadPoolExecutor | None = None
_background_lock = threading.Lock()

# cognee.config calls are global, so the store setup is applied once per
# distinct settings fingerprint — a config change (new data_root, new LLM
# key) takes effect on the next call instead of requiring a restart.
_store_fingerprint: tuple[Any, ...] | None = None
_store_lock = threading.Lock()


def _resolve_llm_api_key(value: str) -> str | None:
    """Resolve the configured LLM key, honoring secret references.

    ``keychain:<name>`` / ``env:<VAR>`` / ``$VAR`` shapes go through
    :func:`omnigent.onboarding.provider_config.resolve_secret`, so the
    plaintext key can stay out of config.yaml. A failed resolution logs
    and returns ``None`` (cognee falls back to its own env/config).
    """
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.startswith(("keychain:", "env:")) or "$" in stripped:
        from omnigent.onboarding.provider_config import resolve_secret

        try:
            return resolve_secret(stripped)
        except Exception as e:
            _logger.error("cognee llm_api_key reference %r failed to resolve: %s", stripped, e)
            return None
    return stripped


def _ensure_local_store(settings: dict[str, Any]) -> None:
    """Point cognee's global config at the embedded local store.

    Also applies optional LLM settings from the ``cognee:`` block
    (``llm_api_key`` — plaintext or a ``keychain:``/``env:``/``$VAR``
    reference — plus ``llm_provider`` / ``llm_model``) so the cognify
    pipeline can run without a separate cognee config file. Absent keys
    leave cognee's own defaults (its env vars / .env) untouched. Applied
    once per settings fingerprint; the first differing settings re-apply.
    """
    global _store_fingerprint
    with _store_lock:
        root = cognee_data_root(settings)
        fingerprint = (
            str(root),
            settings.get("llm_api_key"),
            settings.get("llm_provider"),
            settings.get("llm_model"),
        )
        if fingerprint == _store_fingerprint:
            return
        import cognee

        (root / "system").mkdir(parents=True, exist_ok=True)
        (root / "data").mkdir(parents=True, exist_ok=True)
        cognee.config.system_root_directory(str(root / "system"))
        cognee.config.data_root_directory(str(root / "data"))
        llm_api_key = settings.get("llm_api_key")
        if isinstance(llm_api_key, str):
            resolved = _resolve_llm_api_key(llm_api_key)
            if resolved:
                cognee.config.set_llm_api_key(resolved)
        llm_config = {
            key: settings[cfg_key]
            for key, cfg_key in (("llm_provider", "llm_provider"), ("llm_model", "llm_model"))
            if isinstance(settings.get(cfg_key), str)
        }
        if llm_config:
            cognee.config.set_llm_config(llm_config)
        _store_fingerprint = fingerprint


def _run_bounded(coro_factory: Any, timeout_s: float) -> Any:
    """Run an async cognee operation on a fresh loop, bounded by a timeout.

    Tool ``invoke`` runs synchronously on a worker thread (the runner wraps
    it in ``asyncio.to_thread``), so a private event loop per operation is
    the simple, isolated choice.
    """
    return asyncio.run(asyncio.wait_for(coro_factory(), timeout=timeout_s))


def _record_outcome(target: _CircuitBreaker, ok: bool) -> None:
    if ok:
        target.record_success()
    else:
        target.record_failure()


def memory_search(
    query: str,
    datasets: list[str],
    *,
    settings: dict[str, Any] | None = None,
    top_k: int = 10,
) -> list[str]:
    """Search cognee memory across *datasets*; empty list on any failure.

    Never raises: timeouts, import errors, and store failures all log and
    return ``[]`` — memory must never fail a turn.
    """
    effective = cognee_settings() if settings is None else settings
    if not cognee_available() or not search_breaker.allow():
        return []
    try:
        _ensure_local_store(effective)
        import cognee

        # CHUNKS by default: raw stored memories, no LLM completion inside
        # the tool call (GRAPH_COMPLETION adds seconds of latency plus LLM
        # cost per search, and returns generated prose instead of memories).
        search_type_name = str(effective.get("search_type", "CHUNKS"))
        search_type = getattr(cognee.SearchType, search_type_name, None)
        if search_type is None:
            search_type = cognee.SearchType.CHUNKS

        async def _search() -> Any:
            return await cognee.search(
                query_type=search_type,
                query_text=query,
                datasets=datasets,
                top_k=top_k,
            )

        results = _run_bounded(_search, SEARCH_TIMEOUT_S)
        _record_outcome(search_breaker, True)
        return _flatten_search_results(results)
    except Exception as e:
        _record_outcome(search_breaker, False)
        _logger.error("cognee search failed: %s", e)
        return []


def _flatten_search_results(results: Any) -> list[str]:
    """Flatten cognee search output into the memory texts.

    cognee returns one envelope per searched dataset —
    ``{dataset_id, dataset_name, ..., search_result: [hits]}`` — where a hit
    is a chunk dict (with a ``text`` field next to store metadata) or, for
    LLM search types, a plain answer string. The agent should see the
    memories, not the plumbing, so envelopes are unwrapped and each hit
    reduced to its text; unknown shapes fall back to ``str``.
    """
    texts: list[str] = []
    for entry in results or []:
        if isinstance(entry, dict) and "search_result" in entry:
            inner = entry["search_result"]
            hits = inner if isinstance(inner, list) else [inner]
        else:
            hits = [entry]
        for hit in hits:
            text = _result_text(hit)
            if text.strip():
                texts.append(text)
    return texts


def _result_text(hit: Any) -> str:
    """Reduce one search hit to its memory text (``str`` fallback)."""
    if isinstance(hit, dict):
        for key in ("text", "content", "answer"):
            value = hit.get(key)
            if isinstance(value, str) and value.strip():
                return value
    else:
        text = getattr(hit, "text", None)
        if isinstance(text, str) and text.strip():
            return text
    return str(hit)


def memory_add(
    content: str,
    dataset: str,
    *,
    settings: dict[str, Any] | None = None,
    node_set: list[str] | None = None,
) -> bool:
    """Store *content* into *dataset* and schedule background cognify.

    The ``add`` (local write) runs inline and bounded; the LLM-heavy
    ``cognify`` is handed to the background worker so the tool returns
    fast — eventual consistency within the same session is accepted.
    Returns ``False`` (never raises) when the write failed.
    """
    effective = cognee_settings() if settings is None else settings
    if not cognee_available() or not ingest_breaker.allow():
        return False
    try:
        _ensure_local_store(effective)
        import cognee

        async def _add() -> Any:
            kwargs: dict[str, Any] = {"dataset_name": dataset}
            if node_set:
                kwargs["node_set"] = node_set
            return await cognee.add(content, **kwargs)

        _run_bounded(_add, ADD_TIMEOUT_S)
        _record_outcome(ingest_breaker, True)
    except Exception as e:
        _record_outcome(ingest_breaker, False)
        _logger.error("cognee add failed: %s", e)
        return False

    _get_background_executor().submit(_cognify_blocking, dataset)
    return True


def _get_background_executor() -> ThreadPoolExecutor:
    """Return the shared single-worker executor for off-turn cognee work."""
    global _background_executor
    with _background_lock:
        if _background_executor is None:
            _background_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="cognee-background"
            )
        return _background_executor


def _cognify_blocking(dataset: str) -> None:
    """Background worker body: run cognify for *dataset*, log-and-drop errors.

    Logs the per-run outcome: a cognify that races the preceding ``add`` can
    complete as a silent no-op (observed live: the memory then never reaches
    the vector index until a later cognify), so the pipeline result must be
    visible in logs rather than discarded.
    """
    try:
        import cognee

        async def _cognify() -> Any:
            return await cognee.cognify(datasets=[dataset])

        result = _run_bounded(_cognify, COGNIFY_TIMEOUT_S)
        ingest_breaker.record_success()
        _logger.info("cognee cognify finished for dataset %r: %.300s", dataset, result)
    except Exception as e:
        ingest_breaker.record_failure()
        _logger.error("cognee cognify failed for dataset %r: %s", dataset, e)


# Agent connections already mirrored into cognee's registry this process,
# keyed by (private dataset, session id) so a new session re-registers with
# its current session id but repeat tool calls don't re-post.
# custom-lint: disable-next=workspace-scoped-cache -- globally-unique session id key
_registered_agent_connections: set[tuple[str, str]] = set()
_registration_lock = threading.Lock()

REGISTER_TIMEOUT_S = 10.0


def ensure_agent_registered(
    grants: MemoryGrants,
    *,
    session_id: str | None,
    settings: dict[str, Any] | None = None,
) -> None:
    """Mirror this agent's :class:`MemoryGrants` into cognee's agent registry.

    Best-effort observability only: cognee >=1.5 tracks agent connections
    (``cognee.modules.agents``) so its tooling can show which agents touch
    which memory pools. The registration runs on the background worker —
    it never blocks a turn, never counts toward the circuit breaker, and
    silently skips on cognee builds without the agents module (the extra
    allows any 1.x). Authorization stays with :class:`MemoryGrants`; the
    registration is a descriptive mirror, and the precise read/write split
    rides ``metadata`` because ``RegisterAgentRequest`` only carries flat
    dataset names.
    """
    if not cognee_available():
        return
    key = (grants.private, session_id or "")
    with _registration_lock:
        if key in _registered_agent_connections:
            return
        # Bound the dedupe set: past ~4k live (dataset, session) pairs, drop
        # the history — re-registration is idempotent, so the only cost of
        # forgetting is an occasional duplicate best-effort mirror post.
        if len(_registered_agent_connections) >= 4096:
            _registered_agent_connections.clear()
        _registered_agent_connections.add(key)
    effective = cognee_settings() if settings is None else settings
    _get_background_executor().submit(_register_agent_blocking, grants, session_id, effective)


def _register_agent_blocking(
    grants: MemoryGrants,
    session_id: str | None,
    settings: dict[str, Any] | None,
) -> None:
    """Background worker body: post the agent connection, log-and-drop errors."""
    try:
        import importlib

        _ensure_local_store(settings or {})
        models = importlib.import_module("cognee.modules.agents.models")
        operations = importlib.import_module("cognee.modules.agents.operations")
        users = importlib.import_module("cognee.modules.users.methods")
        readable = list(grants.readable())
        writable = list(grants.writable())
        request = models.RegisterAgentRequest(
            agent_session_name=grants.private,
            type="omnigent",
            memory_mode="cognee",
            session_id=session_id,
            dataset_names=readable + [name for name in writable if name not in readable],
            source="api",
            origin_function="omnigent.runtime.memory",
            metadata={"grants": {"readable": readable, "writable": writable}},
        )

        async def _register() -> Any:
            user = await users.get_default_user()
            return await operations.register_agent_from_request(user, request)

        _run_bounded(_register, REGISTER_TIMEOUT_S)
    except Exception as e:
        _logger.debug("cognee agent registration skipped: %s", e)
