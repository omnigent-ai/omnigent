"""Unit tests for ``scripts/normalize_uv_lock_registry.py``.

The pre-commit fixer rewrites every ``source = { registry = "<url>" }``
in ``uv.lock`` to public PyPI so a developer's local index/proxy never
leaks into the committed lockfile. These tests pin that contract:
arbitrary registry URLs are normalized, non-registry sources are left
alone, the fixer is idempotent, and ``main`` signals modifications via
its exit code (1 = changed → commit aborts and re-stages; 0 = clean).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "normalize_uv_lock_registry.py"

# The canonical index the fixer must always produce — kept in the test as
# an independent literal so a change to the script's constant is caught.
_CANONICAL = "https://pypi.org/simple"


def _load_module() -> Any:
    """Import ``scripts/normalize_uv_lock_registry.py`` from its file path.

    ``scripts/`` is not a package on ``sys.path`` (mirrors
    ``tests/server/test_openapi_drift.py``'s loader), so load it directly.

    :returns: The imported module, exposing ``normalize_text`` and ``main``.
    """
    spec = importlib.util.spec_from_file_location("scripts_normalize_uv_lock", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, (
        f"Could not locate the script at {_SCRIPT_PATH}."
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()


def test_normalize_text_rewrites_proxy_registry() -> None:
    """A Databricks-proxy registry URL is rewritten to public PyPI."""
    text = 'source = { registry = "https://pypi-proxy.cloud.databricks.com/simple" }\n'
    assert _MOD.normalize_text(text) == f'source = {{ registry = "{_CANONICAL}" }}\n'


def test_normalize_text_rewrites_any_index() -> None:
    """Normalization is proxy-agnostic — any registry URL collapses to PyPI."""
    text = 'source = { registry = "https://nexus.internal.example.com/repository/pypi/simple" }'
    assert _MOD.normalize_text(text) == f'source = {{ registry = "{_CANONICAL}" }}'


def test_normalize_text_rewrites_every_occurrence() -> None:
    """Multiple package entries are all normalized in one pass."""
    proxy = "https://pypi-proxy.cloud.databricks.com/simple"
    text = (
        f'source = {{ registry = "{proxy}" }}\n'
        f'source = {{ registry = "{proxy}" }}\n'
        f'source = {{ registry = "{proxy}" }}\n'
    )
    result = _MOD.normalize_text(text)
    assert proxy not in result
    assert result.count(f'registry = "{_CANONICAL}"') == 3


def test_normalize_text_leaves_non_registry_sources_untouched() -> None:
    """``git`` / ``path`` / ``editable`` sources and other content survive."""
    text = (
        'source = { git = "https://github.com/org/repo.git" }\n'
        'source = { editable = "." }\n'
        'source = { path = "sdks/python-client" }\n'
        'name = "some-package"\n'
        'sdist = { url = "https://files.pythonhosted.org/packages/ab/cd/pkg.tar.gz" }\n'
    )
    assert _MOD.normalize_text(text) == text


def test_normalize_text_already_canonical_is_noop() -> None:
    """Text already pointing at public PyPI is returned unchanged."""
    text = f'source = {{ registry = "{_CANONICAL}" }}\n'
    assert _MOD.normalize_text(text) == text


def test_main_rewrites_file_and_returns_one_when_changed(tmp_path: Path) -> None:
    """``main`` rewrites a proxy lockfile in place and returns 1 (changed)."""
    lock = tmp_path / "uv.lock"
    lock.write_text('source = { registry = "https://pypi-proxy.cloud.databricks.com/simple" }\n')
    rc = _MOD.main([str(lock)])
    assert rc == 1
    assert lock.read_text() == f'source = {{ registry = "{_CANONICAL}" }}\n'


def test_main_returns_zero_when_already_canonical(tmp_path: Path) -> None:
    """``main`` leaves a canonical lockfile untouched and returns 0 (clean)."""
    lock = tmp_path / "uv.lock"
    original = f'source = {{ registry = "{_CANONICAL}" }}\n'
    lock.write_text(original)
    rc = _MOD.main([str(lock)])
    assert rc == 0
    assert lock.read_text() == original


def test_main_is_idempotent(tmp_path: Path) -> None:
    """A second run after normalization is a no-op returning 0."""
    lock = tmp_path / "uv.lock"
    lock.write_text('source = { registry = "https://pypi-proxy.cloud.databricks.com/simple" }\n')
    assert _MOD.main([str(lock)]) == 1
    assert _MOD.main([str(lock)]) == 0


def test_main_handles_multiple_files(tmp_path: Path) -> None:
    """Given several files, any change yields exit 1 and each is normalized."""
    proxy = 'source = { registry = "https://pypi-proxy.cloud.databricks.com/simple" }\n'
    canonical = f'source = {{ registry = "{_CANONICAL}" }}\n'
    a = tmp_path / "a.lock"
    b = tmp_path / "b.lock"
    a.write_text(proxy)
    b.write_text(canonical)
    rc = _MOD.main([str(a), str(b)])
    assert rc == 1
    assert a.read_text() == canonical
    assert b.read_text() == canonical


def test_non_canonical_registries_lists_offenders() -> None:
    """The check helper reports each non-canonical registry URL, in order."""
    proxy = "https://pypi-proxy.cloud.databricks.com/simple"
    text = (
        f'source = {{ registry = "{proxy}" }}\n'
        f'source = {{ registry = "{_CANONICAL}" }}\n'
        f'source = {{ registry = "{proxy}" }}\n'
    )
    assert _MOD.non_canonical_registries(text) == [proxy, proxy]


def test_non_canonical_registries_empty_when_canonical() -> None:
    """A fully-canonical lockfile reports no offenders."""
    text = f'source = {{ registry = "{_CANONICAL}" }}\n'
    assert _MOD.non_canonical_registries(text) == []


def test_main_check_fails_without_writing(tmp_path: Path) -> None:
    """``--check`` returns 1 for a proxy lockfile and does NOT modify it."""
    lock = tmp_path / "uv.lock"
    original = 'source = { registry = "https://pypi-proxy.cloud.databricks.com/simple" }\n'
    lock.write_text(original)
    rc = _MOD.main(["--check", str(lock)])
    assert rc == 1
    # Check mode must be read-only — the file is left exactly as-is.
    assert lock.read_text() == original


def test_main_check_passes_when_canonical(tmp_path: Path) -> None:
    """``--check`` returns 0 for an already-canonical lockfile."""
    lock = tmp_path / "uv.lock"
    lock.write_text(f'source = {{ registry = "{_CANONICAL}" }}\n')
    assert _MOD.main(["--check", str(lock)]) == 0


def test_main_check_flag_position_independent(tmp_path: Path) -> None:
    """``--check`` is recognized whether it precedes or follows the file."""
    lock = tmp_path / "uv.lock"
    lock.write_text(f'source = {{ registry = "{_CANONICAL}" }}\n')
    assert _MOD.main([str(lock), "--check"]) == 0
