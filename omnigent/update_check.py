"""Lightweight CLI update reminder.

Two install shapes are supported:

* **Dev clone** (``git clone … && uv sync`` / ``pip install -e .``): we
  walk up from this module to find a ``.git/`` directory, then run
  ``git fetch`` and ``rev-list`` to count how many commits ``HEAD`` is
  behind ``origin/main`` (or ``origin/master``). The result is cached
  to ``~/.omnigent/.update_check.json`` so the (potentially slow)
  ``git fetch`` only runs once per staleness window. The notice points
  the user at ``git pull``.

* **Installed wheel** (``uv tool install omnigent``,
  ``pip install omnigent``, ``pipx install``, Homebrew, etc.): no clone
  is reachable on disk, so we compare the installed version against the
  latest release on the *configured package index* (via the Simple
  Repository API — :func:`fetch_latest_version`) and nag *only when a
  strictly newer release exists* — never merely because the install is
  old. The index is resolved from ``OMNIGENT_INDEX_URL`` /
  ``UV_INDEX_URL`` / ``PIP_INDEX_URL`` or, failing those, the uv/pip
  *config files* (``uv.toml`` / ``pip.conf``); default pypi.org. So it
  works on corporate mirrors and air-gapped networks — even when the
  mirror is configured in a file rather than an env var — and stays
  consistent with what ``omni upgrade`` actually pulls. To avoid adding
  latency to the hot
  path, the foreground only ever reads the cached "latest version" and
  prints from it; the (network) lookup runs in a detached background
  process (:func:`refresh_update_cache`) that refreshes the cache for the
  next invocation. The notice fires once per new release (tracked via
  ``last_notified_version``) and points the user at ``omni upgrade``.

The dispatcher in ``maybe_show_update_notice`` picks the shape based on
whether a ``.git/`` directory is reachable from this module's path.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import tomllib

if TYPE_CHECKING:
    # Imported only for type hints; the heavy/optional imports remain lazy
    # at runtime so importing this module stays cheap.
    from collections.abc import Iterable

    import httpx
    from packaging.version import Version
    from rich.console import Console

_ENV_SKIP = "OMNIGENT_NO_UPDATE_CHECK"
_CACHE_DIR = Path.home() / ".omnigent"
_CACHE_FILE = _CACHE_DIR / ".update_check.json"
_STALENESS_SECONDS = 4 * 60 * 60  # 4 hours
_GIT_TIMEOUT_SECONDS = 5
_DIST_NAME = "omnigent"
# "Is a newer release out?" is answered against the *configured* package
# index via the Simple Repository API (PEP 503/691) — the universal
# protocol every index speaks (pypi.org, a corporate mirror, devpi,
# Artifactory, the Databricks proxy). We deliberately do NOT use PyPI's
# JSON API (`/pypi/<name>/json`): that is Warehouse-specific and 404s on
# mirrors, so it would silently never work on air-gapped / mirror-only
# networks. Querying the same index the user installs from also keeps the
# notice consistent with what ``omni upgrade`` (uv/pip) actually pulls.
_DEFAULT_INDEX_URL = "https://pypi.org/simple"
# Env vars that point at the default index, in precedence order. The
# explicit ``OMNIGENT_INDEX_URL`` wins; then uv's and pip's. When none is
# set, :func:`_resolve_index_url` falls back to uv/pip *config files*
# (uv.toml / pip.conf), so a mirror configured there is honored too.
# Credentials embedded in the URL are honored (httpx applies them), so
# authenticated mirrors work transparently.
_INDEX_ENV_VARS = ("OMNIGENT_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "PIP_INDEX_URL")
# PEP 691 JSON content type to request (falls back to PEP 503 HTML).
_SIMPLE_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"
# Keep the background index lookup snappy. It runs detached so it never
# blocks the CLI, but a tight timeout still bounds the orphan's lifetime.
_INDEX_TIMEOUT_SECONDS = 3.0
# The foreground ``omni upgrade`` is user-initiated and worth waiting on, so it
# uses a more forgiving timeout (and one retry) than the background refresh — a
# briefly slow mirror shouldn't make ``omni upgrade --check`` spuriously report
# "couldn't reach the index".
_UPGRADE_INDEX_TIMEOUT_SECONDS = 10.0
# The placeholder that PEP 610 tooling (pip, newer uv) writes into
# ``direct_url.json`` in place of a URL's userinfo. See
# ``_unredact_ssh_userinfo`` for why we have to repair it.
_REDACTED_USERINFO = "****"


@dataclass
class _CacheEntry:
    """Deserialized update-check cache.

    :param last_check_epoch: Unix timestamp of the last ``git fetch``
        check, e.g. ``1716100000.0``.
    :param commits_behind: Number of commits HEAD is behind
        the upstream branch, e.g. ``3``.
    :param head_sha: The HEAD commit hash at the time the cache was
        written, e.g. ``"abc123..."``.  Used to detect when the user
        has pulled (HEAD moves) so the stale count can be rechecked
        cheaply without a fresh ``git fetch``.
    :param kind: Which detection path wrote this entry —
        ``"clone"`` for the dev-clone (``git fetch``) path or
        ``"wheel"`` for the installed-wheel (PyPI) path. Defaults to
        ``"clone"`` so legacy caches written before this field existed
        are treated as clone caches.
    :param latest_version: The latest release version seen on PyPI for
        the wheel path, e.g. ``"0.2.0"``. Empty string when unknown
        (clone path, or no successful PyPI lookup yet).
    :param last_notified_version: The version the wheel-path notice was
        last shown for, e.g. ``"0.2.0"``. Used to fire the "update
        available" notice exactly once per new release rather than on
        every invocation. Empty string when no notice has been shown.
    """

    last_check_epoch: float
    commits_behind: int
    head_sha: str = ""
    kind: str = "clone"
    latest_version: str = ""
    last_notified_version: str = ""


@dataclass
class _InstalledWheelInfo:
    """Metadata read from the installed package's ``.dist-info/``.

    Populated by ``_read_installed_wheel_info``. ``None`` when our
    distribution is not installed (e.g. running from the source tree
    without any ``pip``/``uv`` install).

    :param install_time_epoch: Unix timestamp for when the package
        was installed, e.g. ``1779311637.0``. Taken from
        ``uv_cache.json`` when available (most accurate; uv records
        the build/cache time), otherwise falls back to the
        ``.dist-info/`` directory's filesystem mtime (universal
        across installers).
    :param installer: The lowercase installer name from the
        ``INSTALLER`` file (PEP 376), e.g. ``"uv"`` / ``"pip"`` /
        ``"poetry"``. ``None`` if the file is missing or empty.
        Note: pipx delegates to pip so its ``INSTALLER`` reads
        ``"pip"``; we additionally check ``sys.prefix`` for the
        ``/pipx/venvs/`` pattern to detect it.
    :param vcs_url: The git URL recorded in ``direct_url.json``
        (PEP 610) when the install came from a VCS source, e.g.
        ``"git+https://github.com/omnigent-ai/omnigent.git"``.
        ``None`` for registry installs (no ``direct_url.json``) or
        URL installs without ``vcs_info``.
    :param commit_sha: The pinned commit SHA recorded by uv or pip
        at install time, e.g. ``"010cf77c3..."``. ``None`` for
        registry installs. Populated from ``direct_url.json``'s
        ``vcs_info.commit_id`` (PEP 610) or, failing that, from
        ``uv_cache.json``'s ``commit`` field.
    :param is_editable: ``True`` when ``direct_url.json`` records
        ``dir_info.editable``, i.e. ``pip install -e`` / editable
        ``uv tool install``. The wheel-check path bails for these.
    :param package_version: The installed package version from
        ``METADATA``, e.g. ``"0.1.0"``. Surfaced in the nag text.
    :param detected_installer: The effective installer label we use
        for picking the upgrade command — same as ``installer`` for
        most cases, but ``"pipx"`` when the pipx venv path heuristic
        fires even though ``INSTALLER`` says ``"pip"``.
    :param extras: Optional extras recorded by the installer, e.g.
        ``["all"]``. Empty when the installer does not preserve them
        (e.g. ``pip``) or when none were requested.
    """

    install_time_epoch: float
    installer: str | None
    vcs_url: str | None
    commit_sha: str | None
    is_editable: bool
    package_version: str
    detected_installer: str | None
    extras: tuple[str, ...] = ()


def maybe_show_update_notice() -> None:
    """Print an update reminder to stderr if this install is stale.

    Dispatches to either the dev-clone path (``.git/`` reachable from
    this module) or the installed-wheel path (``uv tool install`` / pip
    / pipx). Safe to call unconditionally — silently returns when the
    check is disabled by env var, when metadata is missing, when
    ``git`` is unavailable, or when any I/O error occurs.
    """
    if os.environ.get(_ENV_SKIP):
        return

    repo_root = _find_repo_root()
    if repo_root is not None:
        _run_dev_clone_check(repo_root)
    else:
        _run_installed_wheel_check()


def _run_dev_clone_check(repo_root: Path) -> None:
    """Run the dev-clone update check (``git fetch`` + ``rev-list``).

    Caches the commits-behind count to avoid running ``git fetch`` on
    every CLI invocation. Re-counts cheaply when ``HEAD`` has moved
    (the user ran ``git pull``). Any failure is swallowed — the check
    must never break the CLI.

    :param repo_root: Absolute path to the dev clone's Git repo root,
        e.g. ``Path("/Users/me/omnigent")``.
    """
    try:
        cached = _read_cache()
        if cached is not None and cached.kind == "clone" and not _is_stale(cached):
            behind = cached.commits_behind
            # If HEAD moved since the cache was written (e.g. the user
            # ran ``git pull``), do a cheap local recount — no fetch.
            if behind > 0 and cached.head_sha:
                cur_sha = _get_head_sha(repo_root)
                if cur_sha and cur_sha != cached.head_sha:
                    recounted = _local_rev_list_count(repo_root)
                    if recounted is not None:
                        behind = recounted
                        _write_cache(
                            _CacheEntry(
                                last_check_epoch=cached.last_check_epoch,
                                commits_behind=behind,
                                head_sha=cur_sha,
                            )
                        )
            if behind > 0:
                _print_notice(behind)
            return

        result = _run_check(repo_root)
        if result is None:
            # git unavailable or fetch/rev-list failed — record a
            # fresh timestamp so we don't retry on every invocation.
            _write_cache(_CacheEntry(last_check_epoch=time.time(), commits_behind=0, head_sha=""))
            return

        _write_cache(result)
        if result.commits_behind > 0:
            _print_notice(result.commits_behind)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        # Never let the update check break the CLI.
        pass


def _run_installed_wheel_check() -> None:
    """Run the installed-wheel update check (PyPI-version-driven).

    Compares the installed version against the latest release recorded
    in the cache and prints an "update available" notice when a strictly
    newer release exists — and only once per new release, tracked via
    ``last_notified_version``. The notice points the user at
    ``omni upgrade``.

    The foreground never touches the network: it reads the cached latest
    version (written by :func:`refresh_update_cache`) and, when that
    cache is missing or stale, kicks off a detached background refresh so
    the *next* invocation has fresh data. This keeps the hot path at zero
    added latency — the failure mode of the old install-age nag.
    """
    try:
        info = _read_installed_wheel_info()
    except (OSError, ValueError, KeyError):
        # Metadata parsing failed — fail open.
        return
    if info is None:
        return
    if info.is_editable:
        # Editable install with no .git/ reachable — most likely a
        # ``pip install -e .`` outside the source tree. The clone path
        # would have caught this if .git/ were reachable; there's no
        # PyPI release to compare an editable install against, so bail.
        return
    if info.vcs_url:
        # A git/VCS install tracks a moving ref, not a PyPI release. Its
        # version string (e.g. a frozen ``0.1.0`` on an unbumped ``main``)
        # is not comparable to the latest PyPI version: the nag would fire
        # forever even on a build that is *ahead* of the latest release,
        # and reinstalling the ref can never change that version. The
        # ``omni upgrade`` git path re-pulls the ref and reports commit
        # deltas instead; the passive PyPI nag is simply wrong here.
        return

    cache = _read_cache()
    if (
        cache is not None
        and cache.kind == "wheel"
        and cache.latest_version
        and _is_newer(cache.latest_version, info.package_version)
        and cache.latest_version != cache.last_notified_version
    ):
        _print_pypi_notice(info.package_version, cache.latest_version)
        # Stamp the version we just nagged about so the notice fires
        # exactly once per new release (until an even newer one ships).
        _write_cache(
            _CacheEntry(
                last_check_epoch=cache.last_check_epoch,
                commits_behind=0,
                kind="wheel",
                latest_version=cache.latest_version,
                last_notified_version=cache.latest_version,
            )
        )

    # Refresh the cached "latest version" out of band when it is missing,
    # stale, or was written by the (other-shape) clone path.
    if cache is None or cache.kind != "wheel" or _is_stale(cache):
        _spawn_background_refresh()


def _is_newer(latest: str, current: str) -> bool:
    """Return whether *latest* is a strictly newer release than *current*.

    Uses PEP 440 comparison (``packaging.version``) so pre-releases,
    post-releases, and dev builds order correctly. Falls back to a string
    inequality when either value is not a valid PEP 440 version, so a
    malformed PyPI response can never crash the check.

    :param latest: Candidate latest version, e.g. ``"0.2.0"``.
    :param current: The installed version, e.g. ``"0.1.0"``.
    :returns: ``True`` when ``latest`` is strictly greater than
        ``current``.
    """
    from packaging.version import InvalidVersion, parse

    try:
        return parse(latest) > parse(current)
    except InvalidVersion:
        return latest != current and bool(latest)


def _resolve_index_url() -> str:
    """Resolve the package index to query, honoring uv/pip config.

    Precedence, mirroring how ``uv`` / ``pip`` (and therefore
    ``omni upgrade``) actually pick an index:

    1. index env vars (:data:`_INDEX_ENV_VARS`) — explicit
       ``OMNIGENT_INDEX_URL`` first, then uv's and pip's;
    2. uv's configured default index (``uv.toml``);
    3. pip's configured ``index-url`` (``pip.conf``);
    4. :data:`_DEFAULT_INDEX_URL`.

    Reading the config files (not just env vars) is what makes the check
    work on the common corporate setup where the mirror lives in
    ``~/.config/uv/uv.toml`` or ``pip.conf`` rather than an env var — the
    same place ``uv tool install`` found it.

    :returns: The index base URL with any trailing slash stripped, e.g.
        ``"https://pypi.org/simple"``.
    """
    for var in _INDEX_ENV_VARS:
        value = os.environ.get(var)
        if value:
            url = _first_index_token(value)
            if url:
                return url
    for reader in (_index_from_uv_config, _index_from_pip_config):
        url = reader()
        if url:
            return url
    return _DEFAULT_INDEX_URL


def _first_index_token(value: str) -> str:
    """Return the first (primary) URL from a possibly multi-value index var.

    :param value: An index env var value, e.g.
        ``"https://a/simple https://b/simple"``.
    :returns: The first URL with any trailing slash stripped, or ``""``.
    """
    tokens = value.replace(",", " ").split()
    return tokens[0].rstrip("/") if tokens else ""


def _user_config_base() -> Path:
    """Return the XDG user-config dir (``$XDG_CONFIG_HOME`` or ``~/.config``)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def _index_from_uv_config() -> str:
    """Read uv's configured *default* index from ``uv.toml``, or ``""``.

    Checks the user config (``$XDG_CONFIG_HOME/uv/uv.toml`` or
    ``~/.config/uv/uv.toml``) then the system config (``/etc/uv/uv.toml``).
    Honors the legacy ``index-url`` key and a ``[[index]]`` entry marked
    ``default = true``. A non-default ``[[index]]`` is *supplementary*
    (PyPI stays the default) and is deliberately ignored, so we never
    mistake an extra index for the primary one. Best-effort: any read /
    parse error skips that file.

    :returns: The configured default index URL (trailing slash stripped),
        or ``""`` when none is set.
    """
    import tomllib

    for path in (_user_config_base() / "uv" / "uv.toml", Path("/etc/uv/uv.toml")):
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, ValueError):
            continue
        legacy = data.get("index-url")
        if isinstance(legacy, str) and legacy.strip():
            return legacy.strip().rstrip("/")
        indexes = data.get("index")
        if isinstance(indexes, list):
            for entry in indexes:
                url = entry.get("url") if isinstance(entry, dict) else None
                if (
                    isinstance(entry, dict)
                    and entry.get("default") is True
                    and isinstance(url, str)
                    and url.strip()
                ):
                    return url.strip().rstrip("/")
    return ""


