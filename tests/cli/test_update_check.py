"""Tests for :mod:`omnigent.update_check`."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.update_check import (
    _STALENESS_SECONDS,
    _CacheEntry,
    _fetch_and_count,
    _find_repo_root,
    _is_stale,
    _read_cache,
    _run_check,
    _write_cache,
    maybe_show_update_notice,
)

# ------------------------------------------------------------------
# _find_repo_root
# ------------------------------------------------------------------


def test_find_repo_root_finds_git_dir() -> None:
    """``_find_repo_root`` returns the repo root when a ``.git/`` exists."""
    root = _find_repo_root()
    # The test itself runs inside the repo, so root must be non-None
    # and contain a .git directory.
    assert root is not None
    assert (root / ".git").is_dir()


def test_find_repo_root_no_git_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: ``_find_repo_root`` returns None when __file__ is outside any repo."""
    fake_file = tmp_path / "omnigent" / "update_check.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("")

    import omnigent.update_check as mod

    monkeypatch.setattr(mod, "__file__", str(fake_file))
    assert mod._find_repo_root() is None


def test_find_repo_root_ignores_unrelated_ancestor_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated ``.git/`` in an ancestor directory is ignored.

    Regression for the ``uv tool install`` scenario. The previous
    walk-up implementation matched ``~/.git/`` (a dotfiles repo) when
    omnigent was installed under ``~/.local/share/uv/tools/`` —
    misclassifying the install as a dev clone and writing
    ``kind: "clone"`` to the update-check cache.

    Layout:

        tmp_path/.git/                      ← unrelated dotfiles-style repo
        tmp_path/install/site-packages/omnigent/update_check.py
        (no .git/ or pyproject.toml in install/site-packages/)

    Expected: returns ``None`` because the direct parent of
    ``omnigent/`` (i.e. ``install/site-packages/``) is not a
    real repo, even though a ``.git/`` exists higher up.
    """
    (tmp_path / ".git").mkdir()
    site_packages = tmp_path / "install" / "site-packages"
    site_packages.mkdir(parents=True)
    pkg_dir = site_packages / "omnigent"
    pkg_dir.mkdir()
    fake_file = pkg_dir / "update_check.py"
    fake_file.write_text("")

    import omnigent.update_check as mod

    monkeypatch.setattr(mod, "__file__", str(fake_file))
    assert mod._find_repo_root() is None


def test_find_repo_root_requires_pyproject_alongside_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.git/`` next to ``omnigent/`` without ``pyproject.toml`` → None.

    Defense in depth against a hypothetical layout where someone
    has a directory tree like ``some_repo/omnigent/`` (e.g. a
    monorepo subdir, or an accidentally-named folder) but no
    ``pyproject.toml`` in the candidate. The pyproject check
    confirms we found OUR repo, not just any directory that
    happens to contain a folder called ``omnigent/``.
    """
    repo_like = tmp_path
    (repo_like / ".git").mkdir()
    # Deliberately NO pyproject.toml here.
    pkg_dir = repo_like / "omnigent"
    pkg_dir.mkdir()
    fake_file = pkg_dir / "update_check.py"
    fake_file.write_text("")

    import omnigent.update_check as mod

    monkeypatch.setattr(mod, "__file__", str(fake_file))
    assert mod._find_repo_root() is None


def test_find_repo_root_accepts_git_plus_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.git/`` AND ``pyproject.toml`` directly above ``omnigent/`` → repo root."""
    repo = tmp_path / "omnigent"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "omnigent"\n')
    pkg_dir = repo / "omnigent"
    pkg_dir.mkdir()
    fake_file = pkg_dir / "update_check.py"
    fake_file.write_text("")

    import omnigent.update_check as mod

    monkeypatch.setattr(mod, "__file__", str(fake_file))
    # Returns exactly the repo root — the parent of omnigent/.
    assert mod._find_repo_root() == repo


# ------------------------------------------------------------------
# Cache read / write
# ------------------------------------------------------------------


def test_read_cache_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_read_cache`` returns None when the cache file does not exist."""
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", tmp_path / "nope.json")
    assert _read_cache() is None


def test_read_cache_returns_none_on_corrupt_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_read_cache`` returns None when the cache file is not valid JSON."""
    cache_file = tmp_path / "bad.json"
    cache_file.write_text("not json at all")
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)
    assert _read_cache() is None


def test_read_cache_returns_none_on_missing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_read_cache`` returns None when required keys are absent."""
    cache_file = tmp_path / "partial.json"
    cache_file.write_text(json.dumps({"last_check_epoch": 1.0}))
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)
    assert _read_cache() is None


def test_write_then_read_cache_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write + read roundtrip preserves values."""
    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr("omnigent.update_check._CACHE_DIR", tmp_path)
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)

    entry = _CacheEntry(last_check_epoch=1716100000.0, commits_behind=5, head_sha="abc123")
    _write_cache(entry)

    result = _read_cache()
    assert result is not None
    assert result.last_check_epoch == 1716100000.0
    assert result.commits_behind == 5
    assert result.head_sha == "abc123"


# ------------------------------------------------------------------
# _is_stale
# ------------------------------------------------------------------


def test_is_stale_fresh() -> None:
    """A recently-created entry is not stale."""
    entry = _CacheEntry(last_check_epoch=time.time(), commits_behind=0)
    assert not _is_stale(entry)


def test_is_stale_old() -> None:
    """An entry older than 4 hours is stale."""
    entry = _CacheEntry(
        last_check_epoch=time.time() - 5 * 60 * 60,  # 5 hours ago
        commits_behind=0,
    )
    assert _is_stale(entry)


# ------------------------------------------------------------------
# _fetch_and_count
# ------------------------------------------------------------------


def test_fetch_and_count_git_not_found(tmp_path: Path) -> None:
    """Returns None when ``git`` is not on PATH."""
    with patch("omnigent.update_check.subprocess.run", side_effect=FileNotFoundError):
        assert _fetch_and_count(tmp_path, "main") is None


def test_fetch_and_count_fetch_fails(tmp_path: Path) -> None:
    """Returns None when ``git fetch`` exits non-zero."""
    with patch(
        "omnigent.update_check.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert _fetch_and_count(tmp_path, "main") is None


def test_fetch_and_count_fetch_timeout(tmp_path: Path) -> None:
    """Returns None when ``git fetch`` exceeds the timeout."""
    with patch(
        "omnigent.update_check.subprocess.run",
        side_effect=subprocess.TimeoutExpired("git", 5),
    ):
        assert _fetch_and_count(tmp_path, "main") is None


def test_fetch_and_count_success(tmp_path: Path) -> None:
    """Returns the commit count on a successful fetch + rev-list."""
    call_count = 0

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # git fetch — just succeed
            return subprocess.CompletedProcess(cmd, 0)
        # git rev-list --count
        return subprocess.CompletedProcess(cmd, 0, stdout="7\n")

    with patch("omnigent.update_check.subprocess.run", side_effect=fake_run):
        assert _fetch_and_count(tmp_path, "main") == 7


def test_fetch_and_count_revlist_fails(tmp_path: Path) -> None:
    """Returns None when fetch succeeds but rev-list fails."""
    call_count = 0

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subprocess.CompletedProcess(cmd, 0)
        raise subprocess.CalledProcessError(1, "git")

    with patch("omnigent.update_check.subprocess.run", side_effect=fake_run):
        assert _fetch_and_count(tmp_path, "main") is None


# ------------------------------------------------------------------
# _run_check
# ------------------------------------------------------------------


def test_run_check_falls_back_to_master(tmp_path: Path) -> None:
    """Falls back to ``origin/master`` when ``origin/main`` fetch fails."""
    call_count = 0

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        # Calls 1 (fetch main) -> fail, 2 (fetch master) -> ok,
        # 3 (rev-list master) -> 2 commits
        if call_count == 1:
            raise subprocess.CalledProcessError(1, "git")
        if call_count == 2:
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout="2\n")

    with patch("omnigent.update_check.subprocess.run", side_effect=fake_run):
        result = _run_check(tmp_path)
    assert result is not None
    assert result.commits_behind == 2


def test_run_check_both_branches_fail(tmp_path: Path) -> None:
    """Returns None when both main and master fail."""
    with patch(
        "omnigent.update_check.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "git"),
    ):
        assert _run_check(tmp_path) is None


# ------------------------------------------------------------------
# maybe_show_update_notice (top-level)
# ------------------------------------------------------------------


def test_skipped_when_env_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No-op when ``OMNIGENT_NO_UPDATE_CHECK`` is set."""
    monkeypatch.setenv("OMNIGENT_NO_UPDATE_CHECK", "1")
    maybe_show_update_notice()
    assert capsys.readouterr().err == ""


