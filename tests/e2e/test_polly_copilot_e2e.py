"""Gated real-network e2e: polly's orchestrator brain on the GitHub Copilot SDK.

Unlike the mock-LLM smoke in ``test_polly_e2e.py`` (which swaps polly's brain to
``openai-agents`` against a fake server), the Copilot SDK talks only to GitHub's
Copilot backend, so this exercises the REAL harness. It is **skipped** unless a
Copilot-capable GitHub token is resolvable — the ``copilot:`` config block
written by ``omnigent setup``, or an ambient ``COPILOT_GITHUB_TOKEN`` /
``GH_TOKEN`` / ``GITHUB_TOKEN`` — so CI without a token skips it, mirroring how
the harness probes skip when a CLI binary is absent from ``PATH``.

It boots a throwaway local server from this working tree (which carries polly's
in-tree ``omnigent.inner.nessie.policies`` guardrails, resolved server-side) and
runs the real ``examples/polly`` bundle with its orchestrator brain overridden
to ``--harness copilot``. The committed assertion is a brain-only smoke (boots +
coherent reply). The full dispatch→collect→synthesize orchestration loop on a
copilot brain is exercised by the ``copilot-sdk-e2e-dev`` skill's polly driver
(smoke / fanout / review-pr CUJs), which additionally needs the sub-agent CLIs.

Run manually (with a Copilot token configured)::

    pytest tests/e2e/test_polly_copilot_e2e.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_polly_e2e import (
    _MIN_REPLY_CHARS,
    _POLLY,
    _REPO,
    _SERVER_BOOT_TIMEOUT_SEC,
    _free_port,
    _wait_for_health,
)

# A real Copilot turn (network round-trip to GitHub's backend + bundled-CLI
# spin-up) is slower than the mock path; give it a generous one-shot bound.
_COPILOT_RUN_TIMEOUT_SEC = 280


def _copilot_token_available() -> bool:
    """Return whether a Copilot-capable GitHub token is resolvable on this host."""
    try:
        from omnigent.onboarding.copilot_auth import (
            COPILOT_TOKEN_ENV_VARS,
            copilot_github_token_configured,
        )
    except Exception:  # pragma: no cover - defensive: copilot auth module missing
        return False
    if copilot_github_token_configured():
        return True
    return any(os.environ.get(var) for var in COPILOT_TOKEN_ENV_VARS)


pytestmark = pytest.mark.skipif(
    not _copilot_token_available(),
    reason=(
        "no Copilot-capable GitHub token configured "
        "(copilot: config block or COPILOT_GITHUB_TOKEN / GH_TOKEN / GITHUB_TOKEN)"
    ),
)


@pytest.fixture
def local_polly_server_real(tmp_path: Path) -> Iterator[str]:
    """Boot a throwaway local ``omnigent server`` from this working tree.

    Unlike ``test_polly_e2e.local_polly_server`` this does NOT strip the
    developer's credentials — the Copilot harness needs the real GitHub token to
    reach GitHub's backend. Own sqlite DB + artifact dir under ``tmp_path`` keep
    it isolated from the developer's real state.

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: The base URL of the running server, e.g. ``"http://127.0.0.1:8811"``.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'polly_copilot_e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        cwd=str(_REPO),
        env={**os.environ, "OMNIGENT_SKIP_ONBOARD": "1", "OMNIGENT_NO_UPDATE_CHECK": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, time.monotonic() + _SERVER_BOOT_TIMEOUT_SEC)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_polly_brain_on_copilot_boots_and_responds(local_polly_server_real: str) -> None:
    """polly with a ``--harness copilot`` brain boots and returns a coherent reply.

    Runs the real ``examples/polly`` bundle with its orchestrator brain
    overridden to the Copilot SDK harness (``--harness copilot --model auto``)
    against the local server, and asserts exit 0 + a non-trivial reply. This is
    the regression guard that the Copilot SDK harness can serve as polly's brain
    end-to-end: GitHub token resolution, egress, the bundled Copilot CLI, the
    harness wrap, and server-side guardrail resolution all working together. A
    blank reply or non-zero exit means the copilot brain can't drive polly.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(_POLLY),
            "--server",
            local_polly_server_real,
            "--harness",
            "copilot",
            "--model",
            "auto",
            "-p",
            "In one short sentence, what are you and how do you handle a coding task?",
        ],
        cwd=str(_REPO),
        env={**os.environ, "OMNIGENT_SKIP_ONBOARD": "1", "OMNIGENT_NO_UPDATE_CHECK": "1"},
        capture_output=True,
        text=True,
        timeout=_COPILOT_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"polly(copilot) run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    reply = result.stdout.strip()
    assert len(reply) >= _MIN_REPLY_CHARS, (
        f"polly(copilot) produced no/short reply ({len(reply)} chars): {reply!r}\n"
        f"--- stderr ---\n{result.stderr}"
    )
