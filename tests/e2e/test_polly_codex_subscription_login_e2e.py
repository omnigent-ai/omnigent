"""``omni polly`` must not fail "Not logged in" for Codex-only users.

Reproduces the reported user journey end-to-end through the real CLI:

1. The user's only configured provider is a Codex **subscription**
   (``kind: subscription, cli: codex, default: true`` in
   ``~/.omnigent/config.yaml``) and they are logged in to the Codex CLI
   (``~/.codex/auth.json`` carries OAuth tokens).
2. They run ``omni polly`` and ask anything.
3. The turn fails with ``Not logged in · Please run /login`` (surfaced as
   ``[llm] inner executor error: Not logged in · Please run /login``), even
   though a bare ``omnigent`` run detects the Codex subscription and works.

Mechanism observed live (runner log): polly's brain is the ``claude-sdk``
harness. With no Anthropic-family provider configured,
``_ensure_bundled_agent_brain_credential`` is a no-op (it only adopts
credentials for the brain's own family), smart routing is not configured on
the local server, and the launch proceeds anyway — the Claude CLI starts with
``apiKeySource: 'none'`` and every turn dies with its "Not logged in · Please
run /login" result. The Codex subscription the user *does* have is never
used for (or even considered by) the brain, and no actionable guidance is
shown.

The test spawns the real ``omnigent polly -p ...`` CLI under a pseudo-TTY
(pexpect) with a fake ``$HOME`` seeded exactly like the reporter's machine
(Codex subscription provider + a logged-in ``~/.codex/auth.json``, no
Claude-family credential anywhere), waits for the session to launch, and
fails if the "Not logged in" error surfaces. A fix — routing polly's brain to
the configured Codex provider, or refusing the launch with clear guidance
instead of a broken session — makes this pass.

Usage::

    python -m pytest tests/e2e/test_polly_codex_subscription_login_e2e.py -v --timeout=600
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Launch budget: `omni polly` cold-starts a local server daemon, uploads the
# bundled agent, brings a runner online, and attaches the REPL. Matches the
# _LAUNCH_TIMEOUT rationale in test_repl_approval_e2e.py.
_LAUNCH_TIMEOUT_S = 240
# How long after a successful launch we watch for the login failure. On the
# buggy build the error lands within seconds of the session URL printing.
_FAILURE_WINDOW_S = 90

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# Env vars that would hand polly's claude-sdk brain an ambient credential and
# mask the bug (the reporter has none of these — only the Codex subscription).
# Ambient detection also honors OMNIGENT_-prefixed aliases of the credential
# vars (e.g. OMNIGENT_ANTHROPIC_API_KEY), so strip those the same way.
_CREDENTIAL_ENV_BASE_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_",
    "OPENAI_",
    "DATABRICKS_",
    "AWS_",
    "GOOGLE_",
    "GEMINI_",
)
_CREDENTIAL_ENV_PREFIXES = (
    *_CREDENTIAL_ENV_BASE_PREFIXES,
    *(f"OMNIGENT_{prefix}" for prefix in _CREDENTIAL_ENV_BASE_PREFIXES),
)
_CREDENTIAL_ENV_EXACT = frozenset({"LLM_API_KEY", "CODEX_HOME"})


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes so substring assertions are stable.

    :param text: Raw pexpect buffer contents.
    :returns: Plain text.
    """
    return _ANSI_RE.sub("", text)


def _seed_reporter_home(fake_home: Path) -> None:
    """Seed a fake ``$HOME`` matching the reporter's machine.

    - ``~/.omnigent/config.yaml``: the Codex subscription is the ONLY
      configured provider (plus REPL quality-of-life settings so the
      spawned TUI starts cleanly under pexpect).
    - ``~/.codex/auth.json``: a logged-in Codex CLI (``auth_mode:
      "chatgpt"`` OAuth tokens — what ``codex login`` writes).

    :param fake_home: The directory to use as ``$HOME``.
    """
    config_home = fake_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\n"
        "tui:\n"
        "  theme: dark\n"
        "providers:\n"
        "  codex-sub:\n"
        "    kind: subscription\n"
        "    cli: codex\n"
        "    default: true\n"
    )
    codex_home = fake_home / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "e2e-fake-id-token",
                    "access_token": "e2e-fake-access-token",
                    "refresh_token": "e2e-fake-refresh-token",
                    "account_id": "e2e-acct",
                },
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        )
    )


