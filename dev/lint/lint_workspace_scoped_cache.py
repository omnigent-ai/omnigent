"""Flag module-level in-process caches that are not workspace-scoped.

One OSS server process serves many workspaces (multi-tenant deployments
bind ``current_workspace_id()`` per request). A module-level cache keyed by
a workspace-collidable id — ``conversation_id`` / ``session_id`` (collide
across workspaces via imported sessions), ``user_id`` / email (one user,
many workspaces) — leaks one tenant's value into another tenant's request
on a shared pod.

This hook requires every module-level cache-like global under
``omnigent/server`` and ``omnigent/runtime`` to be a
:class:`~omnigent.db.workspace_cache.WorkspaceScopedCache` /
:class:`~omnigent.db.workspace_cache.WorkspaceScopedSet` (which namespace
every key by workspace), unless the global is in :data:`ALLOWLIST` because
its key is already globally unique (``call_id``, ``runner_id``,
``elicitation_id``, task objects, …) or already contains the workspace id.

"Cache-like" = a module-level assignment whose value is a ``cachetools``
cache, or an EMPTY mutable collection (``{}`` / ``dict()`` / ``set()`` /
``defaultdict(...)`` / ``weakref.Weak{Value,Key}Dictionary()``). Empty at
module load == populated at runtime == a cache/registry; non-empty literals
are constant lookup tables and are not flagged.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Roots whose module-level caches must be workspace-scoped. These are the
# request-handling surfaces where a cross-tenant read is possible.
SCANNED_ROOTS = ("omnigent/server/", "omnigent/runtime/")

# cachetools cache classes (all runtime-populated caches).
_CACHETOOLS_CACHES = frozenset(
    {"Cache", "LRUCache", "TTLCache", "LFUCache", "RRCache", "FIFOCache", "MRUCache"}
)
# Workspace-safe wrapper types — the required construction.
_WRAPPER_TYPES = frozenset({"WorkspaceScopedCache", "WorkspaceScopedSet"})
# Bare collection constructors that start empty.
_EMPTY_CTOR_NAMES = frozenset({"dict", "set", "defaultdict"})
_WEAK_MAP_NAMES = frozenset({"WeakValueDictionary", "WeakKeyDictionary"})

# Globals allowed to skip workspace scoping, each with the reason. Keyed by
# ``(repo-relative-path, variable-name)``. Add here only when the key is
# genuinely globally unique across workspaces (or already workspace-scoped);
# adding an entry is visible in review.
ALLOWLIST: dict[tuple[str, str], str] = {
    # Keyed by an already-globally-unique id — no cross-workspace collision.
    ("omnigent/runtime/_globals.py", "_dispatch_capabilities"): "keyed by process-local task_id",
    (
        "omnigent/server/_elicitation_registry.py",
        "_harness_elicitation_registry",
    ): "keyed by elicitation_id (unique UUID)",
    (
        "omnigent/server/_elicitation_registry.py",
        "_harness_elicitation_owners",
    ): "keyed by elicitation_id (unique UUID)",
    (
        "omnigent/server/_elicitation_registry.py",
        "_harness_parked_elicitations",
    ): "keyed by elicitation_id (unique UUID)",
    (
        "omnigent/server/_elicitation_registry.py",
        "_harness_pre_resolved_elicitations",
    ): "keyed by elicitation_id (unique UUID)",
    (
        "omnigent/server/managed_host_keepalive.py",
        "_last_kept",
    ): "keyed by runner_id (globally unique)",
    (
        "omnigent/server/managed_host_keepalive.py",
        "_inflight",
    ): "keyed by runner_id (globally unique)",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_browser_action_registry",
    ): "keyed by server-minted action_id (globally unique)",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_browser_action_owners",
    ): "keyed by server-minted action_id (globally unique)",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_browser_action_claims",
    ): "keyed by server-minted action_id (globally unique)",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_browser_action_claim_events",
    ): "keyed by server-minted action_id (globally unique)",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_recent_mirrored_tool_calls",
    ): "keyed by call_id (globally unique per turn)",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_pending_policy_ask_writes",
    ): "keyed by elicitation_id (unique UUID)",
    # Keyed solely by workspace_id, or a key that already contains it.
    (
        "omnigent/runtime/policies/builder.py",
        "_DEFAULT_POLICY_SPECS_CACHE",
    ): "keyed solely by workspace_id",
    ("omnigent/server/scheduled/fire.py", "_IN_FLIGHT_TASKS"): "keyed by (workspace_id, task_id)",
    # Static process-wide registries — not tenant data, populated at import /
    # startup with a fixed keyspace.
    ("omnigent/server/dictation.py", "_ENGINE_REGISTRY"): "static engine-name registry",
    (
        "omnigent/runtime/filesystem_registry.py",
        "_untracked_cache_enabled",
    ): "keyed by git-root filesystem path, not a tenant id",
    (
        "omnigent/server/sharing_settings.py",
        "_cache",
    ): "keyed by filesystem path (config-file mtime cache)",
    # Sets of asyncio.Task / TimerHandle objects — keyed by object identity,
    # so no id collision is possible.
    ("omnigent/server/routes/_sessions/common.py", "_WATCHER_TASKS"): "set of Task objects",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_catalog_prefetch_tasks",
    ): "set of Task objects",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_deferred_elicitation_clear_tasks",
    ): "set of Task objects",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_native_popup_forward_tasks",
    ): "set of Task objects",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_managed_launch_tasks",
    ): "set of Task objects",
    (
        "omnigent/server/routes/_sessions/helpers.py",
        "_detached_supersede_stops",
    ): "set of Task objects",
    (
        "omnigent/server/routes/_sessions/orchestration.py",
        "_detached_stop_tasks",
    ): "set of Task objects",
    ("omnigent/server/scheduled/fire.py", "_PENDING_FIRES"): "set of Task objects",
    ("omnigent/server/scheduled/scheduler.py", "_PENDING_FIRES"): "set of Task objects",
    # Lock / semaphore registries. A cross-workspace key collision causes at
    # most benign extra serialization of unrelated work — never a data read —
    # and the DATA these guard is workspace-scoped in its own cache.
    (
        "omnigent/server/managed_hosts.py",
        "_resume_locks",
    ): "lock registry keyed by host_id; collision only serializes",
    (
        "omnigent/server/routes/_sessions/common.py",
        "_native_ask_gate_locks",
    ): "lock registry; collision only serializes, no data read",
    (
        "omnigent/server/routes/_sessions/helpers.py",
        "_catalog_prefetch_semaphores",
    ): "semaphore registry keyed by object identity",
    (
        "omnigent/server/routes/_sessions/helpers.py",
        "_relaunch_locks",
    ): "lock registry; collision only serializes, no data read",
    (
        "omnigent/server/routes/sessions/routes_events.py",
        "_retry_recovery_locks",
    ): "lock registry; collision only serializes, no data read",
}


@dataclass(frozen=True)
class Hit:
    """One un-scoped module-level cache global."""

    path: Path
    line: int
    name: str


def _repo_relative(path: Path) -> str:
    """Return a stable repo-relative POSIX path when possible."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    """Return the simple ``Name`` targets of a module-level assignment."""
    if isinstance(node, ast.AnnAssign):
        return [node.target.id] if isinstance(node.target, ast.Name) else []
    names: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _callee_attr(call: ast.Call) -> tuple[str | None, str | None]:
    """Return ``(base, attr)`` for ``base.attr(...)`` / ``(None, name)`` for ``name(...)``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        base = func.value.id if isinstance(func.value, ast.Name) else None
        return base, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _is_wrapper(value: ast.expr) -> bool:
    """``True`` when *value* constructs a workspace-scoped wrapper."""
    if not isinstance(value, ast.Call):
        return False
    _base, attr = _callee_attr(value)
    return attr in _WRAPPER_TYPES