def test_no_repo_root_routes_to_wheel_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No clone reachable → dispatcher routes to the wheel-install path.

    Stub the wheel check so this test stays deterministic regardless of
    whether the test runner itself was installed via uv/pip/editable
    (which would otherwise change the wheel-check decision).
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    wheel_called = False

    def _stub_wheel_check() -> None:
        nonlocal wheel_called
        wheel_called = True

    monkeypatch.setattr("omnigent.update_check._run_installed_wheel_check", _stub_wheel_check)
    with patch("omnigent.update_check._find_repo_root", return_value=None):
        maybe_show_update_notice()
    # Dispatcher invoked the wheel path; no notice printed because we
    # stubbed it out — proves the dispatch is wired correctly.
    assert wheel_called is True
    assert capsys.readouterr().err == ""


def test_fresh_cache_shows_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prints notice when cache is fresh and ``commits_behind > 0``."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr("omnigent.update_check._CACHE_DIR", tmp_path)
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)

    # Write a fresh cache entry with commits_behind=3.
    entry = _CacheEntry(last_check_epoch=time.time(), commits_behind=3)
    _write_cache(entry)

    with patch("omnigent.update_check._find_repo_root", return_value=tmp_path):
        maybe_show_update_notice()

    err = capsys.readouterr().err
    assert "3 commit(s) ahead" in err
    assert "git pull" in err


def test_fresh_cache_clears_after_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Notice disappears when HEAD moves (user ran ``git pull``)."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr("omnigent.update_check._CACHE_DIR", tmp_path)
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)

    # Cache says 3 behind, recorded at old HEAD sha.
    entry = _CacheEntry(
        last_check_epoch=time.time(),
        commits_behind=3,
        head_sha="old_sha",
    )
    _write_cache(entry)

    # Simulate: HEAD has moved (pull), and local rev-list now says 0.
    with (
        patch("omnigent.update_check._find_repo_root", return_value=tmp_path),
        patch("omnigent.update_check._get_head_sha", return_value="new_sha"),
        patch("omnigent.update_check._local_rev_list_count", return_value=0),
    ):
        maybe_show_update_notice()

    # No notice — the user is up to date.
    assert capsys.readouterr().err == ""

    # Cache was updated with new count and new HEAD.
    refreshed = _read_cache()
    assert refreshed is not None
    assert refreshed.commits_behind == 0
    assert refreshed.head_sha == "new_sha"


def test_fresh_cache_no_notice_when_up_to_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No notice when cache is fresh and ``commits_behind == 0``."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr("omnigent.update_check._CACHE_DIR", tmp_path)
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)

    entry = _CacheEntry(last_check_epoch=time.time(), commits_behind=0)
    _write_cache(entry)

    with patch("omnigent.update_check._find_repo_root", return_value=tmp_path):
        maybe_show_update_notice()

    assert capsys.readouterr().err == ""


def test_stale_cache_triggers_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stale cache triggers a fresh check; notice printed if behind."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr("omnigent.update_check._CACHE_DIR", tmp_path)
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)

    # Write a stale cache.
    old_entry = _CacheEntry(
        last_check_epoch=time.time() - 5 * 60 * 60,
        commits_behind=0,
    )
    _write_cache(old_entry)

    call_count = 0

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0, stdout="4\n")

    with (
        patch("omnigent.update_check._find_repo_root", return_value=tmp_path),
        patch("omnigent.update_check.subprocess.run", side_effect=fake_run),
    ):
        maybe_show_update_notice()

    err = capsys.readouterr().err
    assert "4 commit(s) ahead" in err

    # Verify the cache was updated.
    refreshed = _read_cache()
    assert refreshed is not None
    assert refreshed.commits_behind == 4


def test_check_failure_caches_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When git check fails, caches ``commits_behind=0`` to avoid retry storm."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr("omnigent.update_check._CACHE_DIR", tmp_path)
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)

    with (
        patch("omnigent.update_check._find_repo_root", return_value=tmp_path),
        patch(
            "omnigent.update_check.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ),
    ):
        maybe_show_update_notice()

    # No notice printed.
    assert capsys.readouterr().err == ""

    # Cache was written with 0 to suppress retries.
    cached = _read_cache()
    assert cached is not None
    assert cached.commits_behind == 0


# ------------------------------------------------------------------
# Installed-wheel path
# ------------------------------------------------------------------


import importlib.metadata  # noqa: E402
import sys  # noqa: E402

from omnigent.update_check import (  # noqa: E402
    _build_upgrade_suggestion,
    _InstalledWheelInfo,
    _pip_invocation,
    _read_build_info,
    _read_installed_wheel_info,
    _run_installed_wheel_check,
    _unredact_ssh_userinfo,
)


@pytest.fixture(autouse=True)
def _block_build_info_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``from omnigent import _build_info`` fail in every test.

    The build hook in ``setup.py`` writes a real ``_build_info.py``
    into the source tree whenever a wheel is built locally. Without
    this fixture, the on-disk file would make ``_read_build_info``
    return live values for every test that doesn't explicitly inject
    a fake — silently overriding the uv_cache / mtime install-time
    signals most tests are trying to exercise.

    Two things have to be reset every test for the block to work:

    1. ``sys.modules["omnigent._build_info"] = None`` — Python's
       documented "this import raises ImportError" sentinel.
    2. ``delattr(omnigent, "_build_info")`` — once a previous test
       has done ``from omnigent import _build_info`` successfully
       (via its own ``sys.modules`` override with a fake module),
       Python *also* sets ``_build_info`` as an attribute on the
       ``omnigent`` package. Subsequent ``from omnigent import
       _build_info`` finds the attribute first and never consults
       ``sys.modules``, defeating the block above. Wiping the
       attribute restores the import to a clean state.

    Tests that need ``_build_info`` to appear present override (1)
    by ``monkeypatch.setitem(sys.modules, ..., fake_module)`` — that
    later setitem wins over the fixture's None entry.

    We deliberately do NOT monkeypatch ``_read_build_info`` itself.
    If we did, tests of the function would unknowingly call a stub
    instead of the real implementation (because ``from
    omnigent.update_check import _read_build_info`` inside a test
    body would resolve to the stubbed module attribute).
    """
    monkeypatch.setitem(sys.modules, "omnigent._build_info", None)
    # Wipe any leftover attribute from a previous test's successful
    # fake-module import. ``raising=False`` because the attribute may
    # not be set yet (fresh process, no test has imported _build_info).
    monkeypatch.delattr("omnigent._build_info", raising=False)


@pytest.fixture(autouse=True)
def _no_real_background_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the detached PyPI refresh so tests never spawn a real process.

    The wheel-check path fires :func:`_spawn_background_refresh` whenever
    its cached "latest version" is missing or stale. Left un-stubbed that
    would launch a ``python -c`` subprocess (and hit the network) during
    the test run. Replacing it with a no-op keeps the wheel-check tests
    hermetic; tests that assert the refresh *was* triggered patch it with
    their own recorder, which wins over this default.
    """
    monkeypatch.setattr("omnigent.update_check._spawn_background_refresh", lambda: None)


# A git URL with a recognizable host/path so assertions can match a
# substring of the formatted command. Not a real endpoint — never hit.
_FAKE_GIT_URL = "git+https://github.com/example-org/omnigent.git"
_FAKE_COMMIT = "abcdef1234567890abcdef1234567890abcdef12"


def _strip_rich_panel(text: str) -> str:
    """Collapse Rich panel/box-drawing output to one whitespace-normalized line.

    Rich wraps long URLs across multiple lines inside the panel
    (visually fine, but the substring we assert on isn't contiguous).
    Drop the box-drawing characters and collapse runs of whitespace
    so substring asserts work regardless of terminal width.

    :param text: Raw stderr captured from a Rich ``Panel`` print.
    :returns: A single space-normalized string containing the same
        words, in order, with no box characters.
    """
    import re

    no_box = re.sub(r"[╭╮╯╰─│]", "", text)
    return re.sub(r"\s+", " ", no_box).strip()


def _write_fake_dist_info(
    tmp_path: Path,
    *,
    installer: str | None = "uv",
    direct_url: dict[str, object] | None = None,
    uv_cache: dict[str, object] | None = None,
    dir_mtime_epoch: float | None = None,
) -> importlib.metadata.PathDistribution:
    """Build a real ``.dist-info/`` on disk and return a PathDistribution.

    The wheel-check path reads three files from the distribution:
    ``METADATA`` (for ``.version``), ``INSTALLER``, ``direct_url.json``,
    and ``uv_cache.json``. We write whichever of those the test cares
    about — the production code already handles missing files.

    :param tmp_path: pytest's per-test tmp dir.
    :param installer: Contents of the ``INSTALLER`` file, e.g.
        ``"uv"``. ``None`` to omit the file entirely (simulates
        installers that skip PEP 376).
    :param direct_url: Parsed dict to ``json.dumps`` into
        ``direct_url.json``. ``None`` to omit (simulates a registry
        install).
    :param uv_cache: Parsed dict to ``json.dumps`` into
        ``uv_cache.json``. ``None`` to omit (simulates a non-uv
        installer).
    :param dir_mtime_epoch: When provided, ``os.utime`` is used to
        backdate the dist-info dir's mtime to this Unix timestamp —
        this is the fallback signal when ``uv_cache.json`` is absent.
    :returns: A ``PathDistribution`` constructed against the dir.
    """
    dist_info = tmp_path / "omnigent-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: omnigent\nVersion: 0.1.0\n")
    if installer is not None:
        (dist_info / "INSTALLER").write_text(installer + "\n")
    if direct_url is not None:
        (dist_info / "direct_url.json").write_text(json.dumps(direct_url))
    if uv_cache is not None:
        (dist_info / "uv_cache.json").write_text(json.dumps(uv_cache))
    if dir_mtime_epoch is not None:
        import os

        os.utime(dist_info, (dir_mtime_epoch, dir_mtime_epoch))
    return importlib.metadata.PathDistribution(dist_info)


