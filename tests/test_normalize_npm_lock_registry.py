"""Unit tests for ``scripts/normalize_npm_lock_registry.py``.

The pre-commit fixer rewrites every registry-tarball ``"resolved"`` URL in
``ap-web/package-lock.json`` back to public npm so a developer's local
index/proxy (e.g. the Databricks npm proxy) never leaks into the committed
lockfile and breaks ``npm ci`` on public CI. These tests pin that contract:
proxy tarball hosts are normalized, ``git+``/``file:`` resolved entries are left
alone, the fixer is idempotent, and ``main`` signals modifications via its exit
code (1 = changed → commit aborts and re-stages; 0 = clean).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "normalize_npm_lock_registry.py"

# The canonical host the fixer must always produce — kept as an independent
# literal so a change to the script's constant is caught.
_HOST = "registry.npmjs.org"
_PROXY = "npm-proxy.cloud.databricks.com"


def _load_module() -> Any:
    """Import ``scripts/normalize_npm_lock_registry.py`` from its file path.

    ``scripts/`` is not a package on ``sys.path`` (mirrors the uv-lock test's
    loader), so load it directly.

    :returns: The module, exposing ``normalize_text`` / ``non_canonical_hosts`` /
        ``main``.
    """
    spec = importlib.util.spec_from_file_location("scripts_normalize_npm_lock", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None, (
        f"Could not locate the script at {_SCRIPT_PATH}."
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()

# The exact shape that broke CI: the `yaml` dep pinned to the Databricks proxy.
_PROXY_LINE = f'      "resolved": "https://{_PROXY}/yaml/-/yaml-1.10.3.tgz",\n'
_CANONICAL_LINE = f'      "resolved": "https://{_HOST}/yaml/-/yaml-1.10.3.tgz",\n'


def test_normalize_text_rewrites_proxy_tarball_host() -> None:
    """A Databricks-proxy tarball host is rewritten to public npm; path kept."""
    assert _MOD.normalize_text(_PROXY_LINE) == _CANONICAL_LINE


def test_normalize_text_rewrites_scoped_package() -> None:
    """Scoped-package tarball URLs (``@scope/pkg``) are normalized too."""
    text = f'    "resolved": "https://{_PROXY}/@lobehub/icons/-/icons-2.0.0.tgz",\n'
    expected = f'    "resolved": "https://{_HOST}/@lobehub/icons/-/icons-2.0.0.tgz",\n'
    assert _MOD.normalize_text(text) == expected


def test_normalize_text_rewrites_any_internal_host() -> None:
    """Normalization is proxy-agnostic — any non-npmjs tarball host collapses."""
    text = '"resolved": "https://nexus.internal.example.com/left-pad/-/left-pad-1.3.0.tgz"'
    expected = f'"resolved": "https://{_HOST}/left-pad/-/left-pad-1.3.0.tgz"'
    assert _MOD.normalize_text(text) == expected


def test_normalize_text_leaves_git_and_file_resolved_untouched() -> None:
    """``git+``/``file:``/workspace ``resolved`` entries are not registry tarballs."""
    text = (
        '"resolved": "git+ssh://git@github.com/org/repo.git#abc123",\n'
        '"resolved": "file:../local-pkg",\n'
        '"resolved": "https://github.com/org/repo/archive/refs/tags/v1.0.0.tar.gz",\n'
    )
    assert _MOD.normalize_text(text) == text


def test_normalize_text_already_canonical_is_noop() -> None:
    assert _MOD.normalize_text(_CANONICAL_LINE) == _CANONICAL_LINE


def test_normalize_text_rewrites_every_occurrence() -> None:
    text = _PROXY_LINE * 3
    result = _MOD.normalize_text(text)
    assert _PROXY not in result
    assert result.count(f"https://{_HOST}/yaml/-/yaml-1.10.3.tgz") == 3


def test_non_canonical_hosts_lists_offenders() -> None:
    text = _PROXY_LINE + _CANONICAL_LINE + _PROXY_LINE
    assert _MOD.non_canonical_hosts(text) == [_PROXY, _PROXY]


def test_non_canonical_hosts_empty_when_canonical() -> None:
    assert _MOD.non_canonical_hosts(_CANONICAL_LINE) == []


def test_main_rewrites_file_and_returns_one_when_changed(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text(_PROXY_LINE)
    assert _MOD.main([str(lock)]) == 1
    assert lock.read_text() == _CANONICAL_LINE


def test_main_returns_zero_when_already_canonical(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text(_CANONICAL_LINE)
    assert _MOD.main([str(lock)]) == 0
    assert lock.read_text() == _CANONICAL_LINE


def test_main_is_idempotent(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text(_PROXY_LINE)
    assert _MOD.main([str(lock)]) == 1
    assert _MOD.main([str(lock)]) == 0


def test_main_check_fails_without_writing(tmp_path: Path) -> None:
    """``--check`` returns 1 for a proxy lockfile and does NOT modify it."""
    lock = tmp_path / "package-lock.json"
    lock.write_text(_PROXY_LINE)
    assert _MOD.main(["--check", str(lock)]) == 1
    assert lock.read_text() == _PROXY_LINE  # read-only


def test_main_check_passes_when_canonical(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text(_CANONICAL_LINE)
    assert _MOD.main([str(lock), "--check"]) == 0  # flag position-independent
