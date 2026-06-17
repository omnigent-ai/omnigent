"""Warn-mode test-environment guardrails.

A small, additive safety net that checks a test run is pointed at
throwaway resources — running under pytest, against a tmp/in-memory
SQLite DB, and not aimed at a known dev/prod host or port — *before*
the suite starts mutating state.

Today every check is **warn-only**: a violation logs a clear
``TEST GUARDRAIL:`` ``WARNING`` and the run continues. The design keeps
a single ``warn_only`` switch so a future PR can flip the default to
``False`` and have the same checks hard-fail (raise
:class:`TestGuardrailError`) with no other code change.

Entry point: :func:`check_test_environment`.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

_logger = logging.getLogger(__name__)

# Prefix on every guardrail log line so violations are greppable and so a
# future hard-fail mode can reuse the identical message.
_WARN_PREFIX = "TEST GUARDRAIL:"

# Ports we never want a test to drive: the local server default (6767),
# the Docker server (8000), and the Vite dev server (5173). A test base
# URL hitting any of these is almost certainly pointed at a real running
# instance rather than an ephemeral fixture server (which binds a random
# free port). Module-level so it's trivial to extend.
DEV_PORTS: frozenset[int] = frozenset({6767, 8000, 5173})

# Hostnames that denote a developer's own machine. A bare loopback host
# is only flagged when paired with a dev port (handled separately), but
# these named dev/prod hosts are always suspect for a test base URL.
DEV_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
        "host.docker.internal",
    }
)

# Env vars that, when set, explicitly assert "this process is a test run"
# even if pytest's own PYTEST_CURRENT_TEST isn't visible yet (it's only
# set per-test, not at session configure time). OMNIGENT_TEST_MODE is
# introduced by this module as the canonical, settable flag.
_TEST_MODE_ENV_VARS = ("OMNIGENT_TEST_MODE", "OMNIGENT_ENV")
_TEST_MODE_ENV_VALUES = frozenset({"1", "true", "test", "testing", "yes", "on"})


class TestGuardrailError(AssertionError):
    """Raised by :func:`check_test_environment` when ``warn_only=False``.

    Subclasses :class:`AssertionError` so a future hard-fail mode reads
    naturally as a failed test precondition. Unused while the default is
    warn-only, but defined now so the hard-fail flip is a one-line change.
    """

    # Tell pytest this is not a test class despite the ``Test`` prefix.
    __test__ = False


def looks_like_pytest(env: Mapping[str, str]) -> bool:
    """Return whether *env* indicates the process is a pytest run.

    True when pytest's per-test ``PYTEST_CURRENT_TEST`` is set, when the
    ``pytest`` module is already imported, or when an explicit test-mode
    flag (:data:`_TEST_MODE_ENV_VARS`) is truthy.

    :param env: Environment mapping to inspect (usually ``os.environ``).
    :returns: ``True`` if this looks like a test process.
    """
    if env.get("PYTEST_CURRENT_TEST"):
        return True
    if "pytest" in _imported_modules():
        return True
    for name in _TEST_MODE_ENV_VARS:
        if env.get(name, "").strip().lower() in _TEST_MODE_ENV_VALUES:
            return True
    return False


def _imported_modules() -> frozenset[str]:
    """Return the set of top-level imported module names.

    Wrapped in a helper so tests can reason about it; pytest is in
    ``sys.modules`` for the whole session once collection starts.
    """
    import sys

    return frozenset(sys.modules)


def looks_like_test_db(db_uri: str) -> bool:
    """Return whether *db_uri* looks like a throwaway test database.

    Accepts in-memory SQLite, file SQLite under a system temp dir, or any
    URI whose path/name contains ``test``. Everything else (a real
    ``~/.omnigent/chat.db``, a Postgres ``DATABASE_URL``) is treated as a
    non-test DB.

    :param db_uri: A SQLAlchemy-style URI, e.g. ``sqlite:///…`` .
    :returns: ``True`` if the URI looks like a test DB.
    """
    if not db_uri:
        return False
    lowered = db_uri.lower()

    # In-memory SQLite: `sqlite://`, `sqlite:///:memory:`, or `mode=memory`.
    if ":memory:" in lowered or "mode=memory" in lowered:
        return True
    if lowered in ("sqlite://", "sqlite:///"):
        return True

    if "test" in lowered:
        return True

    path = _sqlite_path(db_uri)
    if path is not None and _under_temp_dir(path):
        return True

    return False


def _sqlite_path(db_uri: str) -> Path | None:
    """Extract the filesystem path from a file-backed SQLite URI.

    :param db_uri: A candidate database URI.
    :returns: The DB file path, or ``None`` for non-file-SQLite URIs.
    """
    prefix = "sqlite:///"
    if not db_uri.lower().startswith(prefix):
        return None
    raw = db_uri[len(prefix) :]
    # Strip any query string (e.g. `?mode=memory&cache=shared`).
    raw = raw.split("?", 1)[0]
    if not raw or raw == ":memory:":
        return None
    return Path(raw)


def _under_temp_dir(path: Path) -> bool:
    """Return whether *path* lives under a system temp directory.

    Compares against :func:`tempfile.gettempdir` plus the common
    ``/tmp`` and ``/private/var`` (macOS) roots, all resolved so symlinked
    temp dirs (macOS ``/tmp`` → ``/private/tmp``) still match.

    :param path: A filesystem path (need not exist).
    :returns: ``True`` if *path* is under a temp root.
    """
    candidate = _resolve(path)
    temp_roots = {
        _resolve(Path(tempfile.gettempdir())),
        _resolve(Path("/tmp")),
        _resolve(Path("/private/var/folders")),
    }
    return any(candidate == root or root in candidate.parents for root in temp_roots)


def _resolve(path: Path) -> Path:
    """Best-effort absolute resolution that tolerates missing paths."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def base_url_violation(base_url: str) -> str | None:
    """Return a human-readable reason if *base_url* targets a dev host/port.

    :param base_url: The test base URL, e.g. ``http://localhost:6767``.
    :returns: A reason string when the URL is suspect, else ``None``.
    """
    if not base_url:
        return None

    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    port = parsed.port

    if port in DEV_PORTS:
        return f"base_url {base_url!r} targets dev/prod port {port}"
    if host in DEV_HOSTS and port is None:
        # A named dev host with no explicit port (default 80/443) still
        # smells like a real instance rather than an ephemeral fixture.
        return f"base_url {base_url!r} targets dev host {host!r}"
    return None