def test_read_wheel_info_uv_git_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``uv tool install git+<url>`` install populates every field."""
    install_time = time.time() - 3 * 86400  # 3 days ago
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        direct_url={
            "url": _FAKE_GIT_URL,
            "vcs_info": {"vcs": "git", "commit_id": _FAKE_COMMIT},
        },
        uv_cache={
            "timestamp": {"secs_since_epoch": int(install_time)},
            "commit": _FAKE_COMMIT,
        },
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    info = _read_installed_wheel_info()

    # Every field is populated for this install shape. If a future
    # production refactor drops one of these parsers, the field will
    # be None and this assertion will catch it.
    assert info is not None
    assert info.installer == "uv"
    assert info.detected_installer == "uv"
    assert info.vcs_url == _FAKE_GIT_URL
    assert info.commit_sha == _FAKE_COMMIT
    assert info.is_editable is False
    assert info.package_version == "0.1.0"
    # Install time came from uv_cache.json (preferred over mtime).
    assert abs(info.install_time_epoch - install_time) < 1.0


@pytest.mark.parametrize(
    "stored,expected",
    [
        # The bug: uv/pip redacted the bare SSH user
        # ``git`` to ``****``. We restore ``git`` so the reinstall
        # command can authenticate; without this it ssh's in as ``****``.
        (
            "git+ssh://****@github.com/omnigent-ai/omnigent.git",
            "git+ssh://git@github.com/omnigent-ai/omnigent.git",
        ),
        # Same redaction, but the URL was stored without the ``git+``
        # VCS prefix (the shape uv wrote on the machine in the report).
        (
            "ssh://****@github.com/omnigent-ai/omnigent.git",
            "ssh://git@github.com/omnigent-ai/omnigent.git",
        ),
        # Already-correct SSH user — must be left exactly as-is.
        (
            "git+ssh://git@github.com/org/repo.git",
            "git+ssh://git@github.com/org/repo.git",
        ),
        # HTTPS URL with no userinfo — nothing to repair.
        (
            "git+https://github.com/org/repo.git",
            "git+https://github.com/org/repo.git",
        ),
        # HTTPS with a partially-redacted ``user:****`` — that ``****``
        # stands in for a real password we must NOT reconstruct, and the
        # scheme isn't SSH anyway. Pass through untouched.
        (
            "git+https://user:****@github.com/org/repo.git",
            "git+https://user:****@github.com/org/repo.git",
        ),
        # SSH with a real (non-redacted) custom user — not the marker,
        # so it must be preserved rather than rewritten to ``git``.
        (
            "git+ssh://deploy@git.example.com/org/repo.git",
            "git+ssh://deploy@git.example.com/org/repo.git",
        ),
    ],
)
def test_unredact_ssh_userinfo(stored: str, expected: str) -> None:
    """``_unredact_ssh_userinfo`` restores a redacted SSH user to ``git``.

    Each case pins one branch of the repair logic. A failure on the
    first two cases means the SSH-redaction fix regressed and the reinstall
    command would again ssh in as the literal user ``****``. A failure
    on the remaining cases means the repair became too aggressive —
    rewriting a real user, a real password, or a non-SSH URL it should
    have left alone.
    """
    assert _unredact_ssh_userinfo(stored) == expected


def test_read_wheel_info_repairs_redacted_ssh_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An end-to-end repair: redacted ``direct_url.json`` → runnable command.

    Reproduces the SSH-redaction bug: uv recorded the SSH install URL with the
    user redacted to ``****``. We assert the repair survives all the
    way through ``_build_upgrade_suggestion`` into a runnable
    ``uv tool install --reinstall git+ssh://git@…`` command. If the
    repair were dropped, ``info.vcs_url`` (and therefore the suggested
    command) would still contain ``****@`` and the user would hit
    ``Permission denied (publickey)`` when they confirmed the prompt.
    """
    redacted_url = "ssh://****@github.com/omnigent-ai/omnigent.git"
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        direct_url={
            "url": redacted_url,
            "vcs_info": {"vcs": "git", "commit_id": _FAKE_COMMIT},
        },
        uv_cache={
            "timestamp": {"secs_since_epoch": int(time.time() - 3 * 86400)},
            "commit": _FAKE_COMMIT,
        },
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    info = _read_installed_wheel_info()
    assert info is not None
    # The redacted ``****@`` user was rewritten to the canonical
    # ``git@`` and normalized to the ``git+`` reinstall form.
    assert info.vcs_url == "git+ssh://git@github.com/omnigent-ai/omnigent.git"
    # ``****`` must not survive anywhere in the URL we'd display/run.
    assert "****" not in info.vcs_url

    suggestion = _build_upgrade_suggestion(info)
    # The command the user sees and (on confirm) we run is the repaired,
    # runnable form — not the broken ``****@`` URL.
    assert suggestion.runnable is True
    assert (
        suggestion.command
        == "uv tool install --reinstall git+ssh://git@github.com/omnigent-ai/omnigent.git"
    )