def _index_from_pip_config() -> str:
    """Read pip's configured ``index-url`` from ``pip.conf``, or ``""``.

    Checks ``$PIP_CONFIG_FILE``, the user config
    (``$XDG_CONFIG_HOME/pip/pip.conf`` or ``~/.config/pip/pip.conf``, plus
    legacy ``~/.pip/pip.conf``) and common system locations — highest
    precedence first — returning the first ``[global]`` / ``[install]``
    ``index-url`` found. Interpolation is disabled so URLs containing
    ``%`` parse cleanly. Best-effort: any read / parse error skips.

    :returns: The configured ``index-url`` (trailing slash stripped), or
        ``""`` when none is set.
    """
    import configparser

    candidates: list[Path] = []
    env_file = os.environ.get("PIP_CONFIG_FILE")
    if env_file:
        candidates.append(Path(env_file))
    candidates += [
        _user_config_base() / "pip" / "pip.conf",
        Path.home() / ".pip" / "pip.conf",
        Path("/Library/Application Support/pip/pip.conf"),
        Path("/etc/pip.conf"),
    ]
    for path in candidates:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            if not parser.read(path):
                continue
        except (configparser.Error, OSError, UnicodeDecodeError):
            continue
        for section in ("global", "install"):
            if parser.has_option(section, "index-url"):
                url = parser.get(section, "index-url").strip()
                if url:
                    return url.rstrip("/")
    return ""


