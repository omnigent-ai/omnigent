"""Guard the lazy import seams that keep ``omnigent.host.connect`` cheap.

Importing the host connector is one of the two dominant stages of a cold
``omnigent host`` start, and three eager module-scope imports used to inflate
it: the credential writer (``onboarding.harness_auth``), the spec parser
reached through ``onboarding.provider_config``, and — from the other side — the
CLI's own daemon-env helper importing the whole connector just to read two
frozensets.

Each edge is now deferred: ``harness_auth`` loads inside the rare
``host.store_secret`` / ``host.detect_credentials`` handlers, ``spec.parser``
inside the credential-resolution helpers, and the runner env allowlist lives in
the leaf module :mod:`omnigent.host.runner_env`. Without these assertions the
edges grow back silently, so each probe runs in a fresh subprocess (an
unrelated test in the same session can otherwise pre-import the heavy module
and mask the regression).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

# Hand each child the same import roots as this process so it resolves
# ``omnigent`` to the code under test (worktree or installed package).
_CHILD_ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}


def _assert_absent_after_import(light: str, heavy: str) -> None:
    """Import *light* in a fresh interpreter and assert *heavy* stayed unloaded.

    :param light: Module the hot path imports, e.g. ``"omnigent.host.connect"``.
    :param heavy: Module that must NOT be dragged in, e.g.
        ``"omnigent.onboarding.harness_auth"``.
    :returns: None.
    """
    probe = (
        f"import sys, {light}\n"
        f"assert {heavy!r} not in sys.modules, "
        f"{f'{heavy} was imported eagerly by {light}'!r}\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=_CHILD_ENV,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"importing {light} pulled in {heavy}; the lazy import seam "
        f"regressed. stderr:\n{result.stderr}"
    )


@pytest.mark.parametrize(
    ("light", "heavy"),
    [
        # The credential writer is only needed by the rare store_secret /
        # detect_credentials frames, not by every host-daemon start.
        ("omnigent.host.connect", "omnigent.onboarding.harness_auth"),
        # provider_config reaches the spec parser only when resolving a secret.
        ("omnigent.onboarding.provider_config", "omnigent.spec.parser"),
        # ... and neither of connect's two routes to it may reinstate the edge.
        ("omnigent.onboarding.harness_readiness", "omnigent.spec.parser"),
        ("omnigent.host.connect", "omnigent.spec.parser"),
        # The runner env allowlist must stay a leaf the CLI can read cheaply.
        ("omnigent.host.runner_env", "omnigent.host.connect"),
    ],
)
def test_host_connect_import_graph_stays_lazy(light: str, heavy: str) -> None:
    """Each light module loads without dragging in its heavy neighbour.

    :param light: The module on the startup path.
    :param heavy: The module that must stay unloaded.
    :returns: None.
    """
    _assert_absent_after_import(light, heavy)


def test_build_host_daemon_env_does_not_import_host_connect() -> None:
    """``_build_host_daemon_env`` reads the allowlist without loading the connector.

    The helper used to import ``omnigent.host.connect`` inside its body purely
    to read two frozensets, paying the whole connector import tree on the first
    call during ``omnigent host`` startup.

    :returns: None.
    """
    probe = (
        "import sys\n"
        "from omnigent.cli import _build_host_daemon_env\n"
        "_build_host_daemon_env(server_url=None)\n"
        "_build_host_daemon_env(server_url='https://example.databricksapps.com')\n"
        "assert 'omnigent.host.connect' not in sys.modules, "
        "'_build_host_daemon_env imported the host connector'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=_CHILD_ENV,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"_build_host_daemon_env pulled in omnigent.host.connect; the "
        f"runner_env leaf-module seam regressed. stderr:\n{result.stderr}"
    )


def test_connect_still_re_exports_the_runner_env_allowlist() -> None:
    """Moving the allowlist to a leaf module kept ``connect``'s names identical.

    :returns: None.
    """
    from omnigent.host import runner_env
    from omnigent.host.connect import (
        _RUNNER_ENV_ALLOWLIST,
        _RUNNER_ENV_ALLOWLIST_PREFIXES,
    )

    assert _RUNNER_ENV_ALLOWLIST is runner_env._RUNNER_ENV_ALLOWLIST
    assert _RUNNER_ENV_ALLOWLIST_PREFIXES is runner_env._RUNNER_ENV_ALLOWLIST_PREFIXES
    # Spot-check the contract the allowlist exists to enforce: process
    # essentials in, host-owner provider secrets out.
    assert {"PATH", "HOME"} <= _RUNNER_ENV_ALLOWLIST
    assert "ANTHROPIC_API_KEY" not in _RUNNER_ENV_ALLOWLIST