def test_read_wheel_info_editable_install_is_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pip install -e`` install is correctly detected as editable.

    Failure would mean the wheel check nags dev-clone users whose
    ``.git/`` isn't reachable from ``__file__`` (e.g. running from a
    sibling worktree). The is_editable flag is the only thing that
    suppresses that.
    """
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        direct_url={
            "url": "file:///Users/me/omnigent",
            "dir_info": {"editable": True},
        },
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    info = _read_installed_wheel_info()
    assert info is not None
    assert info.is_editable is True
    assert info.vcs_url is None


def test_read_wheel_info_pip_registry_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pip install omnigent`` from PyPI: no direct_url, mtime fallback."""
    # 2 days ago in epoch seconds.
    install_time = time.time() - 2 * 86400
    dist = _write_fake_dist_info(
        tmp_path,
        installer="pip",
        # No direct_url.json (pip omits it for PyPI installs).
        # No uv_cache.json (pip doesn't write one).
        dir_mtime_epoch=install_time,
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    info = _read_installed_wheel_info()

    # No direct_url means we know nothing about the source — vcs_url
    # and commit_sha are None. install_time falls back to mtime.
    assert info is not None
    assert info.installer == "pip"
    assert info.vcs_url is None
    assert info.commit_sha is None
    assert info.is_editable is False
    # ``time.time() - mtime`` should round-trip within filesystem
    # mtime precision (1s on most filesystems).
    assert abs(info.install_time_epoch - install_time) < 2.0


def test_read_wheel_info_returns_none_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returns None when ``_get_distribution`` says we aren't installed."""
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: None)
    assert _read_installed_wheel_info() is None


def test_read_wheel_info_handles_corrupt_direct_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt direct_url.json is tolerated — fields fall back to None."""
    install_time = time.time() - 86400 - 60  # just over 1 day
    dist_info = tmp_path / "omnigent-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: omnigent\nVersion: 0.1.0\n")
    (dist_info / "INSTALLER").write_text("uv\n")
    (dist_info / "direct_url.json").write_text("{not valid json")
    import os

    os.utime(dist_info, (install_time, install_time))
    dist = importlib.metadata.PathDistribution(dist_info)
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    info = _read_installed_wheel_info()
    # We still get a result back — corrupt direct_url just means we
    # don't know vcs_url / is_editable. Falling open here is correct:
    # mangled direct_url.json shouldn't disable the nag entirely.
    assert info is not None
    assert info.vcs_url is None
    assert info.is_editable is False


@pytest.mark.parametrize(
    "installer,vcs_url,expected_substring,expected_runnable",
    [
        # uv + git install — recommend ``uv tool install --reinstall``
        # with the original URL so the user pulls a fresh commit.
        ("uv", _FAKE_GIT_URL, f"uv tool install --reinstall {_FAKE_GIT_URL}", True),
        # uv + registry install — ``uv tool upgrade`` resolves from the
        # configured index. The user doesn't need to remember the spec.
        ("uv", None, "uv tool upgrade omnigent", True),
        # pip + git install — pip's ``--force-reinstall`` re-pulls the
        # spec; plain ``pip install`` would no-op because the version
        # tag (or HEAD) is the same string.
        ("pip", _FAKE_GIT_URL, f"pip install --force-reinstall {_FAKE_GIT_URL}", True),
        # pip + registry — the canonical upgrade incantation.
        ("pip", None, "pip install -U omnigent", True),
        # pipx — pipx has its own subcommands; we never recommend the
        # underlying pip command because pipx wraps the venv.
        ("pipx", _FAKE_GIT_URL, "pipx reinstall omnigent", True),
        ("pipx", None, "pipx upgrade omnigent", True),
        # poetry path — included for completeness; poetry is rare for
        # CLI tool installs but the format is documented.
        ("poetry", None, "poetry update omnigent", True),
        # Unknown installer WITH a VCS URL — we know the source but
        # not the tool, so the suggestion is prose ("reinstall X from
        # <url>"), not a command. Must be runnable=False so the
        # interactive prompt doesn't offer to execute prose.
        ("custom_tool", _FAKE_GIT_URL, f"reinstall omnigent from {_FAKE_GIT_URL}", False),
        # Unknown installer with no source URL — honest fallback.
        # Must also be runnable=False.
        (None, None, "reinstall omnigent from your original source", False),
    ],
)
def test_build_upgrade_suggestion_matrix(
    installer: str | None,
    vcs_url: str | None,
    expected_substring: str,
    expected_runnable: bool,
) -> None:
    """Upgrade-command formatting and runnable flag match the install shape.

    The ``runnable`` half of the assertion guards ``omni upgrade``: if a
    prose-fallback row ever flipped to runnable=True, the command would
    try to ``subprocess.run`` the literal string "reinstall omnigent
    from ...", which would error or worse (if a binary named "reinstall"
    happened to exist on PATH).
    """
    info = _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer=installer,
        vcs_url=vcs_url,
        commit_sha=None,
        is_editable=False,
        package_version="0.1.0",
        detected_installer=installer,
    )
    suggestion = _build_upgrade_suggestion(info)
    # Substring rather than equality because the command may include
    # leading words the user can copy-paste as a whole; we just need
    # to verify the right tool + the right action got picked.
    assert expected_substring in suggestion.command
    assert suggestion.runnable is expected_runnable


def test_pip_invocation_pins_to_running_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_pip_invocation`` targets ``sys.executable`` (with a bare fallback).

    Guards the PATH gotcha: a bare ``pip`` resolves against ``PATH`` and can
    upgrade a *different* environment than the one running ``omni``. Pinning
    to ``<sys.executable> -m pip`` keeps the upgrade in the running
    interpreter's environment.
    """
    monkeypatch.setattr(sys, "executable", "/opt/venv/bin/python")
    assert _pip_invocation() == "/opt/venv/bin/python -m pip"
    # An interpreter path with a space is shell-quoted so it survives the
    # ``shlex.split`` in ``_run_upgrade_command``.
    monkeypatch.setattr(sys, "executable", "/Applications/My App/python")
    assert _pip_invocation() == "'/Applications/My App/python' -m pip"
    # No interpreter path (frozen / embedded) → bare ``pip`` fallback.
    monkeypatch.setattr(sys, "executable", "")
    assert _pip_invocation() == "pip"


def test_pip_upgrade_suggestions_use_running_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both pip suggestions (registry + VCS) embed ``<python> -m pip``."""
    monkeypatch.setattr(sys, "executable", "/opt/venv/bin/python")

    def _info(vcs_url: str | None) -> _InstalledWheelInfo:
        return _InstalledWheelInfo(
            install_time_epoch=0.0,
            installer="pip",
            vcs_url=vcs_url,
            commit_sha=None,
            is_editable=False,
            package_version="0.1.0",
            detected_installer="pip",
        )

    assert (
        _build_upgrade_suggestion(_info(None)).command
        == "/opt/venv/bin/python -m pip install -U omnigent"
    )
    assert (
        _build_upgrade_suggestion(_info(_FAKE_GIT_URL)).command
        == f"/opt/venv/bin/python -m pip install --force-reinstall {_FAKE_GIT_URL}"
    )


def _point_cache_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the update-check cache at the per-test tmp dir."""
    monkeypatch.setattr("omnigent.update_check._CACHE_DIR", tmp_path)
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", tmp_path / ".update_check.json")


def test_wheel_check_no_nag_when_up_to_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Latest PyPI release == installed version → no nag.

    The whole point of the PyPI-based check: a fresh, fully-up-to-date
    install must never be nagged, no matter how long ago it was
    installed. (The old install-age check failed exactly here.)
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time(),
            commits_behind=0,
            kind="wheel",
            latest_version="0.1.0",  # == the fake dist's version
        )
    )
    dist = _write_fake_dist_info(tmp_path, installer="uv")
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    _run_installed_wheel_check()

    assert capsys.readouterr().err == ""


def test_wheel_check_nags_when_newer_release_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cached latest > installed → nag naming the release and ``omni upgrade``."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time(),
            commits_behind=0,
            kind="wheel",
            latest_version="0.2.0",
        )
    )
    dist = _write_fake_dist_info(tmp_path, installer="uv")
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    _run_installed_wheel_check()

    err = _strip_rich_panel(capsys.readouterr().err)
    # Names the new release, the installed version, and the command —
    # proves the message pipeline runs end to end.
    assert "omnigent 0.2.0 is out" in err
    assert "you have 0.1.0" in err
    assert "omni upgrade" in err
    # The notified version is stamped so the nag fires once per release.
    refreshed = _read_cache()
    assert refreshed is not None
    assert refreshed.last_notified_version == "0.2.0"


def test_wheel_check_fires_once_per_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Already-notified release → silent (no repeat nag).

    Regression for the old behavior people disliked: nagging on every
    single invocation. Once we've shown the notice for a version, it
    must stay quiet until an even newer one ships.
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time(),
            commits_behind=0,
            kind="wheel",
            latest_version="0.2.0",
            last_notified_version="0.2.0",  # already nagged for this one
        )
    )
    dist = _write_fake_dist_info(tmp_path, installer="uv")
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    _run_installed_wheel_check()

    assert capsys.readouterr().err == ""