def fetch_latest_version(
    include_prereleases: bool = False,
    *,
    timeout: float = _INDEX_TIMEOUT_SECONDS,
    attempts: int = 1,
) -> str | None:
    """Fetch the latest ``omnigent`` release from the configured index.

    Queries the Simple Repository API of the resolved index
    (:func:`_resolve_index_url`) — PEP 691 JSON when the index serves it,
    PEP 503 HTML otherwise. Swallows every error so the update check can never
    break the CLI. The background refresh uses the defaults (one snappy try);
    the foreground ``omni upgrade`` passes a longer *timeout* and *attempts=2*
    so a momentarily slow mirror doesn't spuriously read as "unreachable".

    :param include_prereleases: When ``False`` (default), pre-releases and
        dev releases are excluded so we never nag about a non-final build.
        When ``True`` (``omni upgrade --pre`` / TestPyPI rc validation),
        they are considered too.
    :param timeout: Per-request timeout in seconds.
    :param attempts: Total number of tries; a transient connection/timeout
        error retries until they are exhausted. A definitive non-200 response
        is never retried. Values < 1 are treated as 1.
    :returns: The latest matching version string (e.g. ``"0.2.0"`` or, with
        pre-releases, ``"0.2.0rc1"``), or ``None`` on any network / parse
        error, a non-200 response, or when no matching release is found.
    """
    import httpx
    from packaging.utils import canonicalize_name

    url = f"{_resolve_index_url()}/{canonicalize_name(_DIST_NAME)}/"
    resp = None
    for _ in range(max(1, attempts)):
        try:
            resp = httpx.get(
                url,
                headers={"Accept": _SIMPLE_JSON_ACCEPT},
                timeout=timeout,
                follow_redirects=True,
            )
            break  # got a response (any status) — don't retry a definitive reply
        except httpx.HTTPError:
            resp = None  # transient (timeout / connection reset) — retry if any left
    if resp is None or resp.status_code != 200:
        return None

    versions = _parse_simple_versions(resp)
    if not include_prereleases:
        versions = [v for v in versions if not (v.is_prerelease or v.is_devrelease)]
    return str(max(versions)) if versions else None


def _parse_simple_versions(resp: httpx.Response) -> list[Version]:
    """Extract candidate versions from a Simple-API response.

    Prefers the PEP 691 JSON body (``versions`` per PEP 700, else the
    ``files`` list); falls back to scraping filenames from the PEP 503
    HTML index when the server ignored our JSON ``Accept`` header.

    :param resp: A 200 response from ``<index>/<name>/``.
    :returns: Parsed :class:`~packaging.version.Version` objects (possibly
        empty); callers filter pre-releases and pick the max.
    """
    content_type = resp.headers.get("content-type", "")
    if "json" in content_type:
        try:
            data = resp.json()
        except ValueError:
            return []
        if not isinstance(data, dict):
            return []
        listed = data.get("versions")
        if isinstance(listed, list):
            return [v for v in (_safe_version(str(x)) for x in listed) if v is not None]
        files = data.get("files")
        if isinstance(files, list):
            return _versions_from_filenames(
                f.get("filename", "") for f in files if isinstance(f, dict)
            )
        return []
    # PEP 503 HTML fallback: filenames are the <a> link texts / hrefs.
    import re

    names = re.findall(r">([^<>]+\.(?:whl|tar\.gz|zip))<", resp.text)
    return _versions_from_filenames(names)


def _versions_from_filenames(filenames: Iterable[str]) -> list[Version]:
    """Parse versions from wheel / sdist filenames, skipping unparseable ones.

    :param filenames: Distribution filenames, e.g.
        ``["omnigent-0.2.0-py3-none-any.whl", "omnigent-0.2.0.tar.gz"]``.
    :returns: The versions successfully parsed out of them.
    """
    from packaging.utils import (
        InvalidSdistFilename,
        InvalidWheelFilename,
        parse_sdist_filename,
        parse_wheel_filename,
    )

    out: list[Version] = []
    for filename in filenames:
        try:
            if filename.endswith(".whl"):
                out.append(parse_wheel_filename(filename)[1])
            elif filename.endswith((".tar.gz", ".zip")):
                out.append(parse_sdist_filename(filename)[1])
        except (InvalidWheelFilename, InvalidSdistFilename):
            continue
    return out


def _safe_version(value: str) -> Version | None:
    """Parse a version string, returning ``None`` instead of raising.

    :param value: A candidate version, e.g. ``"0.2.0"``.
    :returns: The :class:`~packaging.version.Version`, or ``None`` when
        *value* is not a valid PEP 440 version.
    """
    from packaging.version import InvalidVersion, Version

    try:
        return Version(value)
    except InvalidVersion:
        return None


def refresh_update_cache() -> None:
    """Refresh the cached "latest released version" for the wheel path.

    Runs in a detached background process spawned by
    :func:`_spawn_background_refresh` (via ``python -c``), so it must be
    completely silent and must never raise. Performs the network index
    lookup the foreground deliberately avoids and writes the result to
    the shared cache, preserving ``last_notified_version`` so a pending
    "fire once per release" notice is not re-armed.
    """
    # Background best-effort: a crash here must never surface, so swallow
    # everything (the early returns below still exit cleanly).
    with contextlib.suppress(Exception):
        if os.environ.get(_ENV_SKIP):
            return
        # Only the wheel shape consults the index; a clone refreshes via git.
        if _find_repo_root() is not None:
            return
        info = _read_installed_wheel_info()
        if info is None or info.is_editable:
            return
        latest = fetch_latest_version()
        if latest is None:
            return
        prev = _read_cache()
        last_notified = (
            prev.last_notified_version if prev is not None and prev.kind == "wheel" else ""
        )
        _write_cache(
            _CacheEntry(
                last_check_epoch=time.time(),
                commits_behind=0,
                kind="wheel",
                latest_version=latest,
                last_notified_version=last_notified,
            )
        )


