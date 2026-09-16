"""Check that real agy loads global hooks from the seeded session directory.

Requires agy and a POSIX terminal. Checks startup logs without signing in
or executing a hook."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pexpect
import pytest

from omnigent.harnesses.antigravity_native.bridge import (
    agy_gemini_dir,
    seed_isolated_agy_home,
    write_mcp_config,
)
from omnigent.harnesses.antigravity_native.launch import agy_binary_path

try:
    _AGY_BIN: str | None = agy_binary_path()
except RuntimeError:
    _AGY_BIN = None

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(
        _AGY_BIN is None,
        reason=(
            "antigravity-native isolated-hooks e2e needs the real `agy` CLI on PATH "
            "(or ~/.local/bin/agy); install it with "
            "`curl -fsSL https://antigravity.google/cli/install.sh | bash`"
        ),
    ),
]

_AGY_STARTUP_TIMEOUT = 25.0

_NAMED_HOOKS_RE = re.compile(r"loaded (\d+) named hooks", re.IGNORECASE)


def _write_user_policy_hook(gemini_dir: Path) -> None:
    (gemini_dir / "config").mkdir(mode=0o700, parents=True, exist_ok=True)
    (gemini_dir / "hooks").mkdir(mode=0o700, parents=True, exist_ok=True)
    script = gemini_dir / "hooks" / "deny-no-verify.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'input="$(cat)"\n'
        "printf '%s' \"$input\" | grep -q -- '--no-verify' "
        '&& echo \'{"decision":"deny","reason":"Policy gate: '
        "git commit --no-verify is not allowed\"}' "
        '|| echo \'{"decision":"allow"}\'\n',
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    (gemini_dir / "config" / "hooks.json").write_text(
        json.dumps(
            {
                "no-verify-gate": {
                    "PreToolUse": [
                        {
                            "matcher": "run_command",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": str(script),
                                    "timeout": 10,
                                }
                            ],
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _agy_named_hook_count(gemini_dir: Path, *, cwd: Path) -> int:
    """Read the hook count from agy startup logs, then stop its TUI."""
    assert _AGY_BIN is not None
    child = pexpect.spawn(
        _AGY_BIN,
        [f"--gemini_dir={gemini_dir}"],
        encoding="utf-8",
        codec_errors="replace",
        timeout=_AGY_STARTUP_TIMEOUT,
        dimensions=(40, 200),
        cwd=str(cwd),
        env={**os.environ, "TERM": "xterm-256color"},
    )
    log_glob = gemini_dir / "antigravity-cli" / "log"
    try:
        deadline = time.monotonic() + _AGY_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            for log_path in sorted(log_glob.glob("*.log")) if log_glob.is_dir() else []:
                m = _NAMED_HOOKS_RE.search(log_path.read_text(errors="replace"))
                if m:
                    return int(m.group(1))
            time.sleep(0.5)
    finally:
        try:
            child.kill(15)
            time.sleep(0.5)
            child.kill(9)
        except Exception:
            pass
    raise AssertionError(
        f"agy wrote no hooks-manager startup line under {log_glob} within {_AGY_STARTUP_TIMEOUT}s"
    )


def test_dispatched_agy_session_keeps_user_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    real_gemini = fake_home / ".gemini"
    real_gemini.mkdir(mode=0o700, parents=True)
    _write_user_policy_hook(real_gemini)
    monkeypatch.setenv("HOME", str(fake_home))

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700)
    write_mcp_config(bridge_dir)
    seed_isolated_agy_home(bridge_dir, trusted_workspace=tmp_path / "ws")
    iso_gemini = agy_gemini_dir(bridge_dir)

    assert (iso_gemini / "config" / "mcp_config.json").is_file(), (
        "expected the seeder to have written the relay config into the isolated dir"
    )

    interactive_ws = tmp_path / "interactive-ws"
    interactive_ws.mkdir()
    dispatched_ws = tmp_path / "dispatched-ws"
    dispatched_ws.mkdir()

    interactive_hooks = _agy_named_hook_count(real_gemini, cwd=interactive_ws)
    dispatched_hooks = _agy_named_hook_count(iso_gemini, cwd=dispatched_ws)

    assert interactive_hooks >= 1, (
        "interactive agy did not load the user's policy hook — fixture invalid "
        f"(loaded {interactive_hooks} named hooks)"
    )

    assert (iso_gemini / "config" / "hooks.json").is_file(), (
        "isolated --gemini_dir is missing config/hooks.json: the dispatched agy "
        "session silently drops the user's hook-based policy gates"
    )

    assert dispatched_hooks == interactive_hooks, (
        "dispatched agy loaded a different number of named hooks than the "
        f"interactive one (dispatched={dispatched_hooks}, "
        f"interactive={interactive_hooks}): the user's policy gates were dropped "
        "by the isolated --gemini_dir seed"
    )
