"""E2E regression: ``omnigent config list`` must not present an API-key codex
login as a bare subscription.

The reported journey: codex is authenticated in ``apikey`` mode (its
``~/.codex/auth.json`` carries only ``OPENAI_API_KEY`` — the state ``codex``
itself reports as ``auth_mode: apikey``), the key has no remaining credit and
every codex turn dies with a quota error; the user runs ``omnigent config
list`` to diagnose and it prints ``Codex … subscription … ✓ default``, which
sends the diagnosis in the wrong direction (a subscription looks healthy when
the real problem is the exhausted API key).

This test drives the real user command: a fake ``$HOME`` seeded with an
apikey-mode ``auth.json`` (exactly what the codex CLI writes for that mode),
then ``python -m omnigent config list`` as a subprocess, asserting on the
console text the user reads.

Before the fix: the Codex credential row reads ``subscription codex via
codex CLI`` — the drift — and this test FAILS.
After a fix (reflect the *effective* codex auth mode): the row must stop
labeling an API-key-backed login a bare "subscription" (e.g. by naming the
API-key auth mode), and it PASSES.

Runs with no LLM, no codex binary, and no network::

    pytest tests/e2e/test_config_list_codex_effective_auth_mode_e2e.py -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Env keys that would add ambient credential detections (or omnigent config)
# from the CI machine into the listing under test.
_AMBIENT_ENV_PREFIXES = ("OMNIGENT_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "GOOGLE_")
_AMBIENT_ENV_KEYS = ("LLM_API_KEY", "CLAUDE_CODE_USE_VERTEX", "CLOUD_ML_REGION")

# The checkout under test. Pinned onto the subprocess PYTHONPATH so the child
# imports THIS tree's omnigent, not a site-packages install of another tree.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _clean_env(fake_home: Path) -> dict[str, str]:
    """A subprocess env rooted at *fake_home* with ambient credentials removed."""
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


def test_config_list_does_not_call_an_apikey_codex_login_a_subscription(
    tmp_path: Path,
) -> None:
    """An apikey-mode codex ``auth.json`` must not render as a bare subscription.

    ``codex``'s own ``auth.json`` shape (``codex-rs/login``): apikey mode
    stores ``OPENAI_API_KEY`` and no ``tokens``; ChatGPT-subscription mode
    stores ``tokens.{access,refresh}_token``. ``config list`` must reflect
    that effective auth mode instead of folding both into "subscription".
    """
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    (fake_home / ".codex" / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-proj-dead-key-no-credit-0000"}),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "omnigent", "config", "list"],
        capture_output=True,
        text=True,
        timeout=120.0,
        env=_clean_env(fake_home),
        cwd=str(tmp_path),  # no project-level .omnigent config in scope
    )
    assert proc.returncode == 0, proc.stderr

    # The listing groups credentials by harness; grab the Codex section
    # (up to the next harness header or end of output).
    match = re.search(r"^  Codex\n((?:    .*\n?)*)", proc.stdout, flags=re.MULTILINE)
    assert match is not None, f"no Codex section in output:\n{proc.stdout}"
    codex_section = match.group(1)

    # The login must be detected (the file carries a usable credential) …
    assert "codex" in codex_section.lower(), (
        f"apikey-mode codex login not listed at all:\n{proc.stdout}"
    )
    # … but an API-key-backed login must not be presented as a subscription
    # with no hint of its effective auth mode: `codex` itself reports
    # `auth_mode: apikey` while the listing said only `subscription`.
    presented_as_subscription = "subscription" in codex_section.lower()
    names_api_key_mode = re.search(r"api.?key", codex_section, flags=re.IGNORECASE)
    assert not (presented_as_subscription and not names_api_key_mode), (
        "`omnigent config list` presents an apikey-mode codex login as a "
        "subscription without reflecting the effective auth mode. "
        f"Codex section:\n{codex_section}"
    )