def _spawn_background_refresh() -> None:
    """Spawn a detached process to refresh the PyPI cache, fire-and-forget.

    Uses ``python -c`` rather than re-invoking the ``omni`` CLI so the
    refresh cannot recurse back into :func:`maybe_show_update_notice`,
    and runs in its own session with all standard streams sent to
    ``/dev/null`` so it neither blocks nor pollutes the foreground.
    Any spawn failure is swallowed — the worst case is simply that the
    cache is refreshed on a later invocation instead.
    """
    from omnigent.inner import _proc

    with contextlib.suppress(OSError, ValueError):
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from omnigent.update_check import refresh_update_cache as r; r()",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **_proc.spawn_kwargs(),
        )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _find_repo_root() -> Path | None:
    """Locate the dev clone's repo root, or ``None`` for installed wheels.

    The check is deliberately narrow: the candidate must be the
    DIRECT parent of the ``omnigent/`` package, and must contain
    both ``.git/`` AND a ``pyproject.toml``. The layout we accept
    is exactly what a dev clone of this repo looks like::

        <repo>/.git/
        <repo>/pyproject.toml
        <repo>/omnigent/update_check.py    ← this file

    We do NOT walk further up the filesystem tree. The previous
    implementation did, which caused a real bug: when ``omnigent``
    is installed via ``uv tool install`` at
    ``~/.local/share/uv/tools/omnigent/…``, the unbounded walk-up
    matched ``~/.git/`` (a dotfiles repo, or any other unrelated
    git repo between $HOME and the install dir) and misclassified
    the install as a dev clone. The dispatcher then ran
    ``git fetch`` against the wrong repo and wrote
    ``kind: "clone"`` to the cache.

    :returns: The repo root ``Path`` for a dev clone, or ``None``
        for any installed-wheel scenario.
    """
    package_dir = Path(__file__).resolve().parent  # <candidate>/omnigent/
    candidate = package_dir.parent
    # ``.git`` may be a directory (a normal clone) or a file (a git
    # worktree), so check for existence rather than requiring a dir.
    if (candidate / ".git").exists() and (candidate / "pyproject.toml").is_file():
        return candidate
    return None


def _read_cache() -> _CacheEntry | None:
    """Read the cached check result from disk.

    :returns: A ``_CacheEntry`` if the cache file exists and is valid
        JSON, otherwise ``None``.
    """
    try:
        raw = _CACHE_FILE.read_text()
        data = json.loads(raw)
        return _CacheEntry(
            last_check_epoch=float(data["last_check_epoch"]),
            commits_behind=int(data["commits_behind"]),
            head_sha=str(data.get("head_sha", "")),
            # Legacy caches (written before this field existed) get
            # the default ``"clone"`` — they were all clone caches.
            kind=str(data.get("kind", "clone")),
            latest_version=str(data.get("latest_version", "")),
            last_notified_version=str(data.get("last_notified_version", "")),
        )
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _is_stale(entry: _CacheEntry) -> bool:
    """Return whether *entry* is older than the staleness threshold.

    :param entry: The cached check result.
    :returns: ``True`` if the cache should be refreshed.
    """
    return (time.time() - entry.last_check_epoch) >= _STALENESS_SECONDS


def _write_cache(entry: _CacheEntry) -> None:
    """Atomically write *entry* to the cache file.

    Uses write-to-tmpfile + ``Path.replace()`` so concurrent CLI
    invocations never see a half-written file.

    :param entry: The check result to persist.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "last_check_epoch": entry.last_check_epoch,
            "commits_behind": entry.commits_behind,
            "head_sha": entry.head_sha,
            "kind": entry.kind,
            "latest_version": entry.latest_version,
            "last_notified_version": entry.last_notified_version,
        }
    )
    # ``dir=`` on the same filesystem guarantees atomic replace.
    fd, tmp_path = tempfile.mkstemp(dir=_CACHE_DIR, suffix=".tmp")
    closed = False
    try:
        os.write(fd, payload.encode())
        os.close(fd)
        closed = True
        Path(tmp_path).replace(_CACHE_FILE)
    except OSError:
        if not closed:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _run_check(repo_root: Path) -> _CacheEntry | None:
    """Fetch upstream and count how many commits HEAD is behind.

    Tries ``origin/main`` first; falls back to ``origin/master``.

    :param repo_root: Absolute path to the Git repository root,
        e.g. ``Path("/home/user/omnigent-2")``.
    :returns: A ``_CacheEntry`` with the result, or ``None`` if
        ``git`` is not available or both branches fail.
    """
    for branch in ("main", "master"):
        behind = _fetch_and_count(repo_root, branch)
        if behind is not None:
            return _CacheEntry(
                last_check_epoch=time.time(),
                commits_behind=behind,
                head_sha=_get_head_sha(repo_root) or "",
            )
    return None


def _fetch_and_count(repo_root: Path, branch: str) -> int | None:
    """Fetch ``origin/<branch>`` and return commits-behind count.

    :param repo_root: Absolute path to the Git repository root.
    :param branch: Remote branch name, e.g. ``"main"``.
    :returns: Number of commits HEAD is behind, or ``None`` on
        any failure (timeout, missing remote, missing branch).
    """
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "origin", branch, "--quiet"],
            timeout=_GIT_TIMEOUT_SECONDS,
            capture_output=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-list",
                f"HEAD..origin/{branch}",
                "--count",
            ],
            timeout=_GIT_TIMEOUT_SECONDS,
            capture_output=True,
            check=True,
            text=True,
        )
        return int(result.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _get_head_sha(repo_root: Path) -> str | None:
    """Return the current HEAD commit hash, or ``None`` on failure.

    :param repo_root: Absolute path to the Git repository root.
    :returns: The full SHA-1 hex string, or ``None``.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            timeout=_GIT_TIMEOUT_SECONDS,
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _local_rev_list_count(repo_root: Path) -> int | None:
    """Count commits HEAD is behind origin/main (or master), locally only.

    No ``git fetch`` — uses whatever ``origin/main`` ref is already
    available.  This is cheap and handles the post-pull case.

    :param repo_root: Absolute path to the Git repository root.
    :returns: Number of commits behind, or ``None`` on failure.
    """
    for branch in ("main", "master"):
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "rev-list",
                    f"HEAD..origin/{branch}",
                    "--count",
                ],
                timeout=_GIT_TIMEOUT_SECONDS,
                capture_output=True,
                check=True,
                text=True,
            )
            return int(result.stdout.strip())
        except (subprocess.SubprocessError, OSError, ValueError):
            continue
    return None


def _print_notice(commits_behind: int) -> None:
    """Print the update notice to stderr.

    :param commits_behind: Number of commits the local clone is
        behind, e.g. ``3``.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    console = Console(stderr=True)
    body = Text.assemble(
        ("Update available", "bold yellow"),
        " — origin/main is ",
        (f"{commits_behind}", "bold"),
        " commit(s) ahead.\nRun ",
        ("git pull", "bold"),
        " to update.",
    )
    console.print(Panel(body, border_style="yellow", expand=False))


def _print_pypi_notice(current: str, latest: str) -> None:
    """Print the installed-wheel "update available" notice to stderr.

    Informational only — it names the new release and points the user at
    ``omni upgrade``; the actual upgrade (and the graceful server/daemon
    cycle) lives in that command, not here.

    :param current: The installed version, e.g. ``"0.1.0"``.
    :param latest: The newer release available on PyPI, e.g. ``"0.2.0"``.
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    console = Console(stderr=True)
    body = Text.assemble(
        ("Update available", "bold yellow"),
        " — omnigent ",
        (latest, "bold"),
        f" is out (you have {current}).\nRun ",
        ("omni upgrade", "bold"),
        " to update.",
    )
    console.print(Panel(body, border_style="yellow", expand=False))


# ------------------------------------------------------------------
# Installed-wheel path
# ------------------------------------------------------------------


def _read_build_info() -> tuple[float, str] | None:
    """Return ``(build_time_epoch, commit_sha)`` from ``_build_info``.

    The ``omnigent/_build_info.py`` module is generated by
    ``setup.py``'s ``build_py`` override at wheel build time. It is
    gitignored, so source checkouts that have never been built
    won't have it on disk — in which case the import fails and we
    return ``None`` so the caller can fall back to other signals.

    Wrapped as a module-level helper (rather than an inline ``try
    import``) so tests can monkeypatch it without going through
    ``sys.modules`` to fake the import outcome.

    :returns: A ``(BUILD_TIME_EPOCH, COMMIT_SHA)`` pair, or ``None``
        when ``_build_info`` is unavailable. ``COMMIT_SHA`` may be
        the empty string (when ``setup.py`` ran in an environment
        without ``git``); the caller treats that as "no commit info".
    """
    try:
        from omnigent import _build_info
    except ImportError:
        return None
    try:
        ts = float(_build_info.BUILD_TIME_EPOCH)
        sha = str(_build_info.COMMIT_SHA)
    except (AttributeError, TypeError, ValueError):
        # Malformed _build_info.py (manually edited or corrupted).
        # Don't trust it — fall back to other signals.
        return None
    return ts, sha