def test_wheel_check_bails_for_editable_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Editable install → no nag and no background refresh.

    There's no PyPI release to compare an editable checkout against —
    the right answer is ``git pull``, not a reinstall — so the wheel
    path must bail before it would ever spawn a refresh.
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    spawned: list[bool] = []
    monkeypatch.setattr(
        "omnigent.update_check._spawn_background_refresh", lambda: spawned.append(True)
    )
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        direct_url={
            "url": "file:///Users/me/omnigent",
            "dir_info": {"editable": True},
        },
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    _run_installed_wheel_check()

    assert capsys.readouterr().err == ""
    assert spawned == []


def test_wheel_check_bails_when_distribution_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No installed distribution → silent no-op (running from source)."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: None)
    _run_installed_wheel_check()
    assert capsys.readouterr().err == ""


def test_wheel_check_refreshes_when_cache_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stale wheel cache → no nag this run, but a background refresh fires.

    The foreground never blocks on the network; it shows nothing when
    the cached latest is stale and instead kicks off the detached
    refresh so the *next* invocation has fresh data.
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time() - (_STALENESS_SECONDS + 60),
            commits_behind=0,
            kind="wheel",
            latest_version="0.1.0",
        )
    )
    spawned: list[bool] = []
    monkeypatch.setattr(
        "omnigent.update_check._spawn_background_refresh", lambda: spawned.append(True)
    )
    dist = _write_fake_dist_info(tmp_path, installer="uv")
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    _run_installed_wheel_check()

    assert capsys.readouterr().err == ""
    assert spawned == [True]


def test_wheel_check_env_var_disables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``OMNIGENT_NO_UPDATE_CHECK`` skips the wheel check entirely.

    Verifies the env-var gate works for the wheel path too — not just
    the clone path. Without this, users on uv-tool installs couldn't
    silence the nag.
    """
    monkeypatch.setenv("OMNIGENT_NO_UPDATE_CHECK", "1")
    _point_cache_at(tmp_path, monkeypatch)
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time(),
            commits_behind=0,
            kind="wheel",
            latest_version="0.2.0",
        )
    )
    dist = _write_fake_dist_info(tmp_path, installer="uv")
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    # No-clone scenario so the dispatcher routes to the wheel path.
    with patch("omnigent.update_check._find_repo_root", return_value=None):
        maybe_show_update_notice()

    assert capsys.readouterr().err == ""


def test_wheel_check_ignores_clone_kind_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stale clone-kind cache yields no wheel nag, only a refresh.

    Scenario: the user developed against a clone (cache ``kind="clone"``)
    then switched to a uv-tool install. The wheel path must not read a
    clone-kind cache as a "latest version" signal — it has none — so it
    shows nothing and kicks off a refresh to repopulate a wheel cache.
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time(),
            commits_behind=0,
            head_sha="some_sha",
            kind="clone",
        )
    )
    spawned: list[bool] = []
    monkeypatch.setattr(
        "omnigent.update_check._spawn_background_refresh", lambda: spawned.append(True)
    )
    dist = _write_fake_dist_info(tmp_path, installer="uv")
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    with patch("omnigent.update_check._find_repo_root", return_value=None):
        maybe_show_update_notice()

    assert capsys.readouterr().err == ""
    assert spawned == [True]


# ------------------------------------------------------------------
# PyPI version comparison + background refresh
# ------------------------------------------------------------------


def test_is_newer_pep440_ordering() -> None:
    """``_is_newer`` orders versions by PEP 440, not lexically."""
    from omnigent.update_check import _is_newer

    assert _is_newer("0.2.0", "0.1.0") is True
    assert _is_newer("0.10.0", "0.9.0") is True  # lexical "0.10" < "0.9" — must not fool us
    assert _is_newer("0.1.0", "0.1.0") is False
    assert _is_newer("0.1.0", "0.2.0") is False
    # Pre-releases sort below their final release.
    assert _is_newer("0.2.0", "0.2.0rc1") is True
    assert _is_newer("0.2.0rc1", "0.2.0") is False


def test_is_newer_tolerates_garbage() -> None:
    """A non-PEP-440 latest never crashes the check."""
    from omnigent.update_check import _is_newer

    assert _is_newer("not-a-version", "0.1.0") is True  # falls back to != and non-empty
    assert _is_newer("", "0.1.0") is False


class _FakeResp:
    """Minimal httpx.Response stand-in for the Simple-API parser."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        content_type: str = "application/vnd.pypi.simple.v1+json",
        json_body: object | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._json_body = json_body
        self.text = text

    def json(self) -> object:
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


_INDEX_ENV_VARS = ("OMNIGENT_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "PIP_INDEX_URL")


def _delenv_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every index env var (plus ``PIP_CONFIG_FILE``)."""
    for var in (*_INDEX_ENV_VARS, "PIP_CONFIG_FILE"):
        monkeypatch.delenv(var, raising=False)


def _clear_index_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start from the pypi.org default, ignoring the host's real config.

    Unsets the index env vars AND stubs the uv/pip config readers to
    ``""`` so a developer's real ``~/.config/uv/uv.toml`` / ``pip.conf``
    (e.g. a corporate mirror) can't leak into tests that assert the
    default index. Config-file resolution itself is covered by the
    dedicated ``test_resolve_index_url_from_*`` tests.
    """
    _delenv_index(monkeypatch)
    monkeypatch.setattr("omnigent.update_check._index_from_uv_config", lambda: "")
    monkeypatch.setattr("omnigent.update_check._index_from_pip_config", lambda: "")


def test_fetch_latest_version_pep691_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """PEP 691 ``versions`` → latest *stable* (pre/dev releases excluded)."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    captured: dict[str, object] = {}

    def _get(url: str, **kwargs: object) -> _FakeResp:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        return _FakeResp(json_body={"versions": ["0.1.0", "0.2.0", "0.3.0rc1", "0.9.0.dev1"]})

    monkeypatch.setattr(httpx, "get", _get)

    assert fetch_latest_version() == "0.2.0"
    # Default index + normalized project name + JSON Accept header.
    assert captured["url"] == "https://pypi.org/simple/omnigent/"
    assert captured["headers"] == {"Accept": "application/vnd.pypi.simple.v1+json"}


def test_fetch_latest_version_retries_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``attempts=2`` retries a transient connection error, then succeeds.

    Regression: the foreground ``omni upgrade`` shouldn't report the index as
    unreachable on a single momentary blip against a slow mirror.
    """
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    calls = {"n": 0}

    def _get(url: str, **_kw: object) -> _FakeResp:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient")
        return _FakeResp(json_body={"versions": ["0.1.0", "0.2.0"]})

    monkeypatch.setattr(httpx, "get", _get)

    assert fetch_latest_version(attempts=2) == "0.2.0"
    assert calls["n"] == 2  # retried exactly once


def test_fetch_latest_version_no_retry_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The background path (default ``attempts=1``) makes a single try."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    calls = {"n": 0}

    def _get(url: str, **_kw: object) -> _FakeResp:
        calls["n"] += 1
        raise httpx.ConnectError("transient")

    monkeypatch.setattr(httpx, "get", _get)

    assert fetch_latest_version() is None
    assert calls["n"] == 1


def test_fetch_latest_version_does_not_retry_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """A definitive non-200 reply is not retried, even with ``attempts=2``."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    calls = {"n": 0}

    def _get(url: str, **_kw: object) -> _FakeResp:
        calls["n"] += 1
        return _FakeResp(status_code=503)

    monkeypatch.setattr(httpx, "get", _get)

    assert fetch_latest_version(attempts=2) is None
    assert calls["n"] == 1


def test_fetch_latest_version_from_files_when_no_versions_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index without a ``versions`` key → derive from wheel/sdist filenames."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    body = {
        "files": [
            {"filename": "omnigent-0.1.0-py3-none-any.whl"},
            {"filename": "omnigent-0.2.0.tar.gz"},
            {"filename": "omnigent-0.3.0rc1-py3-none-any.whl"},  # prerelease → excluded
            {"filename": "not-a-distribution.txt"},  # ignored
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: _FakeResp(json_body=body))

    assert fetch_latest_version() == "0.2.0"


def test_fetch_latest_version_html_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PEP 503 HTML index (no JSON) → scrape filenames from the links."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="/p/omnigent-0.1.0-py3-none-any.whl#sha256=a">omnigent-0.1.0-py3-none-any.whl</a>'
        '<a href="/p/omnigent-0.2.0.tar.gz#sha256=b">omnigent-0.2.0.tar.gz</a>'
        "</body></html>"
    )
    monkeypatch.setattr(
        httpx, "get", lambda *_a, **_k: _FakeResp(content_type="text/html", text=html)
    )

    assert fetch_latest_version() == "0.2.0"


def test_fetch_latest_version_none_when_only_prereleases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only pre-releases available → no stable release, returns ``None``."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_a, **_k: _FakeResp(json_body={"versions": ["0.3.0rc1", "0.9.0.dev1"]}),
    )

    assert fetch_latest_version() is None


def test_fetch_latest_version_include_prereleases(monkeypatch: pytest.MonkeyPatch) -> None:
    """``include_prereleases=True`` surfaces an rc that the default hides."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_a, **_k: _FakeResp(json_body={"versions": ["0.1.0", "0.1.1rc1"]}),
    )

    assert fetch_latest_version() == "0.1.0"  # default: stable only
    assert fetch_latest_version(include_prereleases=True) == "0.1.1rc1"


