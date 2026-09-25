"""Tests for ``dev/lint/lint_workspace_scoped_cache.py``.

The hook requires module-level caches under omnigent/server and
omnigent/runtime to be WorkspaceScopedCache / WorkspaceScopedSet so keys can't
leak across tenants. Detection is over ``cachetools`` caches and EMPTY mutable
collections; constant lookup tables and wrapped caches pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import dev.lint.lint_workspace_scoped_cache as lint


def _names(source: str) -> list[str]:
    return [name for _line, name in lint.flagged_globals(source)]


def test_flags_raw_cachetools_caches() -> None:
    """A bare cachetools cache global is flagged."""
    src = (
        "import cachetools\n"
        "_a = cachetools.LRUCache(maxsize=4096)\n"
        "_b = cachetools.TTLCache(maxsize=256, ttl=30)\n"
    )
    assert _names(src) == ["_a", "_b"]


def test_flags_empty_mutable_collections() -> None:
    """Empty dict/set/defaultdict/Weak* globals are runtime caches → flagged."""
    src = (
        "import weakref\n"
        "from collections import defaultdict\n"
        "_a: dict[str, int] = {}\n"
        "_b = dict()\n"
        "_c: set[str] = set()\n"
        "_d = defaultdict(list)\n"
        "_e = weakref.WeakValueDictionary()\n"
        "_f = weakref.WeakKeyDictionary()\n"
    )
    assert _names(src) == ["_a", "_b", "_c", "_d", "_e", "_f"]


def test_passes_workspace_scoped_wrappers() -> None:
    """WorkspaceScopedCache / WorkspaceScopedSet globals pass, incl. a factory."""
    src = (
        "import cachetools\n"
        "_a: WorkspaceScopedCache[str, int] = WorkspaceScopedCache()\n"
        "_b = WorkspaceScopedSet()\n"
        "_c = WorkspaceScopedCache(lambda: cachetools.LRUCache(maxsize=10))\n"
    )
    assert _names(src) == []


def test_ignores_constant_tables_and_declarations() -> None:
    """Non-empty literals, frozensets, and bare annotations are not caches."""
    src = (
        "import cachetools\n"
        "_TABLE = {'a': 1, 'b': 2}\n"  # constant lookup table
        "_SKIP = frozenset({'x', 'y'})\n"  # immutable
        "_SEEDED = dict(a=1)\n"  # constructed with content
        "_SEEDED2 = set(('x',))\n"  # constructed with content
        "_lock = threading.Lock()\n"  # not a collection
        "_only_declared: dict[str, int]\n"  # annotation, no value
    )
    assert _names(src) == []


def test_flags_direct_imported_cachetools() -> None:
    """The direct-import form `from cachetools import LRUCache; _c = LRUCache()` is flagged."""
    src = "from cachetools import LRUCache\n_c = LRUCache(maxsize=1)\n"
    assert _names(src) == ["_c"]


def test_flags_cache_inside_module_level_control_flow() -> None:
    """A cache declared inside a module-level try/except is still module-level state."""
    src = "try:\n    _a = {}\nexcept ImportError:\n    _b = {}\n"
    assert _names(src) == ["_a", "_b"]


def test_ignores_cache_inside_function_body() -> None:
    """A cache-like local inside a function/class is not module-level state."""
    src = "def f():\n    _local = {}\n    return _local\n"
    assert _names(src) == []


def test_scan_skips_files_outside_scanned_roots(tmp_path: Path) -> None:
    """A cache-like global outside omnigent/server|runtime is not scanned."""
    f = tmp_path / "outside.py"
    f.write_text("_c = {}\n")
    # Default roots are omnigent/server|runtime; a tmp file matches neither.
    assert lint.scan(f) == []


def test_scan_respects_inline_disable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `# custom-lint: disable=` on the declaration line suppresses it."""
    f = tmp_path / "mod.py"
    f.write_text("_ok = {}  # custom-lint: disable=workspace-scoped-cache -- safe\n_bad = {}\n")
    monkeypatch.setattr(lint, "SCANNED_ROOTS", (lint._repo_relative(f),))
    assert [h.name for h in lint.scan(f)] == ["_bad"]


def test_scan_respects_disable_next(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `# custom-lint: disable-next=` on the line above suppresses the decl."""
    f = tmp_path / "mod.py"
    f.write_text(
        "# custom-lint: disable-next=workspace-scoped-cache -- safe\n_ok = {}\n_bad = {}\n"
    )
    monkeypatch.setattr(lint, "SCANNED_ROOTS", (lint._repo_relative(f),))
    assert [h.name for h in lint.scan(f)] == ["_bad"]


def test_scan_ignores_disable_for_other_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disable for a different rule id does not suppress this rule."""
    f = tmp_path / "mod.py"
    f.write_text("_c = {}  # custom-lint: disable=some-other-rule\n")
    monkeypatch.setattr(lint, "SCANNED_ROOTS", (lint._repo_relative(f),))
    assert [h.name for h in lint.scan(f)] == ["_c"]


def test_main_clean_tree_returns_zero() -> None:
    """The real tree must be free of un-scoped caches (regression guard)."""
    assert lint.main(["lint_workspace_scoped_cache.py"]) == 0


def test_main_flags_dirty_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``main`` returns 1 when an in-scope file has an un-scoped cache."""
    f = tmp_path / "dirty.py"
    f.write_text("import cachetools\n_c = cachetools.LRUCache(maxsize=1)\n")
    monkeypatch.setattr(lint, "SCANNED_ROOTS", (lint._repo_relative(f),))
    assert lint.main(["lint_workspace_scoped_cache.py", str(f)]) == 1