def _get_distribution() -> importlib.metadata.Distribution | None:
    """Resolve our installed distribution, or ``None`` if not installed.

    Wrapped as a module-level helper so tests can monkeypatch it
    without walking through ``importlib.metadata``'s module
    singleton (see ``CLAUDE.md`` rule 14 on global-module-clobbering).

    :returns: The ``Distribution`` for the ``omnigent`` package,
        or ``None`` when not installed (e.g. running directly from a
        source tarball without ``pip install``).
    """
    try:
        return importlib.metadata.distribution(_DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return None


def _unredact_ssh_userinfo(vcs_url: str) -> str:
    """Repair a redacted SSH user in a VCS URL read from ``direct_url.json``.

    PEP 610 tooling (pip, and newer uv) redacts the userinfo of a VCS
    URL before writing it to ``direct_url.json``: a bare username with
    no password — which for an SSH remote is just the login user — is
    rewritten to the marker ``****``. So an install from
    ``git+ssh://git@github.com/org/repo.git`` is recorded as
    ``git+ssh://****@github.com/org/repo.git``.

    The SSH user is not a secret (it is ``git`` for GitHub, GitLab,
    Bitbucket, and every other major host), but the redaction leaves the
    reinstall command unrunnable: SSH tries to authenticate as the
    literal user ``****`` and fails with ``Permission denied
    (publickey)``. The original user is irrecoverable from
    the metadata, so we restore the canonical ``git`` — the only SSH
    user those hosts accept — which makes both the command we display
    and the command we run match and actually work.

    Only SSH URLs whose userinfo is *exactly* the redaction marker (the
    no-password case) are repaired. A partially-redacted ``user:****@``
    form encodes a real password we cannot and must not reconstruct, and
    non-SSH URLs (HTTPS, ``file://``) pass through untouched.

    :param vcs_url: The normalized ``<vcs>+<scheme>://…`` reinstall URL,
        e.g. ``"git+ssh://****@github.com/omnigent-ai/omnigent.git"``.
    :returns: The same URL with a redacted SSH user restored to ``git``,
        e.g. ``"git+ssh://git@github.com/omnigent-ai/omnigent.git"``;
        the input unchanged when no redacted SSH user is present.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(vcs_url)
    # Scheme is e.g. ``ssh`` or ``git+ssh``. Only SSH carries a login
    # user we can safely default to ``git``; HTTPS userinfo is a real
    # credential.
    if not (parts.scheme == "ssh" or parts.scheme.endswith("+ssh")):
        return vcs_url
    # Repair only the whole-username redaction (no password). A
    # ``user:****`` form hides a real password — leave it alone.
    if parts.username != _REDACTED_USERINFO or parts.password is not None:
        return vcs_url

    new_netloc = parts.netloc.replace(f"{_REDACTED_USERINFO}@", "git@", 1)
    return urlunsplit(parts._replace(netloc=new_netloc))


def _read_installed_wheel_info() -> _InstalledWheelInfo | None:
    """Read the installed distribution's metadata for the wheel check.

    Parses three files in the ``.dist-info/`` directory:

    * ``INSTALLER`` (PEP 376) — the installer that wrote the package
      (``"uv"``, ``"pip"``, ``"poetry"``, ...).
    * ``direct_url.json`` (PEP 610) — present for direct-URL installs
      (``git+<url>``, wheel-by-URL, ``-e``); absent for plain registry
      installs. Carries ``vcs_info`` (commit SHA) for git installs and
      ``dir_info.editable`` for editable installs.
    * ``uv_cache.json`` (uv-specific) — carries the build/cache
      timestamp and pinned commit SHA. Present for any uv-built
      package; absent for pip/pipx/poetry installs.

    Install time falls back to the ``.dist-info/`` directory's mtime
    when ``uv_cache.json`` is absent — this works for any installer.

    :returns: An ``_InstalledWheelInfo`` populated from the
        available metadata, or ``None`` when the package isn't
        installed at all (e.g. running from a checkout without a
        registered distribution) or when no install-time signal
        can be recovered.
    """
    dist = _get_distribution()
    if dist is None:
        return None

    installer = _read_installer(dist)

    is_editable = False
    vcs_url: str | None = None
    commit_sha: str | None = None
    direct_url_raw = _safe_read_dist_file(dist, "direct_url.json")
    if direct_url_raw:
        try:
            data = json.loads(direct_url_raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            dir_info = data.get("dir_info")
            if isinstance(dir_info, dict) and dir_info.get("editable"):
                is_editable = True
            vcs_info = data.get("vcs_info")
            if isinstance(vcs_info, dict):
                url = data.get("url")
                cid = vcs_info.get("commit_id")
                if isinstance(url, str):
                    # Normalize to the ``git+<url>`` form pip/uv accept
                    # back as a reinstall target. ``direct_url.json``
                    # stores the bare ``url`` field; the ``vcs`` (e.g.
                    # ``"git"``) prefix lives in ``vcs_info``.
                    vcs = vcs_info.get("vcs", "git")
                    vcs_url = url if url.startswith(f"{vcs}+") else f"{vcs}+{url}"
                    # Repair an SSH user the installer redacted to
                    # ``****`` when it wrote direct_url.json — otherwise
                    # the reinstall command ssh's in as user ``****``
                    # and fails.
                    vcs_url = _unredact_ssh_userinfo(vcs_url)
                if isinstance(cid, str):
                    commit_sha = cid

    install_time_epoch: float | None = None
    # Tier 1 (preferred): build-baked _build_info.py. Written by
    # ``setup.py``'s build_py override at wheel build time; carries
    # the exact build moment and commit SHA. Works across every
    # installer (uv / pip / pipx / poetry / ...) because it travels
    # inside the wheel, not as installer-specific metadata.
    build_info = _read_build_info()
    if build_info is not None:
        ts, sha = build_info
        install_time_epoch = ts
        if commit_sha is None and sha:
            commit_sha = sha

    # Tier 2: uv_cache.json — uv-only, but more accurate than mtime
    # for uv installs. Only consulted when _build_info didn't set
    # install_time_epoch (i.e. source checkouts where the build
    # hook never ran, or wheels packaged without setup.py running).
    uv_cache_raw = _safe_read_dist_file(dist, "uv_cache.json")
    if uv_cache_raw:
        try:
            uv_data = json.loads(uv_cache_raw)
        except json.JSONDecodeError:
            uv_data = None
        if isinstance(uv_data, dict):
            if install_time_epoch is None:
                timestamp_data = uv_data.get("timestamp")
                if isinstance(timestamp_data, dict):
                    secs = timestamp_data.get("secs_since_epoch")
                    if isinstance(secs, (int, float)):
                        install_time_epoch = float(secs)
            if commit_sha is None:
                uv_commit = uv_data.get("commit")
                if isinstance(uv_commit, str):
                    commit_sha = uv_commit

    if install_time_epoch is None:
        # Fall back to the dist-info dir's mtime — set by the
        # installer when it wrote the files. Works for pip / pipx /
        # poetry, and for uv installs that for some reason lack
        # ``uv_cache.json``.
        dist_info_dir = _dist_info_dir(dist)
        if dist_info_dir is not None:
            try:
                install_time_epoch = dist_info_dir.stat().st_mtime
            except OSError:
                install_time_epoch = None

    if install_time_epoch is None:
        return None

    detected_installer = installer
    if installer == "pip" and _looks_like_pipx_install():
        detected_installer = "pipx"

    # Read requested extras from installer-specific receipts when available.
    extras: list[str] = []
    if detected_installer == "uv":
        uv_extras = _read_uv_tool_extras()
        if uv_extras is not None:
            extras = uv_extras
    elif detected_installer == "pipx":
        pipx_extras = _read_pipx_extras()
        if pipx_extras is not None:
            extras = pipx_extras

    return _InstalledWheelInfo(
        install_time_epoch=install_time_epoch,
        installer=installer,
        vcs_url=vcs_url,
        commit_sha=commit_sha,
        is_editable=is_editable,
        package_version=dist.version,
        detected_installer=detected_installer,
        extras=tuple(extras),
    )


def _read_installer(dist: importlib.metadata.Distribution) -> str | None:
    """Return the lowercase installer name from ``INSTALLER``.

    :param dist: The distribution to read from.
    :returns: The installer name (e.g. ``"uv"``, ``"pip"``), or
        ``None`` when ``INSTALLER`` is missing, empty, or unreadable.
    """
    raw = _safe_read_dist_file(dist, "INSTALLER")
    if not raw:
        return None
    name = raw.strip().lower()
    return name or None


def _safe_read_dist_file(dist: importlib.metadata.Distribution, name: str) -> str | None:
    """Read a dist-info file by name, returning ``None`` on any error.

    ``Distribution.read_text`` already swallows ``FileNotFoundError``
    and friends, but the wrapper guards against transient permission
    errors and surprise ``OSError`` flavors so the caller never has
    to catch.

    :param dist: The distribution to read from.
    :param name: File name within the ``.dist-info/`` directory,
        e.g. ``"INSTALLER"`` or ``"direct_url.json"``.
    :returns: File contents as text, or ``None`` when the file is
        missing / unreadable / empty.
    """
    try:
        text = dist.read_text(name)
    except (OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    return text


def _dist_info_dir(dist: importlib.metadata.Distribution) -> Path | None:
    """Return the ``.dist-info/`` directory for a path-based distribution.

    ``importlib.metadata.PathDistribution`` stores the directory path
    on its ``_path`` attribute. The underscore is unfortunate but
    that's the documented handle; ``Distribution`` itself defines no
    public accessor. Returns ``None`` for non-path distributions
    (e.g. zipped eggs), where ``stat()`` can't give us an mtime.

    :param dist: The distribution.
    :returns: Absolute path to the ``.dist-info/`` directory, or
        ``None`` when the distribution isn't a ``PathDistribution``.
    """
    raw = getattr(dist, "_path", None)
    if isinstance(raw, Path):
        return raw
    # Some Python builds store this as a ``zipp.Path``-like object;
    # ``str()`` of those is the on-disk path. Only return when the
    # resulting path actually exists on disk.
    if raw is not None:
        candidate = Path(str(raw))
        if candidate.is_dir():
            return candidate
    return None


def _looks_like_pipx_install() -> bool:
    """Detect whether the running interpreter lives inside a pipx venv.

    pipx delegates to pip, so ``INSTALLER`` reads ``"pip"`` — but pipx
    creates its venvs under ``~/.local/pipx/venvs/<pkg>/`` (or the
    platform equivalent). Checking ``sys.prefix`` for the ``pipx/venvs``
    segment is the standard heuristic and is what pipx itself uses for
    self-recognition.

    :returns: ``True`` when ``sys.prefix`` looks like a pipx venv.
    """
    return "pipx/venvs" in sys.prefix.replace(os.sep, "/")


def _uv_tool_receipt_path() -> Path | None:
    """Locate ``uv-receipt.toml`` for a uv tool install of ``omnigent``.

    uv tool installs create a per-tool venv. The receipt sits in the
    venv root, next to ``bin/`` and ``lib/``. The running interpreter
    is ``<tool-dir>/<pkg>/bin/python``, so its grandparent is the venv
    root.

    We deliberately do *not* resolve symlinks: uv's ``bin/python`` is a
    symlink to a shared interpreter, and resolving it would point at the
    uv Python install directory instead of the tool venv.

    :returns: Path to ``uv-receipt.toml`` if it exists, otherwise ``None``.
    """
    if not sys.executable:
        return None
    try:
        exe = Path(sys.executable)
    except OSError:
        return None
    receipt = exe.parents[1] / "uv-receipt.toml"
    if receipt.is_file():
        return receipt
    tool_dir = os.environ.get("UV_TOOL_DIR")
    if tool_dir:
        receipt = Path(tool_dir) / _DIST_NAME / "uv-receipt.toml"
        if receipt.is_file():
            return receipt
    return None


def _read_uv_tool_extras() -> list[str] | None:
    """Read requested extras from the uv tool receipt.

    :returns: A list of extras when a receipt for ``omnigent`` is found.
        An empty list means the receipt exists but no extras were
        requested. ``None`` means no receipt was found or it could not
        be parsed.
    """
    receipt_path = _uv_tool_receipt_path()
    if receipt_path is None:
        return None
    try:
        data = tomllib.loads(receipt_path.read_text())
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return None
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        return None
    for req in requirements:
        if not isinstance(req, dict) or req.get("name") != _DIST_NAME:
            continue
        raw_extras = req.get("extras")
        if not isinstance(raw_extras, list):
            return []
        return [
            str(extra).strip() for extra in raw_extras if isinstance(extra, str) and extra.strip()
        ]
    return []


def _pipx_metadata_path() -> Path | None:
    """Locate pipx's venv metadata file for a pipx install of ``omnigent``.

    :returns: Path to ``pipx_metadata.json`` if the running interpreter
        is inside a pipx venv, otherwise ``None``.
    """
    if not sys.prefix:
        return None
    prefix = Path(sys.prefix)
    if "pipx/venvs" not in str(prefix).replace(os.sep, "/"):
        return None
    candidate = prefix / "pipx_metadata.json"
    return candidate if candidate.is_file() else None


def _parse_extras_from_spec(spec: str) -> list[str]:
    """Parse extras out of a PEP 508 package spec string.

    Examples: ``"omnigent[all,server]"`` → ``["all", "server"]``.

    :param spec: A package spec, possibly containing extras.
    :returns: The list of extra names (preserving order, deduplicated).
    """
    import re

    match = re.search(r"\[([^\]]+)\]", spec)
    if not match:
        return []
    seen: set[str] = set()
    extras: list[str] = []
    for raw in match.group(1).split(","):
        extra = raw.strip()
        if extra and extra not in seen:
            seen.add(extra)
            extras.append(extra)
    return extras


def _read_pipx_extras() -> list[str] | None:
    """Read requested extras from pipx's venv metadata.

    :returns: A list of extras parsed from ``main_package.package_or_url``.
        An empty list means metadata was found but the spec had no
        extras. ``None`` means the metadata could not be read.
    """
    metadata_path = _pipx_metadata_path()
    if metadata_path is None:
        return None
    try:
        data = json.loads(metadata_path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    main = data.get("main_package")
    if not isinstance(main, dict):
        return None
    spec = main.get("package_or_url")
    if not isinstance(spec, str):
        return []
    return _parse_extras_from_spec(spec)


def _extras_str(extras: Collection[str]) -> str:
    """Format extras as a PEP 508 extra suffix.

    :param extras: Iterable of extra names.
    :returns: ``"[a,b]"`` if extras is non-empty, otherwise ``""``.
    """
    if not extras:
        return ""
    return f"[{','.join(sorted(extras))}]"


@dataclass
class _UpgradeSuggestion:
    """A suggested upgrade command for the user's install shape.

    :param command: The shell command (or prose) we display in the
        nag panel, e.g. ``"uv tool upgrade omnigent"`` or
        ``"reinstall omnigent from your original source"``.
        Always populated.
    :param runnable: ``True`` when ``command`` is a real shell
        invocation we can execute via ``subprocess.run`` —
        i.e. the installer is one of uv / pip / pipx / poetry
        (or pip-as-fallback for an unknown installer with a known
        VCS URL). ``False`` for the unknown-installer prose
        fallbacks (``"reinstall X from ..."``) which exist to be
        read, not run; the interactive "run this now?" prompt is
        suppressed in that case.
    """

    command: str
    runnable: bool


# Per-installer flag that allows pre-releases (``omni upgrade --pre``).
# Only installers with a clean, well-defined flag are listed; others get
# no suffix (the base command still runs, just stable-only).
_PRERELEASE_FLAG = {
    "uv": " --prerelease allow",
    "pip": " --pre",
    "pipx": " --pip-args=--pre",
}


def _pip_invocation() -> str:
    """Return the pip command prefix bound to the running interpreter.

    ``omni upgrade`` shells out to install the new wheel, and a bare
    ``pip`` is resolved against ``PATH`` — which, in a shell where some
    *other* environment's ``pip`` shadows the one running ``omni`` (a
    conda env layered over the venv that actually holds the install,
    say), targets the wrong environment: it silently upgrades a different
    copy of omnigent while the running one stays put. ``<sys.executable>
    -m pip`` pins the upgrade to the interpreter actually running
    ``omni`` so the new wheel lands where the running CLI lives. Falls
    back to a bare ``pip`` only when ``sys.executable`` is unknown
    (frozen / embedded interpreters).

    uv-tool and pipx installs don't need this: they manage per-user tool
    environments through a global registry, so any ``uv`` / ``pipx`` on
    ``PATH`` upgrades the same install regardless of the active venv.

    :returns: The pip prefix, e.g. ``"/path/to/.venv/bin/python -m pip"``
        (the interpreter path shell-quoted so it survives
        ``shlex.split`` in :func:`_run_upgrade_command`), or ``"pip"``
        when no interpreter path is available.
    """
    import shlex

    if not sys.executable:
        return "pip"
    return f"{shlex.quote(sys.executable)} -m pip"


def _package_spec(*, version: str | None = None, extras: Collection[str] = ()) -> str:
    """Build a PEP 508 package spec for ``omnigent``.

    :param version: Optional pinned version, e.g. ``"0.2.0"``.
    :param extras: Optional extras to append.
    :returns: ``"omnigent"``, ``"omnigent[all]"``, ``"omnigent==0.2.0[all]"``,
        etc., depending on which arguments were provided.
    """
    spec = _DIST_NAME
    if version:
        spec += f"=={version}"
    spec += _extras_str(extras)
    return spec


def _build_upgrade_suggestion(
    info: _InstalledWheelInfo,
    *,
    allow_prerelease: bool = False,
    extra_overrides: tuple[str, ...] = (),
    target_version: str | None = None,
) -> _UpgradeSuggestion:
    """Build the right upgrade command for the user's install shape.

    Picks based on ``detected_installer`` (uv / pip / pipx / poetry /
    unknown) and whether ``direct_url.json`` recorded a VCS URL.

    :param info: Metadata from ``_read_installed_wheel_info``.
    :param allow_prerelease: When ``True`` (``omni upgrade --pre``), append
        the installer's allow-pre-releases flag (uv ``--prerelease allow``,
        pip ``--pre``, pipx ``--pip-args=--pre``) so the upgrade can land on
        a release candidate. A no-op for installers without a known flag.
    :param extra_overrides: Extras supplied by the user with
        ``omni upgrade --extra``. These take precedence over installer
        metadata.
    :param target_version: Pin the upgrade to a specific version instead
        of resolving the latest release.
    :returns: A :class:`_UpgradeSuggestion` whose ``command`` is the
        line printed in the nag panel and whose ``runnable`` flag
        tells the caller whether the line is an actual invocation
        (so the interactive prompt is offered) or a prose fallback
        (so the prompt is suppressed).
    """
    installer = info.detected_installer or info.installer
    pre = _PRERELEASE_FLAG.get(installer or "", "") if allow_prerelease else ""
    extras = sorted(set(info.extras) | set(extra_overrides))

    if info.vcs_url:
        # VCS install — we know the exact source URL.
        vcs_url_with_extras = info.vcs_url
        if extras:
            # Strip any existing fragment and append an egg spec with extras.
            base = vcs_url_with_extras.split("#", 1)[0]
            vcs_url_with_extras = f"{base}#egg={_package_spec(extras=extras)}"
        if installer == "uv":
            return _UpgradeSuggestion(
                command=f"uv tool install --reinstall {vcs_url_with_extras}{pre}",
                runnable=True,
            )
        if installer == "pipx":
            if extras:
                return _UpgradeSuggestion(
                    command=f"pipx install --force {vcs_url_with_extras}",
                    runnable=True,
                )
            # pipx tracks the original spec; ``reinstall`` re-pulls it.
            return _UpgradeSuggestion(command=f"pipx reinstall {_DIST_NAME}{pre}", runnable=True)
        if installer in ("pip", None):
            return _UpgradeSuggestion(
                command=(
                    f"{_pip_invocation()} install --force-reinstall {vcs_url_with_extras}{pre}"
                ),
                runnable=True,
            )
        if installer == "poetry":
            return _UpgradeSuggestion(
                command=f"poetry add --force {vcs_url_with_extras}", runnable=True
            )
        # Unknown installer with a known URL — fall through to a
        # generic suggestion that names the URL so the user can wire
        # it into their own tool. Not runnable.
        return _UpgradeSuggestion(
            command=f"reinstall {_DIST_NAME} from {vcs_url_with_extras}", runnable=False
        )

    # Registry install — no VCS URL recorded.
    registry_spec = _package_spec(version=target_version, extras=extras)
    if installer == "uv":
        if target_version or extras:
            # ``uv tool upgrade`` accepts only a tool name (and optional
            # version), not extras. Reinstall with the full PEP 508 spec
            # to preserve the extras the user originally requested.
            return _UpgradeSuggestion(
                command=f"uv tool install --reinstall {registry_spec}{pre}",
                runnable=True,
            )
        return _UpgradeSuggestion(command=f"uv tool upgrade {_DIST_NAME}{pre}", runnable=True)
    if installer == "pipx":
        if target_version or extras:
            # ``pipx upgrade`` does not accept extras / version specs.
            # ``pipx install --force`` does.
            return _UpgradeSuggestion(
                command=f"pipx install --force {registry_spec}",
                runnable=True,
            )
        return _UpgradeSuggestion(command=f"pipx upgrade {_DIST_NAME}{pre}", runnable=True)
    if installer == "pip":
        return _UpgradeSuggestion(
            command=f"{_pip_invocation()} install -U {registry_spec}{pre}",
            runnable=True,
        )
    if installer == "poetry":
        # Poetry update does not accept extras on the command line.
        return _UpgradeSuggestion(command=f"poetry update {_DIST_NAME}", runnable=True)
    return _UpgradeSuggestion(
        command=f"reinstall {_DIST_NAME} from your original source",
        runnable=False,
    )


# Canonical public repo for nightly builds. Nightlies are git tags consumed
# straight from GitHub (they never reach an index), so the channel lives
# upstream by definition: fork installs also upgrade onto upstream nightlies.
_NIGHTLY_REPO_URL = "https://github.com/omnigent-ai/omnigent"


def _newest_nightly_version(ls_remote_output: str) -> str | None:
    """Pick the newest nightly version from ``git ls-remote --tags`` output.

    Nightly tags are strictly ``vX.Y.Z.devYYYYMMDD``; the 8-digit datestamp
    requirement screens out legacy ``.dev0``-style tags, and rc/final tags
    and annotated-tag peel lines (``^{}``) never match. Ordering is PEP 440,
    so the first nightly after a main version bump outranks all older ones.

    :param ls_remote_output: Raw ``git ls-remote`` stdout (tab-separated
        ``<sha> refs/tags/<name>`` lines).
    :returns: The newest nightly version without the leading ``v`` (e.g.
        ``"0.8.0.dev20260804"``), or ``None`` when no nightly tag exists.
    """
    import re

    from packaging.version import InvalidVersion, Version

    pattern = re.compile(r"^v(\d+\.\d+\.\d+\.dev\d{8})$")
    versions: list[Version] = []
    for line in ls_remote_output.splitlines():
        match = pattern.match(line.rpartition("refs/tags/")[2].strip())
        if match is None:
            continue
        with contextlib.suppress(InvalidVersion):
            versions.append(Version(match.group(1)))
    return str(max(versions)) if versions else None


def _latest_nightly_version(repo_url: str = _NIGHTLY_REPO_URL) -> str | None:
    """Resolve the newest nightly version from the repo's tags, or ``None``.

    Best-effort ``git ls-remote`` with a tight timeout, like
    ``_remote_git_head``: any failure (offline, missing ``git``, no nightly
    tags yet) yields ``None`` so the caller prints an actionable message
    instead of crashing.

    :param repo_url: Repo to list tags from; defaults to the canonical repo.
    :returns: e.g. ``"0.8.0.dev20260804"``, or ``None``.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", repo_url, "refs/tags/v*.dev*"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return _newest_nightly_version(result.stdout)


