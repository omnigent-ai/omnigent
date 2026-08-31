"""
REPL pexpect boot-starvation regression guard.

The REPL pexpect e2e cluster (``test_repl_approval_e2e.py``,
``test_repl_sessions_approval_e2e.py``, ...) passes in isolation but
starves on boot under full e2e shard load and times out at
``_wait_for_prompt_ready`` — boot starvation is counted as a test
failure, so those tests cannot be reliably un-suppressed.

This test reproduces that starvation deterministically on one box by
emulating the full-shard condition instead of depending on whatever
else CI happens to run concurrently:

- CPU burners oversubscribe every core (the co-located concurrent
  shards / heavy loadscope-worker neighbors), and
- several ``omnigent run`` REPLs boot at the same time (each REPL
  pexpect module boots its own daemon + local server + runner).

It then holds each boot to the same ``60s`` prompt-ready budget the
suppressed tests used, and fails with the per-REPL outcome when any
boot starves past it. Under this load the CLI's *own* local-boot
budget (``_wait_for_server`` in ``omnigent/chat.py``, for server
health + runner online) was typically exhausted first, so the REPL
exits with ``Server failed to start`` (EOF) even though the runner
was healthily retrying its tunnel — the sharpest form of "boot
starvation counted as a test failure".

On an unloaded box the same boot reaches prompt-ready in ~12s, so a
harness fix (lighter boot, a readiness signal decoupled from full
boot, or a serial lane for REPL pexpect modules) turns this test
green without touching the assertion.

Usage::

    python -m pytest tests/e2e/test_repl_boot_starvation_under_shard_load.py \
        -v -o addopts="" --timeout=300
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import reset_mock_llm

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASK_DEMO_YAML = _REPO_ROOT / "tests" / "resources" / "agents" / "ask-demo" / "ask-demo.yaml"
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# The launch budget the suppressed REPL pexpect tests held boot to
# (``_wait_for_prompt_ready(child, timeout=60)``) — the exact ceiling
# the shard-load starvation blows through. Deliberately NOT inflated:
# the point of this test is that boot must fit the budget under load,
# not that the budget be raised until starvation stops counting.
_PROMPT_READY_BUDGET_S = 60.0

# Full-shard emulation shape. Each REPL pexpect module boots its own
# daemon + local server + runner, and the box concurrently runs the
# other e2e shards — approximated by several simultaneous boots plus
# CPU oversubscription on every core.
_CONCURRENT_BOOTS = 4
_BURNERS_PER_CORE = 3

# Burner child: peg one core, but self-exit if this test process is
# SIGKILL'd (reparenting changes getppid), so burners never outlive a
# hard-killed run.
_BURNER_SRC = "import os\np = os.getppid()\nwhile os.getppid() == p:\n    pass\n"


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences before substring search."""
    return _ANSI_RE.sub("", text)


def _build_repl_env(mock_llm_server_url: str, tmp_home: Path) -> dict[str, str]:
    """Build the pexpect environment dict for one REPL boot.

    Mirrors the suppressed modules' env: mock LLM routing, a fake
    ``HOME`` seeded with a persisted theme (so the first-launch theme
    picker never blocks the PTY), and this worktree's SDK paths on
    ``PYTHONPATH`` so the spawned runner subprocess resolves
    ``omnigent`` from source rather than a sibling install.
    """
    sdk_paths = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    existing_pp = os.environ.get("PYTHONPATH", "")
    merged_pp = (
        os.pathsep.join([*sdk_paths, existing_pp]) if existing_pp else os.pathsep.join(sdk_paths)
    )

    config_home = tmp_home / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        "auto_open_conversation: false\ntui:\n  theme: dark\n",
    )

    real_databrickscfg = Path.home() / ".databrickscfg"
    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "HOME": str(tmp_home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "DATABRICKS_CONFIG_FILE": str(real_databrickscfg),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "PYTHONPATH": merged_pp,
        "TERM": "xterm-256color",
        "LINES": "40",
        "COLUMNS": "120",
        "PROMPT_TOOLKIT_NO_CPR": "1",
        # Loopback traffic (mock LLM, local server) must never route
        # through an ambient corporate proxy.
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE", "CLAUDECODE", "CODEX", "DATABRICKS_TOKEN"):
        env.pop(k, None)
    # A REPL spawned from inside a hosted Omnigent runner inherits that
    # runner's identity/tunnel env, which silently rewires the fresh boot
    # this test must perform. Strip every runner-scoped ambient so the
    # boot is hermetic wherever the test runs.
    runner_ambients = {
        "OMNIGENT",
        "OMNIGENT_USER_ID",
        "OMNIGENT_PROCESS_LOG_FILE",
        "RUNNER_SERVER_URL",
    }
    for k in [k for k in env if k.startswith("OMNIGENT_RUNNER") or k in runner_ambients]:
        env.pop(k, None)
    return env


def _spawn_repl(env: dict[str, str]) -> Any:
    """Spawn one ``omnigent run`` REPL under a PTY."""
    return pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "run", str(_ASK_DEMO_YAML), "--no-session"],
        env=env,
        cwd=str(_REPO_ROOT),
        encoding="utf-8",
        codec_errors="replace",
        timeout=_PROMPT_READY_BUDGET_S,
        dimensions=(40, 120),
    )