def test_build_upgrade_suggestion_allow_prerelease() -> None:
    """``allow_prerelease`` appends each installer's allow-pre-releases flag."""

    def _info(installer: str, vcs_url: str | None = None) -> _InstalledWheelInfo:
        return _InstalledWheelInfo(
            install_time_epoch=0.0,
            installer=installer,
            vcs_url=vcs_url,
            commit_sha=None,
            is_editable=False,
            package_version="0.1.0",
            detected_installer=installer,
        )

    # Default (no pre) is unchanged.
    assert _build_upgrade_suggestion(_info("uv")).command == "uv tool upgrade omnigent"
    # uv / pip registry installs get the right flag appended.
    assert (
        _build_upgrade_suggestion(_info("uv"), allow_prerelease=True).command
        == "uv tool upgrade omnigent --prerelease allow"
    )
    # pip pins the upgrade to the running interpreter (``<python> -m pip``)
    # so it can't land in some other env whose ``pip`` shadows ours on PATH.
    assert (
        _build_upgrade_suggestion(_info("pip"), allow_prerelease=True).command
        == f"{_pip_invocation()} install -U omnigent --pre"
    )
    # VCS install carries the flag too.
    assert (
        _build_upgrade_suggestion(
            _info("uv", vcs_url="git+https://x/omnigent.git"), allow_prerelease=True
        ).command
        == "uv tool install --reinstall git+https://x/omnigent.git --prerelease allow"
    )


def test_fetch_latest_version_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network error and non-200 both return ``None`` (never raise)."""
    import httpx

    from omnigent.update_check import fetch_latest_version

    _clear_index_env(monkeypatch)

    def _boom(*_a: object, **_k: object) -> object:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", _boom)
    assert fetch_latest_version() is None

    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: _FakeResp(status_code=404))
    assert fetch_latest_version() is None


def test_resolve_index_url_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_resolve_index_url`` follows env precedence and defaults to pypi.org/simple."""
    from omnigent.update_check import _resolve_index_url

    _clear_index_env(monkeypatch)
    assert _resolve_index_url() == "https://pypi.org/simple"

    monkeypatch.setenv("PIP_INDEX_URL", "https://pip.example/simple/")
    assert _resolve_index_url() == "https://pip.example/simple"

    # uv outranks pip; explicit override outranks everything.
    monkeypatch.setenv("UV_INDEX_URL", "https://uv.example/simple/")
    assert _resolve_index_url() == "https://uv.example/simple"
    monkeypatch.setenv("OMNIGENT_INDEX_URL", "https://override.example/simple")
    assert _resolve_index_url() == "https://override.example/simple"

    # Multiple whitespace/comma-separated URLs → the first (primary) index.
    monkeypatch.setenv("OMNIGENT_INDEX_URL", "https://a.example/simple, https://b.example/simple")
    assert _resolve_index_url() == "https://a.example/simple"


