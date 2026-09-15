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
every key by workspace). A global whose key is already globally unique
(``call_id``, ``runner_id``, ``elicitation_id``, task objects, …) or already
contains the workspace id is exempted inline, ruff-style, with a
``# custom-lint: disable=workspace-scoped-cache -- <reason>`` comment on the
declaration line (or ``disable-next`` on the line above); see
:mod:`dev.lint._framework`.

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

from dev.lint._framework import disabled_rules_by_line

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

    Pure detection over source text — no path, root, or suppression filtering —
    so it is directly unit-testable. :func:`scan` layers the root filter and
    inline ``# custom-lint: disable`` suppression on top.
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
    """Return un-scoped cache globals in *path*, honoring inline disables.

    A cache carrying ``# custom-lint: disable=workspace-scoped-cache`` on its
    declaration line (or ``disable-next`` on the line above) is skipped.
    """
    rel = _repo_relative(path)
    if not any(rel.startswith(root) for root in SCANNED_ROOTS):
        return []
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError):
        return []
    disabled = disabled_rules_by_line(source)
    return [
        Hit(path, line, name)
        for line, name in flagged_globals(source)
        if RULE_NAME not in disabled.get(line, frozenset())
    ]


def _iter_scannable_paths() -> list[Path]:
    """Return every tracked ``.py`` file under the scanned roots."""
    output = subprocess.check_output(["git", "ls-files", "-z", *SCANNED_ROOTS])
    return [Path(raw) for raw in output.decode().split("\0") if raw.endswith(".py")]


# Rule identity + fix guidance, consumed by the dev/lint/custom_lint.py runner.
RULE_NAME = "workspace-scoped-cache"
HINT = (
    "Module-level caches under omnigent/server and omnigent/runtime must be "
    "WorkspaceScopedCache / WorkspaceScopedSet (omnigent.db.workspace_cache) so keys are "
    "namespaced by workspace and cannot leak across tenants. If the key is already "
    "globally unique (call_id, runner_id, elicitation_id, task objects) or already "
    "contains the workspace id, exempt it inline with "
    "`# custom-lint: disable=workspace-scoped-cache -- <reason>` on the declaration line "
    "(or `disable-next` on the line above)."
)


def check() -> list[str]:
    """Rule entry point for the custom-lint runner: full-surface scan → messages.

    Returns one ``path:line: message`` string per violation; empty means clean.
    """
    return [
        f"{_repo_relative(hit.path)}:{hit.line}: module-level cache `{hit.name}` is not "
        "workspace-scoped"
        for path in _iter_scannable_paths()
        for hit in scan(path)
    ]


def main(argv: list[str] | None = None) -> int:
    """Scan the given paths (or the full surface) and reject un-scoped caches."""
    args = argv if argv is not None else sys.argv
    explicit = [Path(a) for a in args[1:]]
    if explicit:
        messages = [
            f"{_repo_relative(hit.path)}:{hit.line}: module-level cache `{hit.name}` is not "
            "workspace-scoped"
            for path in explicit
            for hit in scan(path)
        ]
    else:
        messages = check()
    if not messages:
        return 0
    for message in messages:
        sys.stdout.write(f"{message}\n")
    sys.stdout.write(f"\n{HINT}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