def _build_nightly_upgrade_suggestion(
    info: _InstalledWheelInfo,
    nightly_version: str,
    *,
    extra_overrides: tuple[str, ...] = (),
) -> _UpgradeSuggestion:
    """Build the command that moves this install onto a nightly tag.

    Nightlies are git tags, not index releases, so every installer gets a
    git-pinned spec for the canonical repo. That includes registry installs
    (this is how an index install hops onto the nightly channel) and VCS
    installs of a fork (nightly tags only exist upstream). Extras follow
    the same union rule as :func:`_build_upgrade_suggestion`; the CLI
    refuses the registry install shapes that cannot record extras (pip /
    ``uv pip``) before building.

    :param info: Metadata from ``_read_installed_wheel_info``.
    :param nightly_version: Target version without the leading ``v``.
    :param extra_overrides: Extras supplied with ``omni upgrade --extra``.
    :returns: See :func:`_build_upgrade_suggestion`.
    """
    extras = sorted(set(info.extras) | set(extra_overrides))
    spec = f"git+{_NIGHTLY_REPO_URL}@v{nightly_version}"
    if extras:
        # Same egg-fragment shape the VCS upgrade path uses for extras.
        spec = f"{spec}#egg={_package_spec(extras=extras)}"
    installer = info.detected_installer or info.installer
    if installer == "uv":
        # --force: replacing a registry install with a git-sourced one (or
        # hopping between tags) is an overwrite, not an upgrade, in uv's model.
        return _UpgradeSuggestion(command=f"uv tool install --force {spec}", runnable=True)
    if installer == "pipx":
        return _UpgradeSuggestion(command=f"pipx install --force {spec}", runnable=True)
    if installer == "pip" or (installer is None and info.vcs_url is not None):
        return _UpgradeSuggestion(
            command=f"{_pip_invocation()} install --force-reinstall {spec}", runnable=True
        )
    if installer == "poetry":
        return _UpgradeSuggestion(command=f"poetry add --force {spec}", runnable=True)
    # Unknown installer on a registry install: don't guess the tool.
    return _UpgradeSuggestion(command=f"install {_DIST_NAME} from {spec}", runnable=False)


