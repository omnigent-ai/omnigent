"""E2E: a failed git-based upgrade must point users at the package index.

Journey: omnigent was installed from the GitHub git URL
(``git+https://github.com/omnigent-ai/omnigent.git``). On a Databricks
machine github.com is unreachable, so ``omni upgrade`` re-pulls the tracked
git ref, the underlying installer's git fetch fails ("Could not resolve
host: github.com"), and the CLI reports the failure. The failure message
must tell the user about the package-index (PyPI mirror) install path —
Databricks users cannot reach GitHub at all, so pointing them back at the
same blocked git URL leaves them stuck.

Hermetic: the "git install" is a staged PEP 610 ``direct_url.json`` overlay
placed ahead of the real distribution on ``PYTHONPATH``, and the blocked
network is an unroutable proxy, so no real GitHub access (or lack of it) is
required — the real ``omni upgrade`` → ``uv tool install`` → ``git fetch``
chain runs and fails exactly as it does on a Databricks box.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="stages a POSIX install overlay with a fake HOME"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GIT_URL = "https://github.com/omnigent-ai/omnigent.git"
#: TCP port 9 (discard) is closed on any sane box, so a proxy pointed at it
#: refuses instantly — git/uv fail fast instead of hanging on a DNS timeout.
_UNROUTABLE_PROXY = "http://127.0.0.1:9"


@pytest.fixture()
def git_install_overlay(tmp_path: Path) -> dict[str, str]:
    """Stage omnigent as a git-shaped installed wheel, plus a clean HOME.

    The overlay directory holds a copy of the ``omnigent`` package (so
    ``_find_repo_root`` does not classify the run as a source checkout) and
    a ``.dist-info`` whose ``direct_url.json`` records the GitHub VCS URL —
    the exact shape ``uv tool install git+https://github.com/...`` leaves
    behind. Returns the subprocess environment for driving ``omni upgrade``.
    """
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required: the git-install upgrade path shells out to uv")

    version = importlib.metadata.version("omnigent")
    overlay = tmp_path / "overlay"
    home = tmp_path / "home"
    home.mkdir()

    # The package itself, importable from a non-checkout location.
    shutil.copytree(
        _REPO_ROOT / "omnigent",
        overlay / "omnigent",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    dist_info = overlay / f"omnigent-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: omnigent\nVersion: {version}\n"
    )
    (dist_info / "INSTALLER").write_text("uv\n")
    (dist_info / "RECORD").write_text("")
    (dist_info / "direct_url.json").write_text(
        json.dumps(
            {
                "url": _GIT_URL,
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": "9f2c1a7d3e5b4a6c8d0e2f4a6b8c0d2e4f6a8b0c",
                },
            }
        )
    )

    path_entries = [
        str(Path(sys.executable).parent),
        str(Path(uv).parent),
        "/usr/bin",
        "/bin",
    ]
    return {
        "HOME": str(home),
        "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
        "PYTHONPATH": str(overlay),
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "GIT_TERMINAL_PROMPT": "0",
        # Block github.com the way a Databricks network does: every HTTPS
        # hop dies at the proxy, deterministically and immediately.
        "http_proxy": _UNROUTABLE_PROXY,
        "https_proxy": _UNROUTABLE_PROXY,
        "HTTP_PROXY": _UNROUTABLE_PROXY,
        "HTTPS_PROXY": _UNROUTABLE_PROXY,
        "UV_HTTP_TIMEOUT": "8",
        "UV_NO_PROGRESS": "1",
    }


def test_blocked_github_upgrade_failure_mentions_package_index(
    git_install_overlay: dict[str, str],
) -> None:
    """When the git re-pull can't reach GitHub, the error must offer PyPI.

    Drives the real user journey: ``omni upgrade`` on a git-shaped install
    with GitHub unreachable. The upgrade command fails (that part is the
    network, not the bug); the bug is that the resulting error message
    offers no package-index (PyPI mirror) alternative, which is the only
    install path that works on a Databricks machine.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "omnigent.cli", "upgrade"],
        env=git_install_overlay,
        # A neutral cwd: ``python -m`` prepends the cwd to ``sys.path``, so
        # running from the checkout would resolve the source tree instead of
        # the staged git-shaped install and trip the source-checkout guard.
        cwd=git_install_overlay["HOME"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=300,
    )
    combined = proc.stdout + proc.stderr

    # Precondition: the journey reached the failing git-based reinstall.
    # These assertions pin the scenario so the hint assertion below fails
    # only because of the missing guidance, never because of setup noise.
    assert proc.returncode != 0, f"expected the upgrade to fail:\n{combined}"
    assert "github.com" in combined, f"expected a github-based re-pull:\n{combined}"
    assert re.search(
        r"Could not resolve host|Failed to connect|unable to access|Git operation failed",
        combined,
    ), f"expected the git fetch to fail at the network layer:\n{combined}"

    # The bug: the failure message never mentions the package-index (PyPI
    # mirror) install path, leaving GitHub-blocked users with nothing but
    # the same broken git URL.
    assert re.search(r"(?i)\bpypi\b|package index", combined), (
        "the upgrade failure message must tell users who cannot reach GitHub "
        "to install from the package index (PyPI mirror) instead — it only "
        f"repeats the blocked git path:\n{combined}"
    )
