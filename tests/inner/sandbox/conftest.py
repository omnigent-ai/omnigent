"""
Shared fixtures for the cross-backend sandbox behavior suite.

The tests under :mod:`tests.inner.sandbox` exercise the *observable*
contract every active sandbox backend must uphold: cwd RO-by-default,
writable scratch tmpdir, dotfile masking, env passthrough, hard
network deny, etc. They run against whichever spawn-time backend the
current host can use — ``linux_bwrap`` on Linux + ``bwrap`` installed,
``darwin_seatbelt`` on macOS + ``sandbox-exec`` on ``PATH``. A host
that satisfies neither (e.g. a bare Linux box without ``bwrap``)
simply skips every test in the suite — the assertions stay correct,
they just don't run.

The fixtures here are the one source of truth for:

- which backend is "active" on the current host
  (:func:`active_sandbox_type`),
- how to build an :class:`OSEnvSandboxSpec` for that backend with the
  repo root pre-added to ``read_paths`` so helper subprocesses can
  ``import omnigent.*`` from a tempdir cwd
  (:func:`active_sandbox_spec_factory`),
- how to materialise the PYTHONPATH env var the helper inherits
  (:func:`sandbox_pythonpath_env`),
- the ``_run_async`` helper that drives a single coroutine to
  completion on a fresh event loop.

A test using these fixtures runs once per supported backend; there is
no platform branching inside the tests themselves.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from omnigent.inner.datamodel import CredentialProxySpec, OSEnvSandboxSpec

_BWRAP_AVAILABLE = shutil.which("bwrap") is not None
_SANDBOX_EXEC_AVAILABLE = shutil.which("sandbox-exec") is not None


def _repo_root_for_pythonpath() -> str:
    """
    Return the absolute repo-root path so a sandbox-spawned helper can
    reach the ``omnigent`` package via ``PYTHONPATH``.

    Helper subprocesses run with ``cwd`` set to a throwaway tempdir
    in these tests, so they can't ``import omnigent`` unless the
    repo root is on ``sys.path`` and visible inside the sandbox.

    :returns: Absolute path to the repository root containing the
        ``omnigent/`` package.
    """
    # tests/inner/sandbox/conftest.py → tests/inner/sandbox/ →
    # tests/inner/ → tests/ → repo root.
    return str(Path(__file__).resolve().parents[3])


@pytest.fixture(
    params=[
        pytest.param(
            "linux_bwrap",
            id="linux_bwrap",
            marks=pytest.mark.skipif(
                not (sys.platform.startswith("linux") and _BWRAP_AVAILABLE),
                reason="linux_bwrap requires Linux + bwrap on PATH",
            ),
        ),
        pytest.param(
            "darwin_seatbelt",
            id="darwin_seatbelt",
            marks=pytest.mark.skipif(
                not (sys.platform == "darwin" and _SANDBOX_EXEC_AVAILABLE),
                reason="darwin_seatbelt requires macOS + sandbox-exec on PATH",
            ),
        ),
    ]
)
def active_sandbox_type(request: pytest.FixtureRequest) -> str:
    """
    Yield each spawn-time sandbox backend supported on the current host.

    The parametrization runs once per backend; skip marks ensure
    Linux CI only runs the ``linux_bwrap`` branch and macOS CI only
    runs the ``darwin_seatbelt`` branch. A host that can't satisfy
    either branch skips every parametrization.

    :returns: The backend identifier ``"linux_bwrap"`` or
        ``"darwin_seatbelt"``.
    """
    backend_type: str = request.param
    return backend_type


@pytest.fixture
def active_sandbox_spec_factory(
    active_sandbox_type: str,
) -> Callable[..., OSEnvSandboxSpec]:
    """
    Return a factory that builds an :class:`OSEnvSandboxSpec` for the
    currently parametrized backend.

    The factory pre-adds the repo root to ``read_paths`` so the
    helper subprocess can ``import omnigent.*`` from a tempdir
    cwd. Both backends need this because they otherwise hide
    everything outside cwd / the default system mounts.

    Callers can override / extend the spec via keyword arguments
    (``write_paths``, ``env_passthrough``, ``egress_rules``,
    ``allow_network``, ``cwd_allow_hidden``).

    :returns: A callable that returns a fresh
        :class:`OSEnvSandboxSpec` instance per invocation.
    """
    repo_root = _repo_root_for_pythonpath()

    def _make(
        *,
        write_paths: list[str] | None = None,
        env_passthrough: list[str] | None = None,
        egress_rules: list[str] | None = None,
        allow_network: bool = True,
        cwd_allow_hidden: list[str] | None = None,
        extra_read_paths: list[str] | None = None,
        egress_allow_private_destinations: bool = False,
        credential_proxy: CredentialProxySpec | None = None,
    ) -> OSEnvSandboxSpec:
        read_paths = [repo_root]
        if extra_read_paths:
            read_paths.extend(extra_read_paths)
        return OSEnvSandboxSpec(
            type=active_sandbox_type,
            read_paths=read_paths,
            write_paths=write_paths,
            allow_network=allow_network,
            cwd_allow_hidden=cwd_allow_hidden,
            env_passthrough=env_passthrough,
            egress_rules=egress_rules,
            egress_allow_private_destinations=egress_allow_private_destinations,
            credential_proxy=credential_proxy,
        )

    return _make


@pytest.fixture
def sandbox_pythonpath_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Inject the repo root into the helper subprocess's ``PYTHONPATH``.

    Helpers spawned with ``cwd`` set to a tempdir would otherwise
    fail with ``ModuleNotFoundError`` because the agent's own
    bootstrap runs ``import omnigent.inner.os_env`` in the helper.
    The active backend's ``env_passthrough`` is filtered, but
    ``PYTHONPATH`` is set explicitly on the spawn env so this
    fixture just preserves it for the parent process.
    """
    repo = _repo_root_for_pythonpath()
    existing = os.environ.get("PYTHONPATH")
    new = f"{repo}{os.pathsep}{existing}" if existing else repo
    monkeypatch.setenv("PYTHONPATH", new)


def run_async(coro: Any) -> Any:
    """
    Drive a coroutine to completion on a fresh event loop.

    Helper exposed at module level so tests can call it without a
    fixture. Mirrors the ``_run_async`` helper in
    ``tests/inner/test_os_env.py`` so the migrated tests behave
    identically.

    :param coro: The coroutine to await.
    :returns: Whatever the coroutine returns.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
