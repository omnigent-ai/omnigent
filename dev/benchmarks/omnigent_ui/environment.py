"""UI-benchmark environment: server + runner + mock LLM + a served SPA.

:class:`UIEnvironment` extends the HTTP benchmark's :class:`BenchEnvironment`
(``dev/benchmarks/omnigent/environment.py``) — which already spawns the mock
LLM, ``omni server``, and a runner with readiness polling — and adds the one
thing a browser benchmark needs that the HTTP one doesn't: a **built web SPA**
on disk so the spawned server mounts and serves it at ``base_url``. A Playwright
browser then navigates there.

The runner is always on (``with_runner=True``): every journey renders the real
SPA and at least one drives a streamed turn. No host daemon is needed — the
first-token journey uses the boot runner via an in-session composer turn rather
than the host-launched cold path (see ``journeys.py``), so ``with_host`` stays
off to keep the run fast and deterministic.

The SPA build is serialized by the same cross-process file lock the e2e_ui
suite uses (``web/.build.lock``), and can be skipped with ``skip_build=True``
when a build is already present (CI builds once, up front).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import filelock

from dev.benchmarks.omnigent.environment import BenchEnvironment

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _REPO_ROOT / "web"
_BUILD_OUTPUT = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"
_BUILD_LOCK = _WEB_DIR / ".build.lock"
# Vite's emptyOutDir nukes the dir before writing, so concurrent worktrees would
# clobber each other; the lock serializes builds. Generous: a cold npm ci +
# build on a fresh tree runs into minutes.
_BUILD_LOCK_TIMEOUT_S = 600.0

# Files a usable SPA build must contain (subset of the e2e_ui PWA assertion —
# we only need "the app shell is present", not the service-worker contract).
_REQUIRED_BUILD_FILES = ("index.html",)


class SPABuildError(RuntimeError):
    """The web SPA build is missing or failed."""


def ensure_spa_built(*, skip_build: bool) -> None:
    """Build the web SPA into ``omnigent/server/static/web-ui/`` (once).

    Mirrors the e2e_ui ``built_spa`` fixture: ``npm ci --legacy-peer-deps``
    then ``npm run build``, serialized by ``web/.build.lock``. With
    *skip_build* set, only asserts an existing build is present.

    :param skip_build: Reuse whatever is already in the build dir instead of
        rebuilding. Raises if no build is present.
    :raises SPABuildError: If the build is missing (skip mode) or npm fails.
    """
    if skip_build:
        _assert_build_present()
        return

    with filelock.FileLock(str(_BUILD_LOCK), timeout=_BUILD_LOCK_TIMEOUT_S):
        # --legacy-peer-deps matches the workflow + e2e_ui fixture: the lockfile
        # already pins the tree, so this avoids a slow React 19 peer re-resolve.
        try:
            subprocess.run(
                ["npm", "ci", "--legacy-peer-deps", "--no-audit", "--no-fund"],
                cwd=_WEB_DIR,
                check=True,
            )
            subprocess.run(["npm", "run", "build"], cwd=_WEB_DIR, check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SPABuildError(f"web SPA build failed: {exc}") from exc
    _assert_build_present()


def _assert_build_present() -> None:
    """Fail if the built SPA app shell is missing from the output dir."""
    for name in _REQUIRED_BUILD_FILES:
        if not (_BUILD_OUTPUT / name).is_file():
            raise SPABuildError(
                f"SPA build missing {name} at {_BUILD_OUTPUT} — run without "
                "--skip-build, or build web/ first (npm ci && npm run build)."
            )


class UIEnvironment(BenchEnvironment):
    """A :class:`BenchEnvironment` that also serves the web SPA for a browser.

    Always boots with a runner (the SPA + streamed turns need it); no host
    daemon. Building on the base class means the mock-LLM control
    (``configure_mock`` / ``set_mock_fallback``), session primitives
    (``ensure_agent`` / ``create_bound_session`` / ``drive_turn`` /
    ``seed_items``), and the resource sampler all come for free.

    :param database_uri: DB the server boots against; ``None`` uses a fresh
        throwaway SQLite file (the empty-DB path).
    :param skip_build: Reuse an existing SPA build instead of rebuilding.
    """

    def __init__(
        self,
        *,
        database_uri: str | None = None,
        skip_build: bool = False,
    ) -> None:
        super().__init__(with_runner=True, with_host=False, database_uri=database_uri)
        self._skip_build = skip_build

    async def __aenter__(self) -> UIEnvironment:
        # Build the SPA before the server boots so it mounts the static bundle.
        # Synchronous npm work off the event loop.
        import asyncio

        await asyncio.to_thread(ensure_spa_built, skip_build=self._skip_build)
        await super().__aenter__()
        return self