def test_resolve_index_url_from_uv_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var → uv.toml's ``index-url`` is used (corp-mirror-in-a-file case)."""
    from omnigent.update_check import _resolve_index_url

    _delenv_index(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Isolate the uv path under test from any real pip.conf on the box.
    monkeypatch.setattr("omnigent.update_check._index_from_pip_config", lambda: "")
    uv_toml = tmp_path / "uv" / "uv.toml"
    uv_toml.parent.mkdir(parents=True)
    uv_toml.write_text('index-url = "https://uvcfg.example/simple/"\n')

    assert _resolve_index_url() == "https://uvcfg.example/simple"


def test_resolve_index_url_from_uv_default_index_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``[[index]]`` marked ``default = true`` wins; a plain one is ignored."""
    from omnigent.update_check import _resolve_index_url

    _delenv_index(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.update_check._index_from_pip_config", lambda: "")
    uv_toml = tmp_path / "uv" / "uv.toml"
    uv_toml.parent.mkdir(parents=True)
    uv_toml.write_text(
        '[[index]]\nname = "pub"\nurl = "https://pub.example/simple"\n\n'
        '[[index]]\nname = "corp"\nurl = "https://corp.example/simple"\ndefault = true\n'
    )

    assert _resolve_index_url() == "https://corp.example/simple"


def test_resolve_index_url_ignores_non_default_uv_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A supplementary ``[[index]]`` (no ``default``) does not override pypi.org."""
    from omnigent.update_check import _resolve_index_url

    _delenv_index(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.update_check._index_from_pip_config", lambda: "")
    uv_toml = tmp_path / "uv" / "uv.toml"
    uv_toml.parent.mkdir(parents=True)
    uv_toml.write_text('[[index]]\nname = "extra"\nurl = "https://extra.example/simple"\n')

    assert _resolve_index_url() == "https://pypi.org/simple"


def test_resolve_index_url_from_pip_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv has nothing → pip.conf's ``[global] index-url`` is used."""
    from omnigent.update_check import _resolve_index_url

    _delenv_index(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("omnigent.update_check._index_from_uv_config", lambda: "")
    pip_conf = tmp_path / "pip" / "pip.conf"
    pip_conf.parent.mkdir(parents=True)
    pip_conf.write_text("[global]\nindex-url = https://pipcfg.example/simple/\n")

    assert _resolve_index_url() == "https://pipcfg.example/simple"


def test_resolve_index_url_env_beats_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index env var takes precedence over a configured one."""
    from omnigent.update_check import _resolve_index_url

    _delenv_index(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    uv_toml = tmp_path / "uv" / "uv.toml"
    uv_toml.parent.mkdir(parents=True)
    uv_toml.write_text('index-url = "https://uvcfg.example/simple"\n')
    monkeypatch.setenv("UV_INDEX_URL", "https://env.example/simple")

    assert _resolve_index_url() == "https://env.example/simple"


def test_refresh_update_cache_writes_latest_and_preserves_notified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``refresh_update_cache`` records the PyPI latest, keeping the notified stamp.

    Preserving ``last_notified_version`` is what stops a routine refresh
    from re-arming a notice the user already saw.
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    # A wheel cache that already nagged for 0.2.0.
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time() - 10_000,
            commits_behind=0,
            kind="wheel",
            latest_version="0.2.0",
            last_notified_version="0.2.0",
        )
    )
    dist = _write_fake_dist_info(tmp_path, installer="uv")
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    monkeypatch.setattr("omnigent.update_check._find_repo_root", lambda: None)
    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", lambda: "0.2.0")

    from omnigent.update_check import refresh_update_cache

    refresh_update_cache()

    refreshed = _read_cache()
    assert refreshed is not None
    assert refreshed.kind == "wheel"
    assert refreshed.latest_version == "0.2.0"
    assert refreshed.last_notified_version == "0.2.0"


def test_refresh_update_cache_noop_for_clone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a dev clone, the PyPI refresh does nothing (git path owns it)."""
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    monkeypatch.setattr("omnigent.update_check._find_repo_root", lambda: tmp_path)

    def _must_not_fetch() -> str:
        raise AssertionError("PyPI fetch attempted in a dev clone")

    monkeypatch.setattr("omnigent.update_check.fetch_latest_version", _must_not_fetch)

    from omnigent.update_check import refresh_update_cache

    refresh_update_cache()  # must not raise


def test_upgrade_command_for_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``upgrade_command_for_installed`` maps the install shape to a command."""
    from omnigent.update_check import upgrade_command_for_installed

    dist = _write_fake_dist_info(tmp_path, installer="uv")  # registry uv install
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    suggestion = upgrade_command_for_installed()
    assert suggestion is not None
    assert suggestion.command == "uv tool upgrade omnigent"
    assert suggestion.runnable is True

    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: None)
    assert upgrade_command_for_installed() is None


def test_wheel_info_prefers_build_info_over_uv_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_build_info`` install_time wins over ``uv_cache.json`` when both exist.

    Scenario: a uv-built wheel has both files. The build_py hook
    wrote ``_build_info.py`` with the *build* timestamp (the moment
    the wheel was produced); uv later wrote ``uv_cache.json`` with
    the *cache* timestamp (when the wheel was unpacked into the
    tool dir, possibly weeks later). The build moment is the
    semantically correct signal for "how stale is this code" — if
    this test ever flips, users on slow-network reinstalls would
    see false-fresh nags. The commit_sha priority is symmetric:
    the build-baked SHA is the ground truth.
    """
    build_time = time.time() - 10 * 86400  # 10 days ago (build moment)
    uv_cache_time = time.time() - 1 * 86400  # 1 day ago (extracted recently)
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        uv_cache={
            "timestamp": {"secs_since_epoch": int(uv_cache_time)},
            "commit": "uv_cache_commit_sha",
        },
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    monkeypatch.setattr(
        "omnigent.update_check._read_build_info",
        lambda: (build_time, "build_info_commit_sha"),
    )

    info = _read_installed_wheel_info()
    assert info is not None
    # Build time, not uv_cache time — proves the priority is correct.
    assert abs(info.install_time_epoch - build_time) < 1.0
    # commit_sha from _build_info, not the uv_cache fallback.
    assert info.commit_sha == "build_info_commit_sha"


def test_wheel_info_falls_back_to_uv_cache_when_build_info_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``_build_info`` is unavailable, ``uv_cache.json`` takes over.

    This is the path source checkouts hit (no build hook ran) and
    also any wheel published by a different build system that
    doesn't include our ``setup.py`` hook output. If the fallback
    breaks, those installs lose the install-age signal entirely.
    """
    uv_cache_time = time.time() - 2 * 86400
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        uv_cache={
            "timestamp": {"secs_since_epoch": int(uv_cache_time)},
            "commit": "uv_cache_sha",
        },
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    # _build_info import returns None — simulates a source checkout.
    monkeypatch.setattr("omnigent.update_check._read_build_info", lambda: None)

    info = _read_installed_wheel_info()
    assert info is not None
    assert abs(info.install_time_epoch - uv_cache_time) < 1.0
    assert info.commit_sha == "uv_cache_sha"


def test_wheel_info_falls_back_to_mtime_when_only_dist_info_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No _build_info, no uv_cache — fall back to dist-info mtime."""
    mtime = time.time() - 5 * 86400
    dist = _write_fake_dist_info(tmp_path, installer="pip", dir_mtime_epoch=mtime)
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    monkeypatch.setattr("omnigent.update_check._read_build_info", lambda: None)

    info = _read_installed_wheel_info()
    assert info is not None
    # mtime precision is fs-dependent; 2s tolerance covers macOS/Linux.
    assert abs(info.install_time_epoch - mtime) < 2.0


def test_wheel_info_build_info_empty_sha_does_not_clobber_direct_url_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty ``COMMIT_SHA`` from ``_build_info`` doesn't blank a direct_url SHA.

    Scenario: the wheel was built in an environment without ``git``
    available, so ``setup.py`` baked ``COMMIT_SHA = ""``. But the
    install method (``uv tool install git+<url>``) recorded a real
    commit in ``direct_url.json``. The direct_url SHA must survive
    — it's the only commit info we have.
    """
    build_time = time.time() - 3 * 86400
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        direct_url={
            "url": _FAKE_GIT_URL,
            "vcs_info": {"vcs": "git", "commit_id": _FAKE_COMMIT},
        },
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)
    monkeypatch.setattr(
        "omnigent.update_check._read_build_info",
        lambda: (build_time, ""),  # empty SHA from a no-git build
    )

    info = _read_installed_wheel_info()
    assert info is not None
    # Build time wins for age signal, but the direct_url SHA wasn't
    # clobbered by the empty build-info SHA.
    assert abs(info.install_time_epoch - build_time) < 1.0
    assert info.commit_sha == _FAKE_COMMIT


def test_read_build_info_returns_none_when_module_missing() -> None:
    """``_read_build_info`` returns None when import fails.

    The autouse fixture already blocked
    ``sys.modules["omnigent._build_info"]`` by setting it to
    None — Python's documented "this import fails" sentinel. The
    function catches the ImportError and returns None. Source
    checkouts that have never been built sit in this state (no
    on-disk ``_build_info.py`` either), so this is the path most
    real users on a clone hit.
    """
    assert _read_build_info() is None


def test_read_build_info_returns_values_when_module_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_read_build_info`` reads the constants when the module exists.

    Override the autouse fixture's sys.modules blocker with a real
    fake module so the production ``from omnigent import
    _build_info`` import succeeds and the function returns its
    values. Verifies the actual import path, not a stubbed shortcut.
    """
    import types

    fake_module = types.ModuleType("omnigent._build_info")
    fake_module.BUILD_TIME_EPOCH = 1779000000  # type: ignore[attr-defined]
    fake_module.COMMIT_SHA = "deadbeef" * 5  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omnigent._build_info", fake_module)

    result = _read_build_info()
    assert result is not None
    ts, sha = result
    # Build time round-trips as a float exactly (no rounding loss).
    assert ts == 1779000000.0
    assert sha == "deadbeef" * 5


def test_read_build_info_tolerates_malformed_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted ``_build_info`` (missing attrs) is treated as absent.

    Production code must keep working even with a half-written or
    hand-edited ``_build_info.py`` — falling back to other signals
    is always better than crashing the CLI's startup banner.
    """
    import types

    fake_module = types.ModuleType("omnigent._build_info")
    # Missing BUILD_TIME_EPOCH and COMMIT_SHA — AttributeError when read.
    monkeypatch.setitem(sys.modules, "omnigent._build_info", fake_module)

    assert _read_build_info() is None


def test_legacy_cache_without_kind_field_defaults_to_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache written before the ``kind`` field existed reads as ``clone``.

    Backward-compat for users whose ``~/.omnigent/.update_check.json``
    was written by a previous version of this module. If this fails,
    the dispatcher would treat legacy caches as a different kind and
    re-do the (slow) ``git fetch`` on every invocation.
    """
    cache_file = tmp_path / ".update_check.json"
    cache_file.write_text(
        json.dumps(
            {
                "last_check_epoch": 1716100000.0,
                "commits_behind": 2,
                "head_sha": "abc",
            }
        )
    )
    monkeypatch.setattr("omnigent.update_check._CACHE_FILE", cache_file)

    entry = _read_cache()
    assert entry is not None
    assert entry.kind == "clone"
    assert entry.commits_behind == 2


# ------------------------------------------------------------------
# Upgrade command runner (_run_upgrade_command)
# ------------------------------------------------------------------


def test_run_upgrade_command_invokes_subprocess(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``_run_upgrade_command`` shells out via subprocess.run with shlex tokens.

    Direct test of the helper because the higher-level tests above
    stub it out. The string must be tokenized with ``shlex.split``
    (no ``shell=True``), and the helper must return the subprocess's
    exit code unchanged.
    """
    import subprocess as _subprocess

    from rich.console import Console

    from omnigent.update_check import _run_upgrade_command

    captured_args: list[list[str]] = []

    class _FakeResult:
        returncode = 7  # arbitrary value to prove it propagates

    def _fake_run(args: list[str], check: bool = False) -> _FakeResult:
        captured_args.append(args)
        # Loud failure if anyone ever flips us to shell=True.
        assert isinstance(args, list)
        assert check is False
        return _FakeResult()

    monkeypatch.setattr(_subprocess, "run", _fake_run)

    console = Console(stderr=True)
    code = _run_upgrade_command(
        "uv tool install --reinstall git+https://example.test/repo.git", console
    )

    # shlex.split tokenization is the contract — a single
    # whitespace-joined string would let an installer interpret
    # spaces inside the URL as separate args.
    assert captured_args == [
        [
            "uv",
            "tool",
            "install",
            "--reinstall",
            "git+https://example.test/repo.git",
        ]
    ]
    # The exit code must propagate unchanged so the caller's
    # success/failure branching works.
    assert code == 7
    err = _strip_rich_panel(capsys.readouterr().err)
    # User-visible "Running:" status so they know what's executing.
    assert "Running:" in err
    assert "uv tool install --reinstall" in err


def test_run_upgrade_command_returns_minus_one_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Binary missing from PATH → ``_run_upgrade_command`` returns -1 and prints why.

    The -1 sentinel is what the caller distinguishes from a real
    non-zero exit code. The user-facing error must name the failure
    mode (the OS error) so they can diagnose (PATH issue, broken
    installer install, etc.) rather than seeing a generic "exited
    with status -1" that hides the actual cause.
    """
    import subprocess as _subprocess

    from rich.console import Console

    from omnigent.update_check import _run_upgrade_command

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError(2, "No such file or directory: 'uv'")

    monkeypatch.setattr(_subprocess, "run", _raise)

    console = Console(stderr=True)
    code = _run_upgrade_command("uv tool upgrade omnigent", console)

    # -1 distinguishes "couldn't start" from "ran and exited
    # non-zero" — the latter would be the subprocess's own code.
    assert code == -1
    err = _strip_rich_panel(capsys.readouterr().err)
    assert "Upgrade failed to start" in err
    # The OS error text must be surfaced so the user can act on it.
    assert "No such file or directory" in err


# ------------------------------------------------------------------
# Version line formatting (cli._format_version)
# ------------------------------------------------------------------


def test_format_version_falls_back_to_bare_version_when_build_info_missing() -> None:
    """Without ``_build_info``, ``--version`` prints ``omnigent <ver>``.

    Source checkouts (and any wheel built without our setup.py hook)
    hit this path. The line must remain stable across releases —
    scripts that grep for "omnigent X.Y.Z" must keep working.
    """
    # The autouse fixture has already blocked sys.modules['omnigent._build_info']
    # via the None sentinel, so _read_build_info returns None.
    from omnigent.cli import _format_version

    out = _format_version()
    # Exact prefix match — if the format ever gains extra content
    # in the no-build-info case, scripts that look for "omnigent
    # X.Y.Z" at the start of the line still work.
    assert out.startswith("omnigent ")
    # The version comes from importlib.metadata; just check it's
    # non-empty and contains no parenthesized build-info suffix.
    assert "(" not in out
    assert "built" not in out


def test_format_version_includes_sha_and_build_time_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``_build_info`` present, ``--version`` includes SHA + UTC build time.

    Picks a known epoch (2026-05-21 14:34:45 UTC) and a known SHA
    to assert the exact rendering. If either changes shape, the
    assertion catches it — bug reports rely on this string being
    copy-pasteable.
    """
    import types

    from omnigent.cli import _format_version

    fake_module = types.ModuleType("omnigent._build_info")
    # 2026-05-20T14:34:45Z exactly.
    fake_module.BUILD_TIME_EPOCH = 1779287685  # type: ignore[attr-defined]
    fake_module.COMMIT_SHA = "0123456789abcdef" + "0" * 24  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omnigent._build_info", fake_module)

    out = _format_version()
    # Short SHA = first 8 chars of the full SHA.
    assert "(01234567, " in out
    # UTC ISO-8601 with the "Z" suffix.
    assert "built 2026-05-20T14:34:45Z" in out


def test_format_version_omits_sha_when_build_info_has_empty_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build with no ``git`` (empty SHA) still prints the build time.

    Builds inside Docker layers / sdists that have no ``git`` will
    bake ``COMMIT_SHA = ""``. The build time is still useful — the
    line should include it but skip the SHA segment cleanly rather
    than render ``(, built ...)``.
    """
    import types

    from omnigent.cli import _format_version

    fake_module = types.ModuleType("omnigent._build_info")
    fake_module.BUILD_TIME_EPOCH = 1779287685  # type: ignore[attr-defined]
    fake_module.COMMIT_SHA = ""  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "omnigent._build_info", fake_module)

    out = _format_version()
    assert "built 2026-05-20T14:34:45Z" in out
    # Critical: no orphan comma or empty parens from the missing SHA.
    assert "(, " not in out
    assert "()" not in out


# --- VCS (git) install handling: passive notice + URL parsing -----------------


def test_split_vcs_url_strips_prefix_and_separates_revision() -> None:
    """``git+<url>@<rev>`` splits into the bare repo URL and the revision."""
    from omnigent.update_check import _split_vcs_url

    assert _split_vcs_url("git+https://github.com/o/omnigent.git") == (
        "https://github.com/o/omnigent.git",
        None,
    )
    assert _split_vcs_url("git+https://github.com/o/omnigent.git@main") == (
        "https://github.com/o/omnigent.git",
        "main",
    )


def test_split_vcs_url_strips_pip_fragment() -> None:
    """A ``#egg=`` / ``#subdirectory=`` fragment is dropped from URL and ref.

    Left on, the fragment would ride along into ``git ls-remote`` and match no
    ref, silently making the commit comparison indeterminate.
    """
    from omnigent.update_check import _split_vcs_url

    assert _split_vcs_url("git+https://github.com/o/omnigent.git#egg=omnigent") == (
        "https://github.com/o/omnigent.git",
        None,
    )
    assert _split_vcs_url("git+https://github.com/o/omnigent.git@main#subdirectory=pkg") == (
        "https://github.com/o/omnigent.git",
        "main",
    )


def test_split_vcs_url_ssh_userinfo_is_not_a_revision() -> None:
    """An ``@`` in SSH userinfo (``git@host``) must not be read as a revision."""
    from omnigent.update_check import _split_vcs_url

    assert _split_vcs_url("git+ssh://git@github.com/o/omnigent.git") == (
        "ssh://git@github.com/o/omnigent.git",
        None,
    )
    # …but a real trailing revision still parses, even with SSH userinfo.
    assert _split_vcs_url("git+ssh://git@github.com/o/omnigent.git@v1") == (
        "ssh://git@github.com/o/omnigent.git",
        "v1",
    )


def test_wheel_check_skips_vcs_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A git/VCS install is never nagged about a PyPI version.

    Its version string is frozen at the source branch's (e.g. an unbumped
    ``main`` still saying ``0.1.0``), so a PyPI-version comparison would
    fire forever — even on a build that is *ahead* of the latest release.
    The passive notice must bail for vcs installs just like editable ones.
    """
    monkeypatch.delenv("OMNIGENT_NO_UPDATE_CHECK", raising=False)
    _point_cache_at(tmp_path, monkeypatch)
    _write_cache(
        _CacheEntry(
            last_check_epoch=time.time(),
            commits_behind=0,
            kind="wheel",
            latest_version="0.2.0",  # strictly newer than the dist's 0.1.0
        )
    )
    dist = _write_fake_dist_info(
        tmp_path,
        installer="uv",
        direct_url={"url": _FAKE_GIT_URL, "vcs_info": {"vcs": "git", "commit_id": _FAKE_COMMIT}},
    )
    monkeypatch.setattr("omnigent.update_check._get_distribution", lambda: dist)

    _run_installed_wheel_check()

    assert capsys.readouterr().err == ""
