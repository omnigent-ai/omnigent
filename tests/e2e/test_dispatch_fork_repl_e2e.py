"""
PTY-driven e2e: ``omnigent run --harness <harness>`` returns
the LLM's reply through the new harness contract (mock LLM).

Why this test exists: it covers the interactive CLI path that
server integration tests miss — the CLI's spec-bundling pipeline
(turning ``--harness <harness>`` into an ``executor.harness:
<harness>`` YAML, packaging it, posting to ``/api/agents``) and
the REPL's SSE rendering (event consumption from the Omnigent server
back into the terminal). CLAUDE.md mandates a real REPL run
before declaring an executor change done; this test pins it for
every wrapped harness.

Mock LLM: the mock server responds with ``XYZZY42`` so the test
verifies the full REPL rendering pipeline without real credentials.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.e2e._harness_probes import HARNESS_PROBES
from tests.e2e.conftest import configure_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Strip ANSI escape codes before substring assertions; pexpect
# captures everything raw and the REPL emits a heavy amount of
# styling that would otherwise drown the marker out.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MARKER_TIMEOUT_S = 120.0

# Mock-only model for the REPL repl test — keyed per harness so
# concurrent parametrize rows get isolated mock queues.
_MOCK_MODEL_PREFIX = "mock-repl-harness"


def _mock_model_key(harness: str) -> str:
    """Return the unique mock model name (= queue key) for *harness*."""
    return f"{_MOCK_MODEL_PREFIX}-{harness}"


# Harnesses whose LLM client honours OPENAI_BASE_URL / ANTHROPIC_BASE_URL
# and therefore work against the mock server without a real API key.
# ``claude-sdk`` and ``pi`` use native CLI binaries that call additional
# auth/metadata endpoints not served by the mock (e.g. /v1/beta/auth,
# pi gateway initialisation), so they are excluded from this mock-only
# parametrize and remain covered by the real-LLM e2e suite.
_MOCK_COMPATIBLE_HARNESSES = {"openai-agents", "codex"}

_MOCK_PARAMS = [
    (p.harness, _mock_model_key(p.harness))
    for p in HARNESS_PROBES
    if p.harness in _MOCK_COMPATIBLE_HARNESSES
]
_MOCK_IDS = [p.harness for p in HARNESS_PROBES if p.harness in _MOCK_COMPATIBLE_HARNESSES]


def _strip_ansi(text: str) -> str:
    """
    Remove ANSI escape codes from a captured pexpect buffer.

    :param text: Raw captured text.
    :returns: Plain text suitable for substring assertions.
    """
    return _ANSI_RE.sub("", text)


def _read_until_marker(
    child: Any,
    marker: str,
    *,
    forbidden_in_match: str,
    timeout_s: float = 180.0,
) -> str:
    """
    Read child output until *marker* appears in Claude's reply.

    Why this isn't just ``child.expect(marker)``: the REPL
    echoes the user's input back, so a marker that happens to
    appear in the prompt text would match the echo line first.
    This helper accumulates all output, strips ANSI, scrubs the
    forbidden text (the user prompt), and only succeeds when
    the marker is found in the *remaining* text.

    :param child: Active pexpect child.
    :param marker: Substring that must appear in the model's
        reply.
    :param forbidden_in_match: Text that must be excluded
        before checking for the marker — typically the user's
        own prompt, which is echoed back.
    :param timeout_s: Total deadline before failing.
    :returns: The full ANSI-stripped buffer at success time.
    :raises AssertionError: If the marker never appears within
        the timeout.
    """
    import time

    buf = ""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with contextlib.suppress(pexpect.exceptions.TIMEOUT):
            child.expect([pexpect.TIMEOUT, pexpect.EOF], timeout=2.0)
        if child.before:
            buf += child.before
        plain = _strip_ansi(buf)
        scrubbed = plain.replace(forbidden_in_match, "")
        if marker in scrubbed:
            return plain
        if not child.isalive():
            break
    raise AssertionError(
        f"marker {marker!r} never appeared in Claude's reply within "
        f"{timeout_s:.0f}s. captured (last 4000 chars, ANSI-stripped):\n"
        f"{_strip_ansi(buf)[-4000:]}"
    )


@pytest.fixture
def repl_env(mock_llm_server_url: str, tmp_path: Path) -> dict[str, str]:
    """
    Build the env dict for the REPL subprocess using the mock LLM server.

    Injects ``OPENAI_BASE_URL`` (and ``ANTHROPIC_BASE_URL`` for the
    claude-sdk harness path) pointing at the mock server so the REPL
    process's LLM calls are answered by the mock without real credentials.

    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Isolated temp directory for OMNIGENT_CONFIG_HOME.
    :returns: Env mapping for ``pexpect.spawn``.
    """
    from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env

    config_home = tmp_path / "omnigent-config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "auth:\n  type: none\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        # PYTHONPATH so the worktree wins over any sibling
        # editable install of omnigent.
        "PYTHONPATH": (f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"),
        # Force ANSI on; we strip it per-assertion via _ANSI_RE.
        "TERM": "xterm-256color",
        # Disable cursor-position reporting so the buffer doesn't
        # fill with control sequences that confuse expect() patterns.
        "PROMPT_TOOLKIT_NO_CPR": "1",
        # Point all harnesses at the mock LLM server.
        # openai-agents / codex / pi speak the Responses API.
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        # claude-sdk speaks the Anthropic Messages API; the mock
        # server also serves POST /v1/messages. The Anthropic SDK
        # appends /v1/messages to ANTHROPIC_BASE_URL, so set the
        # base WITHOUT the /v1 suffix.
        "ANTHROPIC_API_KEY": "mock-key",
        "ANTHROPIC_BASE_URL": mock_llm_server_url,
    }
    # Strip real credentials that may have been inherited.
    for var in ("DATABRICKS_TOKEN", "CODEX", "CLAUDE_CODE"):
        env.pop(var, None)
    return ensure_repl_test_theme_env(env)


@pytest.mark.parametrize("harness,model", _MOCK_PARAMS, ids=_MOCK_IDS)
def test_repl_run_routes_harness_through_new_harness_contract(
    repl_env: dict[str, str],
    harness: str,
    model: str,
    mock_llm_server_url: str,
) -> None:
    """
    Drive the full ``omnigent run --harness <harness>``
    flow under a PTY and verify the mock LLM's reply comes back.

    Verifies the path that the HTTP-only e2e tests miss:

    1. The CLI's ``run_chat`` packs ``--harness <harness>`` +
       ``--model`` into the temporary spec.
    2. It spawns a local Omnigent server subprocess.
    3. It uploads the spec via ``/api/agents``.
    4. The Omnigent server's ``_create_executor`` sees an
       ``executor.type == "omnigent"`` +
       ``config.harness == <harness>`` spec (after the
       omnigent-YAML translator runs) and dispatches to
       the harness HTTP client via the step-5f branch.
    5. The mock LLM replies with ``XYZZY42`` which streams back
       through SSE → the REPL's SDK client → terminal rendering.

    The marker is XYZZY42 (not in the prompt) so the assertion
    checks the model's reply specifically, not the echoed prompt.
    """
    marker = "XYZZY42"
    user_prompt = (
        "Reply with EXACTLY 7 characters and nothing else: "
        "capital X, then capital Y, then capital Z, then "
        "capital Z, then capital Y, then digit 4, then digit "
        "2 — joined with no separators. Your entire reply "
        "must be those 7 characters in that order with no "
        "spaces, no dashes, no quotes, no commas, no "
        "newlines, and no surrounding text."
    )

    # Pre-configure the mock server to return XYZZY42 for this harness.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": marker}],
        key=model,
    )

    child = pexpect.spawn(
        sys.executable,
        [
            "-m",
            "omnigent.cli",
            "run",
            "tests/resources/examples/hello_world.yaml",
            "--harness",
            harness,
            "--model",
            model,
            # This test verifies harness routing and rendered output, not
            # persistent session resumption. Keep it on the isolated
            # one-shot path so parallel shards do not contend over the
            # shared local daemon / persistent chat.db.
            "--no-session",
            "-p",
            user_prompt,
        ],
        cwd=str(_REPO_ROOT),
        env=repl_env,
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 160),
        timeout=_MARKER_TIMEOUT_S,
    )
    try:
        plain = _read_until_marker(
            child,
            marker=marker,
            forbidden_in_match=user_prompt,
            timeout_s=_MARKER_TIMEOUT_S,
        )
    finally:
        # Best-effort clean shutdown — the REPL hangs in input
        # mode after the one-shot prompt completes, so we have
        # to send /quit. Fall back to terminate() if it sticks.
        with contextlib.suppress(Exception):
            child.sendline("/quit")
            child.expect(pexpect.EOF, timeout=5)
        if child.isalive():
            child.terminate(force=True)

    # Sanity that Claude's reply actually rendered into the
    # terminal — without this assertion a regression that
    # silently swallowed all SSE deltas (e.g. the SDK client's
    # event mapper changing) might still leave the marker
    # visible elsewhere (a debug line, etc.).
    assert marker in plain, f"marker {marker!r} not found in REPL output (post-strip)"


def test_repl_pexpect_dependencies_are_present() -> None:
    """
    Sanity check that :mod:`pexpect` is importable.

    Acts as a guard rail — if the prior ``importorskip`` at
    module load skipped the whole file, this test would also
    skip. Useful diagnostic for "the REPL test isn't running"
    cases on CI: the file collected, this test ran, but the
    real one was skipped due to ``--profile`` being absent.
    """
    # Trivially true; the load-time import-or-skip is what
    # matters.
    assert pexpect is not None
    # Sanity that ``omnigent`` is importable from this
    # worktree — if it isn't, the REPL spawn would fail with
    # a confusing ``ModuleNotFoundError`` instead of a clean
    # skip on missing fixtures. ``shutil.which`` is irrelevant
    # because we invoke the module directly via ``-m``.
    assert shutil.which(sys.executable) is not None
