"""End-to-end regression: the isolated ``--gemini_dir`` must carry the user's hooks.

Regression guard: the antigravity-native isolated ``--gemini_dir`` used to
silently drop the user's ``hooks.json`` policy gates.

Both Antigravity-native launch paths (the CLI launch in
:mod:`omnigent.antigravity_native` and the runner-owned launch in
:mod:`omnigent.runner.native.orchestration`) run the real ``agy`` CLI under a
**per-session isolated** ``--gemini_dir`` and seed selected state into it from
the user's real ``~/.gemini`` via
:func:`omnigent.antigravity_native_bridge.seed_isolated_agy_home` (auth tokens,
``settings.json``, onboarding/migration markers, plugins, skills, workspace
trust) plus the relay ``config/mcp_config.json`` written by
:func:`omnigent.antigravity_native_bridge.write_mcp_config`.

agy 1.1.x loads its **global lifecycle hooks** from
``<gemini_dir>/config/hooks.json`` (verified against the bundled agy binary,
whose ``hooks_manager`` logs ``loaded N named hooks from N hooks.json file(s)``
at startup). The seed routine never copies that file, so an Omnigent-dispatched
agy session starts with **zero** of the user's hooks even though the identical
hooks run in an interactive ``agy``. That is security-relevant: hooks are how
users install local policy gates (deny ``git commit --no-verify``, force-pushes,
package installs), and they disappear silently from every dispatched session.

**What this test drives (the real user journey, faithfully):**

1. A user has a global agy policy-gate hook at ``~/.gemini/config/hooks.json``
   (here: a ``PreToolUse``/``run_command`` handler that denies
   ``git commit --no-verify``).
2. Omnigent dispatches an agy session, which runs the **production seeder**
   :func:`seed_isolated_agy_home` + :func:`write_mcp_config` against that real
   home to build the per-session isolated ``--gemini_dir`` — the exact functions
   both launch paths call (``antigravity_native.py`` / ``orchestration.py``).
3. The **real ``agy`` binary** is launched with ``--gemini_dir=<isolated dir>``
   (exactly the flag the launch prepends) and, separately, against the user's
   real Gemini dir (the interactive baseline). Each agy's own startup log is
   read for its named-hook count.

The bug is the divergence: the interactive dir loads the user's hook while the
dispatched/isolated dir loads **zero**, and the isolated
``config/hooks.json`` is absent. The fix seeds the user's ``config/hooks.json``
into the isolated dir, so both load the same count. The assertions below fail on
the current build and pass once the fix lands.

The transport that the full ``omnigent antigravity`` dispatch adds on top (host
daemon, runner, tmux pane, connect-RPC mirror) is deliberately **not** exercised
here: the defect is entirely in the isolated-home *seeding*, hook loading is
independent of it (and of agy sign-in), and driving a full behavioral turn where
the gate visibly fires would require Google OAuth that CI cannot complete. This
test therefore drives the genuine seeder and the genuine agy binary, which is
where the bug lives.

Prerequisites (skipped cleanly when absent):

* The ``agy`` CLI on ``PATH`` / at ``~/.local/bin/agy`` (the harness binary whose
  real hook-loading behavior this test observes). No sign-in is required: the
  hooks manager logs its count at startup regardless of auth, and each agy launch
  is bounded and killed.
* POSIX only (uses a pseudo-terminal to start agy's TUI).
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import pexpect
import pytest

from omnigent.antigravity_native_bridge import (
    agy_gemini_dir,
    seed_isolated_agy_home,
    write_mcp_config,
)
from omnigent.antigravity_native_launch import agy_binary_path

# Skip cleanly where the real agy binary is unavailable: without it there is no
# harness whose hook-loading behavior to observe.
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

# agy cold-starts a TUI and writes its startup log (including the hooks-manager
# count) within a couple of seconds; give it headroom on a contended host, then
# kill it (we only need the startup log, never a turn).
_AGY_STARTUP_TIMEOUT = 25.0

# The hooks_manager startup line, e.g.
# "hooks_manager.go:53] loaded 1 named hooks from 1 hooks.json file(s)".
_NAMED_HOOKS_RE = re.compile(r"loaded (\d+) named hooks", re.IGNORECASE)


def _write_user_policy_hook(gemini_dir: Path) -> None:
    """Install a realistic global agy policy-gate hook under *gemini_dir*.

    Writes ``config/hooks.json`` plus the referenced ``PreToolUse`` script,
    mirroring how a user installs a local safety gate (deny
    ``git commit --no-verify``).

    :param gemini_dir: A ``.gemini`` directory to populate.
    """
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
    """Launch the real agy against *gemini_dir* and return its named-hook count.

    Spawns ``agy --gemini_dir=<gemini_dir>`` under a pseudo-terminal (agy is a
    TUI and needs a TTY), waits for it to write its startup log, then kills it
    and parses the ``hooks_manager`` "loaded N named hooks" line.

    :param gemini_dir: The ``.gemini`` dir to pass via ``--gemini_dir``.
    :param cwd: Working directory for the agy process.
    :returns: The number of named hooks agy reported loading at startup.
    :raises AssertionError: If agy never wrote a hooks-manager startup line.
    """
    assert _AGY_BIN is not None  # narrowed by the module skip
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
    """A dispatched agy session must load the same user hooks an interactive one does.

    Builds a fake ``$HOME`` carrying a global agy policy-gate hook, runs the
    production isolated-home seeder (the code both launch paths invoke) to build
    the per-session ``--gemini_dir``, then launches the real agy against both the
    interactive (real) Gemini dir and the dispatched (isolated) one and compares
    their named-hook counts.

    Fails on the buggy build (isolated dir has no ``config/hooks.json`` → agy
    loads 0 while interactive loads 1); passes once the seeder copies the user's
    ``config/hooks.json`` into the isolated dir.

    :param tmp_path: Per-test temp dir (fake home + bridge dir + agy cwd).
    :param monkeypatch: Points ``$HOME`` at the fake home so the seeder's
        ``Path.home()`` reads the user policy hook we installed.
    """
    # 1. A user with a global agy policy-gate hook in their real ~/.gemini.
    fake_home = tmp_path / "home"
    real_gemini = fake_home / ".gemini"
    real_gemini.mkdir(mode=0o700, parents=True)
    _write_user_policy_hook(real_gemini)
    monkeypatch.setenv("HOME", str(fake_home))

    # 2. Omnigent dispatches a session: the production seeder + relay-config
    #    writer build the per-session isolated --gemini_dir from that real home.
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700)
    write_mcp_config(bridge_dir)
    seed_isolated_agy_home(bridge_dir, trusted_workspace=tmp_path / "ws")
    iso_gemini = agy_gemini_dir(bridge_dir)

    # Sanity: the seed genuinely ran (it seeds the relay config + migration
    # marker), so a missing isolated hooks.json is a real omission, not an
    # un-run seeder.
    assert (iso_gemini / "config" / "mcp_config.json").is_file(), (
        "expected the seeder to have written the relay config into the isolated dir"
    )

    # 3. Compare what the real agy loads from each dir.
    interactive_ws = tmp_path / "interactive-ws"
    interactive_ws.mkdir()
    dispatched_ws = tmp_path / "dispatched-ws"
    dispatched_ws.mkdir()

    interactive_hooks = _agy_named_hook_count(real_gemini, cwd=interactive_ws)
    dispatched_hooks = _agy_named_hook_count(iso_gemini, cwd=dispatched_ws)

    # The fixture hook must actually register in an interactive agy, or the
    # comparison below would be vacuous.
    assert interactive_hooks >= 1, (
        "interactive agy did not load the user's policy hook — fixture invalid "
        f"(loaded {interactive_hooks} named hooks)"
    )

    # The regression: the user's global config/hooks.json must be present in the
    # dispatched session's isolated Gemini dir (the direct fail->pass target of
    # the fix) ...
    assert (iso_gemini / "config" / "hooks.json").is_file(), (
        "isolated --gemini_dir is missing config/hooks.json: the dispatched agy "
        "session silently drops the user's hook-based policy gates"
    )

    # ... and the dispatched agy must load the SAME user hooks the interactive
    # one does, so a policy gate that denies e.g. `git commit --no-verify` is not
    # silently absent in an Omnigent-dispatched session.
    assert dispatched_hooks == interactive_hooks, (
        "dispatched agy loaded a different number of named hooks than the "
        f"interactive one (dispatched={dispatched_hooks}, "
        f"interactive={interactive_hooks}): the user's policy gates were dropped "
        "by the isolated --gemini_dir seed"
    )