def _polly_env(fake_home: Path) -> dict[str, str]:
    """Build the subprocess env: reporter's home, no ambient credentials.

    :param fake_home: The seeded fake ``$HOME``.
    :returns: Env mapping for ``pexpect.spawn``.
    """
    env: dict[str, str] = {
        key: value
        for key, value in os.environ.items()
        if key not in _CREDENTIAL_ENV_EXACT
        and not any(key.startswith(prefix) for prefix in _CREDENTIAL_ENV_PREFIXES)
    }
    env.update(
        {
            "HOME": str(fake_home),
            "OMNIGENT_CONFIG_HOME": str(fake_home / ".omnigent"),
            "OMNIGENT_SKIP_ONBOARD": "1",
            "TERM": "xterm-256color",
            "PROMPT_TOOLKIT_NO_CPR": "1",
            # Resolve `omnigent` from this worktree, not a sibling install.
            "PYTHONPATH": os.pathsep.join(
                [str(_REPO_ROOT), *filter(None, [os.environ.get("PYTHONPATH")])]
            ),
        }
    )
    return env


def _kill_local_server(fake_home: Path) -> None:
    """Best-effort teardown of the local server daemon the CLI spawned.

    :param fake_home: The fake ``$HOME`` whose ``.omnigent`` holds the pid.
    """
    pid_file = fake_home / ".omnigent" / "local_server.pid"
    with contextlib.suppress(OSError, ValueError):
        pid = int(pid_file.read_text().splitlines()[0].strip())
        os.kill(pid, signal.SIGTERM)


@pytest.mark.timeout(600)
def test_polly_with_codex_subscription_does_not_fail_not_logged_in(
    tmp_path: Path,
) -> None:
    """``omni polly`` must not die "Not logged in" for a Codex-only user.

    Journey (verbatim from the report): configure only a Codex subscription
    (logged in to the Codex CLI) → run ``omni polly`` → ask anything. The
    reported failure is the turn erroring with ``Not logged in · Please run
    /login``. Bare ``omnigent`` works on the same config, so polly must
    either use the configured Codex provider or fail the *launch* with
    actionable guidance — not start a session whose every turn dies with a
    login error for a CLI the user was never asked to log in to.
    """
    fake_home = tmp_path / "home"
    _seed_reporter_home(fake_home)
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent.cli", "polly", "-p", "hi"],
        cwd=str(workdir),
        env=_polly_env(fake_home),
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(50, 160),
        timeout=_LAUNCH_TIMEOUT_S,
    )
    transcript: list[str] = []
    try:
        # Phase 1 — launch. The REPL prints the session URL once the local
        # server + runner are up and the session is attached. The bug's error
        # can only surface after this point (it is a turn failure).
        launch_patterns = [
            r"Omnigent session:",
            "Not logged in",
            pexpect.EOF,
            pexpect.TIMEOUT,
        ]
        index = child.expect(launch_patterns, timeout=_LAUNCH_TIMEOUT_S)
        transcript.append(_strip_ansi(child.before or ""))
        if index == 1:
            pytest.fail(
                "`omni polly` surfaced 'Not logged in · Please run /login' "
                "with a configured, logged-in Codex subscription.\n"
                f"Transcript:\n{''.join(transcript)[-4000:]}"
            )
        if index in (2, 3):
            pytest.fail(
                "`omni polly` did not reach a live session (no 'Omnigent "
                f"session:' URL within {_LAUNCH_TIMEOUT_S}s).\n"
                f"Transcript:\n{''.join(transcript)[-4000:]}"
            )

        # Phase 2 — the turn. On the buggy build the claude-sdk brain (which
        # has no credential: the user only has a Codex subscription) fails the
        # turn with Claude Code's login error within seconds. Watch the
        # failure window; anything BUT that error is acceptable here.
        failure_patterns = ["Not logged in", pexpect.EOF, pexpect.TIMEOUT]
        deadline = time.monotonic() + _FAILURE_WINDOW_S
        while True:
            remaining = max(1.0, deadline - time.monotonic())
            index = child.expect(failure_patterns, timeout=remaining)
            transcript.append(_strip_ansi(child.before or ""))
            if index == 0:
                pytest.fail(
                    "`omni polly` turn failed with 'Not logged in · Please "
                    "run /login' despite the Codex subscription being "
                    "configured and logged in (bare `omnigent` works on "
                    "this same config).\n"
                    f"Transcript:\n{''.join(transcript)[-4000:]}"
                )
            # EOF (clean-ish exit) or the window elapsing without the login
            # error: the reported failure did not occur.
            break
    finally:
        with contextlib.suppress(Exception):
            child.close(force=True)
        _kill_local_server(fake_home)
