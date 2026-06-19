"""Small, opt-in e2e smoke for the polly coding orchestrator (examples/polly).

Real model: boots the claude-sdk orchestrator bundle against a
LOCAL server and confirms it completes a turn. This exercises the parts a
structural spec-load test can't - the claude-sdk harness authenticating
against the `oss` workspace (via the global-config auth block), the sub-agents
(implementation + review) registering, the server-side polly
function-policies resolving, and a turn streaming back through the run path.

Why a local server (not bare ``omnigent run``): polly's guardrail policies
(``omnigent.inner.nessie.policies`` — the package keeps its historical
name) are resolved SERVER-SIDE when the workflow executes. Bare ``omnigent
run`` routes to the developer's configured default server (the shared
``omnigent`` prod app), which may not carry the in-tree policy module, so
the turn 500s at event-execution. We therefore stand up a throwaway local
``omnigent server`` from this working tree - which DOES carry the polly
code - and point ``run --server`` at it.

OPT-IN. polly needs the dev-box toolset that CI runners don't have: a
logged-in `oss` Databricks OAuth profile that the claude-sdk orchestrator and
the claude-native / codex-native sub-agents route through.
So it is gated behind ``OMNIGENT_E2E_POLLY=1`` and is not collected in the
default suite. Run it manually after touching the polly bundle, its skills, or
the claude-sdk / openai-agents auth paths:

    OMNIGENT_E2E_POLLY=1 uv run --extra dev python -m pytest \
        tests/e2e/test_polly_e2e.py -v

The full multi-agent loop (decompose -> fanout to implementation sub-agents ->
cross-review -> integrate) is a heavier follow-up; this smoke just guards the
substrate so a blank-turn regression (auth, harness, bundle load, policy
resolution) is caught.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

# tests/e2e/test_polly_e2e.py -> repo root is 2 parents up.
_REPO = Path(__file__).resolve().parents[2]
_POLLY = _REPO / "examples" / "polly"
_PROFILE = "oss"
_RUN_TIMEOUT_SEC = 300
_SERVER_BOOT_TIMEOUT_SEC = 90
# Long enough to prove a real model reply, short enough to flag an empty turn.
_MIN_REPLY_CHARS = 12

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_POLLY") != "1",
    reason=(
        "polly e2e needs the dev-box toolset (oss OAuth login) absent on CI - "
        "set OMNIGENT_E2E_POLLY=1 to opt in."
    ),
)


def _clean_env() -> dict[str, str]:
    """
    Build a subprocess env with token vars stripped so the ``oss`` profile's
    OAuth (resolved by the harnesses via the global-config auth block)
    isn't shadowed.

    :returns: A copy of ``os.environ`` with onboarding/update-check disabled and
        credential env vars that would override profile auth removed.
    """
    env = dict(os.environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    # The ``--profile`` CLI flag was removed from the omnigent CLI; the
    # supported replacement is an ``auth:`` block in the global config.
    # Write it into an isolated ``OMNIGENT_CONFIG_HOME`` so the spawned
    # CLI/harnesses route Databricks auth through the ``oss`` profile.
    config_home = Path(tempfile.mkdtemp(prefix="omnigent-polly-config-"))
    (config_home / "config.yaml").write_text(
        f"auth:\n  type: databricks\n  profile: {_PROFILE}\n",
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["DATABRICKS_CONFIG_PROFILE"] = _PROFILE
    for stale in (
        "DATABRICKS_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CODEX",
    ):
        env.pop(stale, None)
    return env


def _free_port() -> int:
    """
    Reserve an ephemeral localhost port for the local server.

    :returns: A port number the OS just confirmed is free. There is a small
        window between close and the server re-binding it; acceptable for a
        single-process opt-in smoke.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_health(base_url: str, deadline: float) -> None:
    """
    Block until the local server answers HTTP, or fail past ``deadline``.

    :param base_url: e.g. ``"http://127.0.0.1:8811"``.
    :param deadline: ``time.monotonic()`` value past which to give up.
    """
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/", timeout=5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as err:  # not up yet
            last_err = err
        time.sleep(1)
    raise TimeoutError(f"local server at {base_url} never became healthy: {last_err}")


@pytest.fixture
def local_polly_server(tmp_path: Path) -> Iterator[str]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    The server carries the in-tree ``omnigent.inner.nessie.policies`` module
    that polly's guardrails resolve server-side, so the workflow doesn't 500
    the way it does against the shared prod app. Own sqlite DB + artifact dir
    under ``tmp_path`` keep it isolated from the developer's real state.

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: The base URL of the running server, e.g. ``"http://127.0.0.1:8811"``.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_uri = f"sqlite:///{tmp_path / 'polly_e2e.db'}"
    artifacts = tmp_path / "artifacts"
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
            db_uri,
            "--artifact-location",
            str(artifacts),
        ],
        cwd=str(_REPO),
        env=_clean_env(),
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


def test_polly_orchestrator_boots_and_responds(
    local_polly_server: str, using_mock_llm: bool
) -> None:
    """
    ``omnigent run examples/polly --server <local> -p <prompt>``
    (with the ``oss`` profile supplied via the global-config auth block)
    exits 0 and emits a non-trivial reply.

    Proves the bundle loads end-to-end against a server that carries polly's
    code: the claude-sdk orchestrator authenticates (the profile
    auth fix), the sub-agents register without aborting startup,
    the server-side guardrail policies resolve, and a turn completes. A blank
    reply here is the exact failure that masqueraded as "no output" before the
    auth fix — so this is the regression guard for the substrate.

    :param local_polly_server: Base URL of the in-tree local server fixture.
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "polly orchestrator e2e requires real model inference via claude-sdk "
            "and real subprocess omnigent run invocations; not feasible under mock LLM"
        )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(_POLLY),
            "--server",
            local_polly_server,
            "-p",
            "In one short sentence, what are you and how do you handle a coding task?",
        ],
        cwd=str(_REPO),
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    # Exit 0 proves boot + turn completion; a harness that aborts startup,
    # an auth 401, or a server-side policy that fails to resolve would
    # surface here as a non-zero exit.
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    reply = result.stdout.strip()
    # A real model reply, not an empty turn. The pre-auth-fix bug produced an
    # empty stdout with exit 0; this length check is what would have caught it.
    assert len(reply) >= _MIN_REPLY_CHARS, (
        f"polly produced no/short reply ({len(reply)} chars): {reply!r}\n"
        f"--- stderr ---\n{result.stderr}"
    )
