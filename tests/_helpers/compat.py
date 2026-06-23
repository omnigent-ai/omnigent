"""
Helpers for the server-version backwards-compatibility harness.

See ``docs/SERVER_VERSION_COMPAT_CI.md``. Two concerns:

1. **Server redirect** — in compat mode the ``omnigent.cli server``
   subprocess is launched from a *different* venv (one holding a pinned,
   older ``omnigent`` build) than the test process. Driven by
   ``OMNIGENT_COMPAT_SERVER_PYTHON``.
2. **Version skip** — resolve the running server's version and enforce
   ``@pytest.mark.min_server_version(...)`` so tests for features newer
   than the server-under-test are skipped rather than failing.

Outside compat mode (neither env var set) every function here is inert and
the test harness behaves exactly as before.
"""

from __future__ import annotations

import os
import sys
import tempfile

import httpx
from packaging.version import Version

# Stable empty directory used as the server subprocess CWD in compat mode
# (created lazily; see :func:`compat_server_cwd`).
_compat_cwd: str | None = None

# Interpreter for the SERVER subprocess. Set to a venv python holding the
# pinned older build; unset in normal runs (use the test process's python).
COMPAT_SERVER_PYTHON_ENV = "OMNIGENT_COMPAT_SERVER_PYTHON"
# Version string the workflow pinned (e.g. "0.1.1"). Backstop / cross-check
# for the skip logic — never used to launch anything.
COMPAT_SERVER_VERSION_ENV = "OMNIGENT_COMPAT_SERVER_VERSION"


# ── Server redirect ────────────────────────────────────────────────────


def compat_server_python() -> str | None:
    """
    Interpreter the server subprocess should run under, or ``None``.

    :returns: The value of ``OMNIGENT_COMPAT_SERVER_PYTHON`` (a venv python
        path, e.g. ``"/tmp/server-env/bin/python"``) when compat mode is
        active, else ``None``.
    """
    return os.environ.get(COMPAT_SERVER_PYTHON_ENV) or None


def server_executable() -> str:
    """
    Interpreter to launch ``omnigent.cli server`` with.

    :returns: The compat interpreter in compat mode, else ``sys.executable``
        (the test process's own python).
    """
    return compat_server_python() or sys.executable


def server_pythonpath(repo_root: str | os.PathLike[str]) -> str | None:
    """
    ``PYTHONPATH`` value for the server subprocess, or ``None`` to drop it.

    Normally the worktree (*repo_root*) is prepended so the server imports
    the branch's source rather than a stale installed copy. In compat mode
    that prepend is **dropped** — otherwise the worktree would shadow the
    pinned older ``omnigent`` in the compat venv, silently testing main
    against main.

    :param repo_root: Worktree root to prepend in normal mode, e.g.
        ``Path("/Users/me/omnigent")``.
    :returns: ``"<repo_root>:<existing PYTHONPATH>"`` in normal mode;
        ``None`` in compat mode (caller should omit ``PYTHONPATH`` so the
        compat venv's site-packages resolves ``omnigent``).
    """
    if compat_server_python() is not None:
        return None
    existing = os.environ.get("PYTHONPATH", "")
    return f"{repo_root}{os.pathsep}{existing}"


def compat_server_cwd() -> str | None:
    """
    Working directory for the server subprocess, or ``None`` to inherit.

    In compat mode the subprocess must **not** run with the worktree as its
    CWD: ``python -m omnigent.cli`` puts the CWD on ``sys.path[0]``, so the
    worktree's ``omnigent/`` package would shadow the pinned older install
    exactly like the ``PYTHONPATH`` prepend would — and CI runs from the repo
    checkout root, which contains ``omnigent/``. Returning a stable empty
    directory forces the compat venv's installed ``omnigent`` to resolve.

    :returns: A stable empty directory path in compat mode; ``None`` outside
        compat mode (inherit the parent's CWD — today's behavior).
    """
    global _compat_cwd
    if compat_server_python() is None:
        return None
    if _compat_cwd is None:
        _compat_cwd = tempfile.mkdtemp(prefix="omnigent-compat-cwd-")
    return _compat_cwd