def _run_upgrade_command(command: str, console: Console) -> int:
    """Run the upgrade command in a foreground subprocess.

    The subprocess inherits stdin/stdout/stderr so the user sees
    installer progress (uv/pip output) live. We never pass
    ``shell=True``; the command is tokenized with ``shlex.split``.

    :param command: Shell-style command line, e.g.
        ``"uv tool upgrade omnigent"``.
    :param console: Rich console (stderr) used for the surrounding
        "Running:" / failure status lines, kept consistent with the
        panel above.
    :returns: The subprocess's exit code, or ``-1`` when the binary
        couldn't be started (binary missing from PATH, invalid
        command string). The caller treats any non-zero return as
        "upgrade failed" and falls back to the existing install.
    """
    import shlex

    console.print(f"[yellow]Running:[/yellow] {command}")
    try:
        result = subprocess.run(shlex.split(command), check=False)
    except (OSError, ValueError) as exc:
        # OSError: binary not on PATH. ValueError: shlex.split
        # rejected the command (extremely unlikely for our
        # generated commands, but the upgrade is best-effort).
        console.print(f"[red]Upgrade failed to start:[/red] {exc}")
        return -1
    return result.returncode


def upgrade_command_for_installed() -> _UpgradeSuggestion | None:
    """Return the upgrade command for the current install, or ``None``.

    Convenience wrapper used by ``omni upgrade``: reads the installed
    distribution's metadata and maps it to the installer-appropriate
    upgrade command (``uv tool upgrade omnigent``, ``pip install -U
    omnigent``, etc.).

    :returns: A :class:`_UpgradeSuggestion`, or ``None`` when the
        package is not installed as a wheel (e.g. running from a source
        checkout with no registered distribution).
    """
    info = _read_installed_wheel_info()
    if info is None:
        return None
    return _build_upgrade_suggestion(info)