def check_test_environment(
    *,
    env: Mapping[str, str] | None = None,
    db_uri: str,
    base_url: str | None = None,
    warn_only: bool = True,
) -> list[str]:
    """Check the test environment is pointed at throwaway resources.

    Runs three checks and collects a reason string for each violation:

    a. the process looks like a pytest run (:func:`looks_like_pytest`);
    b. *db_uri* looks like a test DB (:func:`looks_like_test_db`);
    c. *base_url*, when given, does not target a dev/prod host or port
       (:func:`base_url_violation`).

    When ``warn_only`` is ``True`` (the default today) each violation is
    logged as a ``WARNING`` prefixed with ``TEST GUARDRAIL:`` and the
    function returns the list of reasons. When ``warn_only`` is ``False``
    the same set of violations raises :class:`TestGuardrailError` — this
    is the future hard-fail mode and is not used by the suite yet.

    :param env: Environment mapping; defaults to ``os.environ``.
    :param db_uri: The resolved store DB URI for this run.
    :param base_url: Optional base URL the test will drive.
    :param warn_only: Log instead of raise on violation (default ``True``).
    :returns: The list of violation reason strings (empty when clean).
    :raises TestGuardrailError: when ``warn_only=False`` and any check fails.
    """
    if env is None:
        env = os.environ

    violations: list[str] = []

    if not looks_like_pytest(env):
        violations.append(
            "process does not look like a pytest run "
            "(no PYTEST_CURRENT_TEST / OMNIGENT_TEST_MODE and pytest not imported)"
        )

    if not looks_like_test_db(db_uri):
        violations.append(
            f"db_uri {db_uri!r} does not look like a test DB "
            "(expected an in-memory/tmp SQLite or a URI containing 'test')"
        )

    if base_url is not None:
        reason = base_url_violation(base_url)
        if reason is not None:
            violations.append(reason)

    if not violations:
        return violations

    if warn_only:
        for reason in violations:
            _logger.warning("%s %s", _WARN_PREFIX, reason)
        return violations

    raise TestGuardrailError(f"{_WARN_PREFIX} " + "; ".join(violations))
