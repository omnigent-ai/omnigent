"""Guard: the CLI startup import graph stays free of avoidable weight.

Every `omnigent` invocation pays for `omnigent.cli`'s module graph before
Click dispatches, so a heavy import on that path is a tax on `--version`,
`--help`, and tab-completion alike. `httpx` costs ~40ms to import and is
reached only by two sandbox-bootstrap probe helpers that run when a user
actually provisions a remote sandbox.
"""

from __future__ import annotations

import subprocess
import sys

# Reached only from `_probe_server` / `_workspace_org_id`, both of which
# import it at call time.
_MUST_NOT_LOAD = ("httpx",)


def test_cli_import_does_not_load_httpx() -> None:
    """Importing the CLI must not build an HTTP client stack."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main\nimport sys\n"
            f"print(sorted(m for m in {_MUST_NOT_LOAD!r} if m in sys.modules))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "[]", f"omnigent.cli pulled in: {proc.stdout.strip()}"


def test_sandbox_probes_still_reach_httpx() -> None:
    """The deferred import must actually resolve when a probe runs.

    An unreachable host exercises the ``except httpx.HTTPError`` branch,
    which is where a missing module-level import would surface as a
    ``NameError`` rather than a clean ``None``.
    """
    from omnigent.onboarding.sandboxes.bootstrap import _probe_server, _workspace_org_id

    assert _probe_server("http://127.0.0.1:1") is None
    assert _workspace_org_id("http://127.0.0.1:1") is None