def apply_server_env(env: dict[str, str], repo_root: str | os.PathLike[str]) -> dict[str, str]:
    """
    Set/drop ``PYTHONPATH`` on a server-subprocess env dict for the mode.

    Mutates *env* in place (and returns it) so call sites can pass their
    fully-built env straight to ``subprocess.Popen``.

    :param env: The server subprocess environment being assembled.
    :param repo_root: Worktree root (see :func:`server_pythonpath`).
    :returns: The same dict, with ``PYTHONPATH`` set in normal mode or
        removed in compat mode.
    """
    pythonpath = server_pythonpath(repo_root)
    if pythonpath is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = pythonpath
    return env


# ── Version resolution + skip ──────────────────────────────────────────


def release_tuple(version: str) -> tuple[int, ...]:
    """
    PEP 440 release tuple, ignoring ``.devN`` / ``rc`` / ``.postN`` suffixes.

    Comparing on the release tuple lets a development version of ``X``
    satisfy ``min_server_version("X")`` — main (e.g. ``0.1.2.dev0``) must
    run its own just-landed features even though ``0.1.2.dev0 < 0.1.2``
    under full PEP 440 ordering.

    :param version: A version string, e.g. ``"0.1.2.dev0"`` or ``"0.1.1"``.
    :returns: The release tuple, e.g. ``(0, 1, 2)`` or ``(0, 1, 1)``.
    """
    return Version(version).release


def meets_min_server_version(server_version: str, required: str) -> bool:
    """
    Whether *server_version* is new enough to run a *required*-gated test.

    :param server_version: The running server's version, e.g. ``"0.1.1"``.
    :param required: The ``min_server_version`` marker argument, e.g.
        ``"0.1.2"``.
    :returns: ``True`` iff the server's release tuple is ``>=`` the
        required release tuple.
    """
    return release_tuple(server_version) >= release_tuple(required)


def reconcile_server_version(
    reported: str | None,
    override: str | None,
    *,
    source: str = "server",
) -> str:
    """
    Combine the ``/api/version`` report with the env backstop into one version.

    Pure decision logic (no I/O) so the precedence/fail-loud rules are
    unit-testable. ``/api/version`` is the source of truth; the env backstop
    is used only when the report is missing, and otherwise cross-checked.

    :param reported: Version from ``GET /api/version``, or ``None`` if it
        couldn't be read.
    :param override: ``OMNIGENT_COMPAT_SERVER_VERSION`` value, or ``None``.
    :param source: Base URL (for the error message), e.g.
        ``"http://localhost:6767"``.
    :returns: The reconciled server version string, e.g. ``"0.1.1"``.
    :raises RuntimeError: If the report is missing and no backstop is set, or
        if the report and backstop release tuples disagree.
    """
    if reported is None:
        if override is not None:
            return override
        raise RuntimeError(
            f"could not read {source}/api/version and no {COMPAT_SERVER_VERSION_ENV} "
            f"backstop is set"
        )
    if override is not None and release_tuple(override) != release_tuple(reported):
        raise RuntimeError(
            f"server version mismatch: /api/version reports {reported!r} but "
            f"{COMPAT_SERVER_VERSION_ENV}={override!r}. The pinned old server may be "
            f"shadowed by the worktree via PYTHONPATH — see "
            f"docs/SERVER_VERSION_COMPAT_CI.md."
        )
    return reported


def _fetch_reported_version(base_url: str) -> str | None:
    """
    Read ``GET /api/version``, returning ``None`` if it can't be obtained.

    :param base_url: Live server base URL, e.g. ``"http://localhost:6767"``.
    :returns: The reported version string, or ``None`` on any HTTP/parse
        error.
    """
    try:
        return httpx.get(f"{base_url}/api/version", timeout=10).json()["version"]
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def resolve_server_version(base_url: str) -> str:
    """
    Resolve the running server's version (source of truth: ``GET /api/version``).

    Thin I/O wrapper over :func:`reconcile_server_version`. The env backstop
    ``OMNIGENT_COMPAT_SERVER_VERSION`` covers an unreadable endpoint and
    cross-checks the report (mismatch → raise; the tripwire for the
    PYTHONPATH-shadow regression).

    :param base_url: Live server base URL, e.g. ``"http://localhost:6767"``.
    :returns: The server version string, e.g. ``"0.1.1"``.
    :raises RuntimeError: See :func:`reconcile_server_version`.
    """
    override = os.environ.get(COMPAT_SERVER_VERSION_ENV) or None
    return reconcile_server_version(_fetch_reported_version(base_url), override, source=base_url)
