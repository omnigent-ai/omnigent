"""
Helpers for the version backwards-compatibility harness.

See ``docs/SERVER_VERSION_COMPAT_CI.md``. Two independent redirect knobs and
their version skips:

1. **Server redirect (Config 1)** — pin the ``omnigent.cli server`` subprocess
   to an older build (``OMNIGENT_COMPAT_SERVER_PYTHON``) while the client,
   runner, host, and tests stay on main. Skip newer-than-server features with
   ``@pytest.mark.min_server_version(...)``.
2. **Runner/host redirect (Config 2)** — pin the ``omnigent.runner._entry`` and
   ``omnigent.host._daemon_entry`` subprocesses to an older build
   (``OMNIGENT_COMPAT_RUNNER_PYTHON``) while the server, client, and tests stay
   on main. Runner and host are colocated (one install, one version), so a
   single knob governs both. Skip newer-than-runner features with
   ``@pytest.mark.min_runner_version(...)``.

The two knobs are orthogonal: each spawn site consults its own knob, so a run
pins the server OR the runner/host (or neither), never shadowing the other.

Outside compat mode (no env var set) every function here is inert and the test
harness behaves exactly as before.
"""

from __future__ import annotations

import os
import sys
import tempfile

import httpx
from packaging.version import Version

# CWD caches keyed by component label ("server" / "runner"), created lazily.
_compat_cwds: dict[str, str] = {}

# Interpreter for the SERVER subprocess. Set to a venv python holding the
# pinned older build; unset in normal runs (use the test process's python).
COMPAT_SERVER_PYTHON_ENV = "OMNIGENT_COMPAT_SERVER_PYTHON"
# Version string the workflow pinned (e.g. "0.1.1"). Backstop / cross-check
# for the server skip logic — never used to launch anything.
COMPAT_SERVER_VERSION_ENV = "OMNIGENT_COMPAT_SERVER_VERSION"
# Interpreter for the RUNNER and HOST subprocesses (colocated → one knob).
COMPAT_RUNNER_PYTHON_ENV = "OMNIGENT_COMPAT_RUNNER_PYTHON"
# Version string the workflow pinned for the runner/host. The runner and host
# expose no ``/api/version`` endpoint, so this env var is the *only* source for
# the ``min_runner_version`` skip (no live cross-check).
COMPAT_RUNNER_VERSION_ENV = "OMNIGENT_COMPAT_RUNNER_VERSION"


# ── Redirect core (shared by server + runner/host) ─────────────────────


def _compat_python(env_var: str) -> str | None:
    """
    The pinned-build interpreter named by *env_var*, or ``None``.

    :param env_var: The redirect env var, e.g.
        ``"OMNIGENT_COMPAT_SERVER_PYTHON"``.
    :returns: The venv python path (e.g. ``"/tmp/old-env/bin/python"``) when
        that component's compat mode is active, else ``None``.
    """
    return os.environ.get(env_var) or None


def _compat_executable(env_var: str) -> str:
    """
    Interpreter to launch a component's subprocess with.

    :param env_var: The component's redirect env var.
    :returns: The pinned interpreter in compat mode, else ``sys.executable``.
    """
    return _compat_python(env_var) or sys.executable


def _compat_cwd(env_var: str, label: str) -> str | None:
    """
    A neutral working directory for a redirected subprocess, or ``None``.

    ``python -m omnigent...`` puts the CWD on ``sys.path[0]``, so a subprocess
    launched from the repo checkout would import the worktree's ``omnigent/``
    package — shadowing the pinned older install exactly like a ``PYTHONPATH``
    prepend would. A stable empty directory forces the pinned venv's installed
    ``omnigent`` to resolve.

    :param env_var: The component's redirect env var.
    :param label: Component label for the cache + temp-dir prefix, e.g.
        ``"server"`` or ``"runner"``.
    :returns: A stable empty directory path in compat mode; ``None`` otherwise
        (inherit the parent's CWD — today's behavior).
    """
    if _compat_python(env_var) is None:
        return None
    if label not in _compat_cwds:
        _compat_cwds[label] = tempfile.mkdtemp(prefix=f"omnigent-compat-{label}-cwd-")
    return _compat_cwds[label]


# ── Server redirect (Config 1) ─────────────────────────────────────────


def compat_server_python() -> str | None:
    """
    Interpreter the server subprocess should run under, or ``None``.

    :returns: The value of ``OMNIGENT_COMPAT_SERVER_PYTHON`` (a venv python
        path, e.g. ``"/tmp/server-env/bin/python"``) when compat mode is
        active, else ``None``.
    """
    return _compat_python(COMPAT_SERVER_PYTHON_ENV)