def _is_cache_like(value: ast.expr) -> bool:
    """``True`` when *value* is a cachetools cache or an EMPTY mutable collection."""
    # Empty dict literal: {}
    if isinstance(value, ast.Dict) and not value.keys:
        return True
    if not isinstance(value, ast.Call):
        return False
    base, attr = _callee_attr(value)
    if attr is None:
        return False
    # cachetools.<X>Cache(...)
    if base == "cachetools" and attr in _CACHETOOLS_CACHES:
        return True
    # weakref.WeakValueDictionary() / WeakKeyDictionary() (any args → still a registry)
    if attr in _WEAK_MAP_NAMES:
        return True
    # defaultdict(...) is always a runtime-populated registry.
    if attr == "defaultdict":
        return True
    # dict()/set() with NO args == an empty runtime cache. dict(a=1)/set(x) build
    # constant content, so require the empty form.
    if attr in {"dict", "set"} and not value.args and not value.keywords:
        return True
    return False


def flagged_globals(source: str) -> list[tuple[int, str]]:
    """Return ``(lineno, name)`` for every un-scoped cache-like module global.

    Pure detection over source text — no path, root, or allowlist filtering — so
    it is directly unit-testable. :func:`scan` layers the root and allowlist
    filters on top.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []
    found: list[tuple[int, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None or _is_wrapper(value) or not _is_cache_like(value):
            continue
        for name in _target_names(node):
            found.append((node.lineno, name))
    return found


def scan(path: Path) -> list[Hit]:
    """Return un-scoped cache globals declared at module top level in *path*."""
    rel = _repo_relative(path)
    if not any(rel.startswith(root) for root in SCANNED_ROOTS):
        return []
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    return [
        Hit(path, line, name)
        for line, name in flagged_globals(source)
        if (rel, name) not in ALLOWLIST
    ]


def _iter_scannable_paths() -> list[Path]:
    """Return every tracked ``.py`` file under the scanned roots."""
    output = subprocess.check_output(["git", "ls-files", "-z", *SCANNED_ROOTS])
    return [Path(raw) for raw in output.decode().split("\0") if raw.endswith(".py")]


def main(argv: list[str] | None = None) -> int:
    """Scan the given paths (or the full surface) and reject un-scoped caches."""
    args = argv if argv is not None else sys.argv
    explicit = [Path(a) for a in args[1:]]
    paths = explicit or _iter_scannable_paths()
    hits = [hit for path in paths for hit in scan(path)]
    if not hits:
        return 0
    for hit in hits:
        sys.stdout.write(
            f"{_repo_relative(hit.path)}:{hit.line}: module-level cache `{hit.name}` is not "
            "workspace-scoped\n"
        )
    sys.stdout.write(
        "\nModule-level caches under omnigent/server and omnigent/runtime must be "
        "WorkspaceScopedCache / WorkspaceScopedSet (omnigent.db.workspace_cache) so keys are "
        "namespaced by workspace and cannot leak across tenants. If the key is already "
        "globally unique (call_id, runner_id, elicitation_id, task objects) or already "
        "contains the workspace id, add it to ALLOWLIST in "
        "dev/lint/lint_workspace_scoped_cache.py with a reason.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
