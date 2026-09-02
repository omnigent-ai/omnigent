"""E2E regression: a wrapper-injected managed ``--server`` must not break
``host status --all``.

The reported journey: a Databricks-internal deployment wraps the CLI behind
``isaac omni`` (the wrapper sets ``OMNIGENT_REQUIRE_WRAPPER`` /
``OMNIGENT_WRAPPER_BYPASS`` / ``OMNIGENT_WRAPPER_COMMAND`` and rewrites
invocations to add the managed MAS ``--server``). The user runs::

    isaac omni host status --all

and the wrapper hands Omnigent the equivalent of::

    omnigent host status --all --server <managed-server>

Omnigent's ``host status`` defines ``--server`` and ``--all`` as mutually
exclusive, so argument parsing dies with::

    Error: Use either --server or --all, not both.

before any status operation runs — the user cannot query all host/server
statuses through the supported wrapper at all, while the direct
``omnigent host status --all`` works.

This test drives the exact rewritten argv the wrapper produces (the isaac
binary itself is Databricks-internal and not available here), as a real CLI
subprocess under the wrapper's environment, with an isolated ``$HOME`` (empty
daemon registry) so the run is deterministic and offline.

Before a fix: the wrapped form exits 1 printing the mutual-exclusion error and
the ``all_targets`` case in this file FAILS.
After a fix (tolerate/strip a managed-server injection when ``--all`` is the
user's intent — or any equivalent that satisfies the acceptance criteria): the
wrapped form must reach the status operation and render the same all-targets
listing the direct command does, and this file PASSES. The direct-command
control must keep passing throughout.

Runs with no LLM, no isaac binary, and no network::

    pytest tests/e2e/test_host_status_all_with_wrapper_injected_server_e2e.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# The console error whose appearance IS the bug: emitted by the
# --server/--all mutual-exclusion guard in the host status record selector.
_MUTUAL_EXCLUSION_ERROR = "Use either --server or --all, not both."

# The all-targets listing an empty daemon registry renders — proof the status
# operation actually ran instead of dying at argument parsing.
_EMPTY_REGISTRY_LISTING = "No host daemons found."

# A managed MAS server URL of the shape the wrapper injects.
_MANAGED_SERVER = "https://managed.example.databricksapps.com"

# Env keys that would leak this machine's real Omnigent state, credentials, or
# runner identity into the subprocess under test.
_AMBIENT_ENV_PREFIXES = ("OMNIGENT_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "GOOGLE_", "DATABRICKS_")
_AMBIENT_ENV_KEYS = ("LLM_API_KEY", "CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION")

# The checkout under test. Pinned onto the subprocess PYTHONPATH so the child
# imports THIS tree's omnigent, not a site-packages install of another tree.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_env(fake_home: Path) -> dict[str, str]:
    """A subprocess env rooted at *fake_home* with ambient state removed."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_AMBIENT_ENV_PREFIXES) and k not in _AMBIENT_ENV_KEYS
    }
    env["HOME"] = str(fake_home)
    env["LC_ALL"] = "C.UTF-8"
    env["TERM"] = "dumb"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    return env


def _wrapper_env(fake_home: Path) -> dict[str, str]:
    """The env the ``isaac omni`` wrapper sets around its own CLI invocation."""
    env = _clean_env(fake_home)
    env["OMNIGENT_WRAPPER_COMMAND"] = "isaac omni"
    env["OMNIGENT_REQUIRE_WRAPPER"] = "1"
    env["OMNIGENT_WRAPPER_BYPASS"] = "1"
    return env


def _run_host_status(
    args: list[str],
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one ``omnigent host status`` CLI subprocess and capture its output."""
    return subprocess.run(
        [sys.executable, "-m", "omnigent", *args],
        capture_output=True,
        text=True,
        timeout=120.0,
        env=env,
        cwd=str(cwd),  # no project-level .omnigent config in scope
    )


@pytest.mark.parametrize(
    "argv",
    [
        # Trailing injection: the documented rewrite appends the managed
        # server to the subcommand's own options.
        pytest.param(
            ["host", "status", "--all", "--server", _MANAGED_SERVER],
            id="subcommand-level-injection",
        ),
        # Group-level injection: the managed server lands on the `host`
        # group; `host status` inherits it via the group option fallback.
        pytest.param(
            ["host", "--server", _MANAGED_SERVER, "status", "--all"],
            id="group-level-injection",
        ),
    ],
)
def test_wrapper_injected_managed_server_must_not_break_host_status_all(
    tmp_path: Path,
    argv: list[str],
) -> None:
    """The wrapper-rewritten ``host status --all`` must reach the status operation.

    Drives the argv the ``isaac omni`` wrapper hands Omnigent for
    ``isaac omni host status --all``, under the wrapper's environment. The
    command must not die at argument parsing with the mutual-exclusion error;
    it must render the same all-targets listing the direct command does (an
    empty registry under the isolated ``$HOME`` renders the no-daemons line).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    proc = _run_host_status(argv, _wrapper_env(fake_home), tmp_path)

    combined = f"{proc.stdout}\n{proc.stderr}"
    assert _MUTUAL_EXCLUSION_ERROR not in combined, (
        "The wrapper-injected managed --server tripped the --server/--all "
        "mutual-exclusion guard before any status operation ran — the exact "
        f"failure `isaac omni host status --all` hits.\nOutput:\n{combined}"
    )
    assert proc.returncode == 0, (
        f"wrapped `host status --all` exited {proc.returncode} instead of "
        f"rendering host status.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _EMPTY_REGISTRY_LISTING in proc.stdout, (
        "wrapped `host status --all` did not render the all-targets listing "
        f"(expected {_EMPTY_REGISTRY_LISTING!r} for an empty daemon registry).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_direct_host_status_all_keeps_working(tmp_path: Path) -> None:
    """Control: bare ``omnigent host status --all`` renders the listing.

    This is the direct invocation the bug report confirms works; any fix for
    the wrapped form must not regress it.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    proc = _run_host_status(["host", "status", "--all"], _clean_env(fake_home), tmp_path)

    assert proc.returncode == 0, (
        f"direct `host status --all` exited {proc.returncode}.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert _EMPTY_REGISTRY_LISTING in proc.stdout, (
        "direct `host status --all` did not render the all-targets listing.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