def server_executable() -> str:
    """
    Interpreter to launch ``omnigent.cli server`` with.

    :returns: The compat interpreter in compat mode, else ``sys.executable``
        (the test process's own python).
    """
    return _compat_executable(COMPAT_SERVER_PYTHON_ENV)


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

    See :func:`_compat_cwd`. In compat mode the server runs from a stable
    empty directory so the worktree's ``omnigent/`` doesn't shadow the pinned
    older install via ``sys.path[0]``.

    :returns: A stable empty directory path in compat mode; ``None`` outside
        compat mode (inherit the parent's CWD — today's behavior).
    """
    return _compat_cwd(COMPAT_SERVER_PYTHON_ENV, "server")


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


# ── Runner / host redirect (Config 2) ──────────────────────────────────


def compat_runner_python() -> str | None:
    """
    Interpreter the runner and host subprocesses should run under, or ``None``.

    :returns: The value of ``OMNIGENT_COMPAT_RUNNER_PYTHON`` (a venv python
        path) when runner/host compat mode is active, else ``None``.
    """
    return _compat_python(COMPAT_RUNNER_PYTHON_ENV)


def runner_executable() -> str:
    """
    Interpreter to launch ``omnigent.runner._entry`` / ``omnigent.host._daemon_entry``.

    :returns: The pinned-old interpreter in runner compat mode, else
        ``sys.executable`` (the test process's own python = main).
    """
    return _compat_executable(COMPAT_RUNNER_PYTHON_ENV)


def compat_runner_cwd() -> str | None:
    """
    Working directory for the runner/host subprocess, or ``None`` to inherit.

    See :func:`_compat_cwd` — neutral dir in runner compat mode so the worktree
    doesn't shadow the pinned old runner/host install.

    :returns: A stable empty directory path in runner compat mode; ``None``
        otherwise.
    """
    return _compat_cwd(COMPAT_RUNNER_PYTHON_ENV, "runner")


def apply_runner_env(env: dict[str, str]) -> dict[str, str]:
    """
    Neutralize ``PYTHONPATH`` on a runner/host-subprocess env dict in compat mode.

    Unlike :func:`apply_server_env`, this is **neutralize-only**: in runner
    compat mode it drops any inherited ``PYTHONPATH`` (so the pinned old build
    in the compat venv resolves instead of being shadowed by the worktree);
    outside compat mode it leaves *env* untouched. It never *adds* a prepend,
    because the runner inherits its base env from the server fixture and the
    host tests each set their own ``PYTHONPATH`` convention — forcing a prepend
    here would change normal-mode behavior.

    Mutates *env* in place (and returns it).

    :param env: The runner/host subprocess environment being assembled.
    :returns: The same dict, with ``PYTHONPATH`` removed in runner compat mode
        and unchanged otherwise.
    """
    if compat_runner_python() is not None:
        env.pop("PYTHONPATH", None)
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


def meets_min_version(actual: str, required: str) -> bool:
    """
    Whether *actual* is new enough to run a *required*-gated test.

    Component-agnostic release-tuple comparison shared by the server and
    runner skips.

    :param actual: The running component's version, e.g. ``"0.1.1"``.
    :param required: The marker's minimum version, e.g. ``"0.1.2"``.
    :returns: ``True`` iff *actual*'s release tuple is ``>=`` *required*'s.
    """
    return release_tuple(actual) >= release_tuple(required)


def meets_min_server_version(server_version: str, required: str) -> bool:
    """
    Whether *server_version* is new enough to run a *required*-gated test.

    :param server_version: The running server's version, e.g. ``"0.1.1"``.
    :param required: The ``min_server_version`` marker argument, e.g.
        ``"0.1.2"``.
    :returns: ``True`` iff the server's release tuple is ``>=`` the
        required release tuple.
    """
    return meets_min_version(server_version, required)


def meets_min_runner_version(runner_version: str, required: str) -> bool:
    """
    Whether *runner_version* is new enough to run a *required*-gated test.

    :param runner_version: The pinned runner/host version, e.g. ``"0.2.0"``.
    :param required: The ``min_runner_version`` marker argument, e.g.
        ``"0.2.1"``.
    :returns: ``True`` iff the runner's release tuple is ``>=`` the required
        release tuple.
    """
    return meets_min_version(runner_version, required)


def pinned_runner_version() -> str | None:
    """
    The pinned runner/host version from the env backstop, or ``None``.

    The runner and host have no ``/api/version`` endpoint to query, so unlike
    :func:`resolve_server_version` there is no live source to reconcile — the
    workflow-set ``OMNIGENT_COMPAT_RUNNER_VERSION`` is authoritative. ``None``
    (normal runs) means "newest / unbounded", so no ``min_runner_version`` test
    is skipped.

    :returns: The pinned version string (e.g. ``"0.2.0"``), or ``None`` outside
        runner compat mode.
    """
    return os.environ.get(COMPAT_RUNNER_VERSION_ENV) or None


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