def test_repl_boot_reaches_prompt_ready_under_full_shard_load(
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Every concurrent REPL boot must reach prompt-ready within the
    60s budget while the box is under full-shard CPU load.

    This is the regression guard for the boot starvation: the
    suppressed REPL pexpect tests fail exactly here — the REPL never
    reaches the input-ready prompt (``❯``) within 60s under shard
    load (or the CLI aborts its own boot first with ``Server failed
    to start``), while the identical boot takes ~12s unloaded.
    """
    if os.environ.get("PYTEST_XDIST_WORKER_COUNT", "1") != "1":
        pytest.skip(
            "pegs every core with CPU burners; run standalone (see module "
            "docstring), not inside a parallel xdist shard where it would "
            "starve co-located workers"
        )
    reset_mock_llm(mock_llm_server_url)

    ncpu = os.cpu_count() or 2
    # Spawn inside the try: a mid-spawn failure (fork/EAGAIN is plausible
    # on the loaded box this test creates) must still reach the cleanup
    # for the burners already started.
    burners: list[subprocess.Popen[bytes]] = []
    children: list[Any] = []
    results: list[tuple[int, float, str, str]] = []
    t0 = time.monotonic()
    try:
        for _ in range(ncpu * _BURNERS_PER_CORE):
            burners.append(subprocess.Popen([sys.executable, "-c", _BURNER_SRC]))
        for i in range(_CONCURRENT_BOOTS):
            env = _build_repl_env(
                mock_llm_server_url,
                tmp_path_factory.mktemp(f"repl_boot_home_{i}"),
            )
            children.append(_spawn_repl(env))

        def _wait_prompt_ready(idx: int, child: Any) -> None:
            try:
                # The input-ready prompt marker the suppressed modules
                # wait for (`_wait_for_prompt_ready`).
                child.expect("❯", timeout=_PROMPT_READY_BUDGET_S)
                results.append((idx, time.monotonic() - t0, "ready", ""))
            except pexpect.TIMEOUT:
                tail = _strip_ansi(child.before or "")[-600:]
                results.append((idx, time.monotonic() - t0, "starved (60s TIMEOUT)", tail))
            except pexpect.EOF:
                tail = _strip_ansi(child.before or "")[-600:]
                results.append((idx, time.monotonic() - t0, "exited during boot (EOF)", tail))

        threads = [
            threading.Thread(target=_wait_prompt_ready, args=(i, c))
            for i, c in enumerate(children)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        for child in children:
            with contextlib.suppress(Exception):
                child.terminate(force=True)
        for b in burners:
            b.kill()
        for b in burners:
            with contextlib.suppress(Exception):
                b.wait(timeout=10)

    failures = [r for r in sorted(results) if r[2] != "ready"]
    summary = "\n".join(
        f"  repl[{idx}]: {status} at {dt:.1f}s"
        + (f"\n    tail: {tail.strip()[-300:]}" if tail else "")
        for idx, dt, status, tail in sorted(results)
    )
    assert not failures, (
        f"{len(failures)}/{_CONCURRENT_BOOTS} REPL boots starved past the "
        f"{_PROMPT_READY_BUDGET_S:.0f}s prompt-ready budget under full-shard load "
        f"(boot starvation counted as test failure).\n{summary}"
    )