def _probe_installed_distribution() -> tuple[str | None, str | None]:
    """Read the freshly-installed version and VCS commit in a subprocess.

    ``omni upgrade`` swaps the on-disk install while *this* process is
    running, but the running interpreter already imported the old
    ``omnigent`` and cached its metadata — re-reading in-process returns the
    *pre-upgrade* version. A fresh ``sys.executable`` subprocess loads the
    new metadata from disk, so it reports what the upgrade actually
    produced. This is what lets ``omni upgrade`` verify the install really
    advanced instead of trusting the installer's exit code (a no-op upgrade
    — pinned spec, cooldown, stale index cache, or a git ref that can't move
    the version — exits 0 without changing anything).

    The version is read via ``importlib.metadata`` (no ``omnigent`` import,
    so it survives even a broken upgrade); the commit is read from the
    install's PEP 610 ``direct_url.json`` by re-running this module's own
    detector, and is empty for registry installs.

    :returns: ``(version, commit_sha)`` of the now-installed distribution.
        Either element is ``None`` when it can't be determined (subprocess
        failure, frozen interpreter with no ``sys.executable``, or — for the
        commit — a non-VCS install).
    """
    if not sys.executable:
        return None, None
    probe = (
        "import importlib.metadata as m\n"
        "try:\n"
        "    print(m.version('omnigent'))\n"
        "except Exception:\n"
        "    print('')\n"
        "try:\n"
        "    from omnigent.update_check import _read_installed_wheel_info as r\n"
        "    i = r()\n"
        "    print((i.commit_sha or '') if i is not None else '')\n"
        "except Exception:\n"
        "    print('')\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            # A metadata lookup is sub-second; the git timeout is reused only as
            # a generous upper bound so a wedged interpreter can't hang the CLI.
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode != 0:
        return None, None
    lines = result.stdout.splitlines()
    version = lines[0].strip() if lines and lines[0].strip() else None
    commit = lines[1].strip() if len(lines) >= 2 and lines[1].strip() else None
    return version, commit


def _split_vcs_url(vcs_url: str) -> tuple[str, str | None]:
    """Split a ``git+<url>[@<rev>]`` into ``(repo_url, revision)``.

    Drops the ``git+`` prefix uv/pip prepend and separates a trailing
    ``@<rev>`` (branch / tag / sha) when present. An ``@`` that is *userinfo*
    (``git@host`` in an SSH URL) is NOT a revision: only an ``@`` after the
    final ``/`` — i.e. past the repo path — is treated as one.

    :param vcs_url: e.g. ``"git+https://host/org/repo.git@main"``.
    :returns: ``(repo_url, revision_or_None)``, e.g.
        ``("https://host/org/repo.git", "main")``.
    """
    url = vcs_url[4:] if vcs_url.startswith("git+") else vcs_url
    # Drop a PEP 508 / pip URL fragment (``#egg=...`` / ``#subdirectory=...``).
    # It is never part of the remote URL or the ref, and leaving it on would
    # make ``git ls-remote`` query a nonexistent ref and silently return None.
    url = url.split("#", 1)[0]
    at = url.rfind("@")
    if at > url.rfind("/"):  # '@' past the final path segment → revision suffix
        return url[:at], (url[at + 1 :] or None)
    return url, None


def _remote_git_head(vcs_url: str) -> str | None:
    """Return the remote commit a ``git+`` install's ref points at, or ``None``.

    Runs ``git ls-remote`` against the bare repo URL for the install's
    revision (or ``HEAD`` when none was pinned), so ``omni upgrade`` can tell
    whether a git install is behind its tracked ref *before* re-pulling.
    Best-effort with a tight timeout: any failure (offline, auth, bad URL, no
    ``git`` binary) yields ``None`` so ``--check`` degrades to "can't
    determine" rather than crashing.

    :param vcs_url: A normalized VCS URL, e.g.
        ``"git+https://github.com/omnigent-ai/omnigent.git"`` or
        ``"git+https://…/omnigent.git@main"``.
    :returns: The 40-char commit SHA the ref resolves to, or ``None``.
    """
    url, ref = _split_vcs_url(vcs_url)
    if not url:
        return None
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref or "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    first = result.stdout.split("\n", 1)[0].strip()
    sha = first.split("\t", 1)[0].strip() if first else ""
    return sha or None
