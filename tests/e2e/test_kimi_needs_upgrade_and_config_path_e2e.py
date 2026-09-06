"""Kimi Code onboarding regressions: version floor and config-path guidance.

Three user-observable failure modes are guarded here:

1. ``omni setup`` reported ``Kimi Code ✗ Needs upgrade`` for every shipping
   Kimi Code CLI because ``_KIMI_MIN_VERSION`` was derived from the separate
   ``kimi-cli`` project's 1.x changelog while Kimi Code ships a 0.x series
   (0.32.0 at report time). No release could satisfy ``>=1.47.0``.
2. The same predicate (``harness_cli_installed`` → ``harness_is_configured``)
   made the host refuse every kimi launch, so the harness was unusable.
3. The Kimi auth guidance Omnigent prints names the wrong config path:
   ``~/.kimi/config.toml`` is the legacy ``kimi-cli`` location; Kimi Code
   keeps its config in ``$KIMI_CODE_HOME/config.toml`` (default
   ``~/.kimi-code/config.toml`` — ``kimi doctor`` is authoritative). The
   user-visible ``executor.auth`` rejection in
   ``omnigent/runtime/workflow.py`` still points users at ``~/.kimi/``.

Failure modes 1–2 were fixed by "fix(onboarding): correct the kimi and hermes
CLI version floors" (the floor is now 0.7.0); the tests here guard that fix so
a future edit can't silently re-import kimi-cli's 1.x series. Failure mode 3 is
guarded by ``test_kimi_auth_rejection_names_the_real_kimi_code_config_path``,
which fails while any user-visible guidance names the legacy path.

All three tests drive real user surfaces: the ``omni setup`` TUI under a
pseudo-TTY and the ``omnigent run`` launcher as a subprocess, with a stub
``kimi`` binary reporting the exact version from the report (0.32.0) placed
first on ``PATH``.

Usage::

    python -m pytest tests/e2e/test_kimi_needs_upgrade_and_config_path_e2e.py -v --timeout=180
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from packaging.version import Version

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The Kimi Code CLI version from the bug report — a current, shipping 0.x
# build that the (buggy) 1.47.0 floor rejected.
_REPORTED_KIMI_VERSION = "0.32.0"

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so TUI
# rows can be matched as plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _write_stub_kimi(bin_dir: Path, version: str = _REPORTED_KIMI_VERSION) -> Path:
    """Create a stub ``kimi`` binary whose ``--version`` prints *version*.

    The version-floor check (``_harness_cli_version_satisfies``) only runs
    ``kimi --version``, so a stub is a faithful stand-in for a real install
    at that version — and it pins the exact 0.x number from the report
    regardless of which Kimi Code build happens to be on the machine.
    """
    stub = bin_dir / "kimi"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then\n'
        f'  echo "kimi {version}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "stub kimi: only --version is supported" >&2\n'
        "exit 1\n"
    )
    stub.chmod(0o755)
    return stub


def _base_env(home: Path, bin_dir: Path) -> dict[str, str]:
    """Subprocess env: isolated ``HOME``, stub ``kimi`` first on ``PATH``.

    ``PYTHONPATH`` is pointed at this checkout (repo root plus the two local
    SDK packages) so the spawned CLI — and the runner the host launches —
    resolve the worktree under test even when the ambient interpreter's
    site-packages carries a different build.
    """
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm"
    # Isolate every ambient override that could redirect the journey away
    # from this test's fixture: KIMI_CODE_HOME moves kimi-auth detection off
    # the seeded $HOME/.kimi-code; OMNIGENT_CONFIG_HOME / OMNIGENT_DATA_DIR
    # bypass the isolated $HOME's config; the kimi binary-path overrides
    # would shadow the stub placed first on PATH.
    for var in (
        "KIMI_CODE_HOME",
        "OMNIGENT_DATA_DIR",
        "OMNIGENT_KIMI_PATH",
        "HARNESS_KIMI_PATH",
    ):
        env.pop(var, None)
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    pythonpath = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return env


def test_kimi_version_floor_targets_kimi_code_0x_series() -> None:
    """Guard: the kimi floor stays in Kimi Code's 0.x series.

    The bug was the floor being re-derived from the wrong upstream project
    (``kimi-cli``, a 1.x series). Kimi Code ships 0.x releases, so any floor
    at or above 1.0.0 is unsatisfiable by every build of the binary the spec
    itself installs. Also asserts the report's shipping build (0.32.0)
    satisfies the declared floor.
    """
    from omnigent.onboarding import harness_install as hi

    spec = hi.harness_install_spec(hi.KIMI_KEY)
    assert spec is not None, "kimi install spec missing"
    assert spec.min_version is not None, "kimi spec no longer declares a floor"
    assert Version(spec.min_version) < Version("1.0.0"), (
        f"kimi min_version {spec.min_version!r} is outside Kimi Code's 0.x "
        "series — this re-imports the separate kimi-cli project's numbering "
        "and rejects every shipping Kimi Code build"
    )
    assert Version(_REPORTED_KIMI_VERSION) >= Version(spec.min_version), (
        f"a current Kimi Code build ({_REPORTED_KIMI_VERSION}) no longer "
        f"satisfies the declared floor {spec.min_version!r}"
    )


def test_setup_kimi_row_does_not_read_needs_upgrade(tmp_path: Path) -> None:
    """``omni setup`` with a current Kimi Code CLI never shows "Needs upgrade".

    Reconstructs the reported journey: a fully up-to-date Kimi Code CLI
    (0.32.0) on PATH, fresh config, run ``omni setup``. On the buggy build the
    Kimi Code row read ``✗ Needs upgrade``; on a fixed build it reads
    ``Not configured`` (installed, no credential yet) or ``Signed in``.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub_kimi(bin_dir)
    env = _base_env(home, bin_dir)

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(50, 120),
        timeout=120,
        cwd=str(_REPO_ROOT),
    )
    try:
        child.expect(re.compile(rb"Configure harnesses"), timeout=120)
        # The harness rows render (and re-render) after the title; poll the
        # accumulated screen until the Kimi Code row carries a status. Seed
        # with pexpect's internal buffer: expect() may have already consumed
        # the chunk carrying the rows, and read_nonblocking() bypasses it.
        deadline = time.monotonic() + 30.0
        kimi_lines: list[str] = []
        collected = bytes(child.buffer or b"")
        while time.monotonic() < deadline:
            try:
                collected += child.read_nonblocking(size=65536, timeout=1)
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break
            text = _ANSI_RE.sub(b"", collected).decode("utf-8", "replace")
            kimi_lines = [line for line in text.splitlines() if "Kimi Code" in line]
            if any(
                marker in line
                for line in kimi_lines
                for marker in ("Needs upgrade", "Not configured", "Not installed", "Signed in")
            ):
                break
        assert kimi_lines, "omni setup never rendered a Kimi Code row"
        joined = "\n".join(kimi_lines)
        assert "Needs upgrade" not in joined, (
            "a current Kimi Code CLI "
            f"({_REPORTED_KIMI_VERSION}) is marked 'Needs upgrade' — the "
            f"version floor rejects every shipping 0.x build:\n{joined}"
        )
        assert "Not installed" not in joined, (
            f"the stub kimi on PATH was not detected at all:\n{joined}"
        )
        assert "Not configured" in joined or "Signed in" in joined, (
            f"unexpected Kimi Code row state:\n{joined}"
        )
    finally:
        with pexpect_suppress():
            child.sendcontrol("c")
        child.close(force=True)


class pexpect_suppress:
    """Tiny ``contextlib.suppress``-alike for teardown sends on a dead PTY."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


def test_kimi_auth_rejection_names_the_real_kimi_code_config_path(tmp_path: Path) -> None:
    """The ``executor.auth`` rejection must point at Kimi Code's real config.

    Journey: a configured Kimi Code install (CLI on PATH + login credential),
    launch a kimi agent whose spec declares ``executor.auth``. The launch is
    rejected by design (kimi has no per-spawn auth injection), and the error
    is *guidance*: it tells the user where to configure the provider instead.
    That guidance currently names ``~/.kimi/config.toml`` — the legacy
    ``kimi-cli`` project's path. Kimi Code reads ``$KIMI_CODE_HOME/config.toml``
    (default ``~/.kimi-code/config.toml``; ``kimi doctor`` confirms), so a
    user following the message edits a file their CLI never loads.

    FAILS on the buggy build (message says ``~/.kimi/config.toml``); passes
    once the guidance names the Kimi Code location.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub_kimi(bin_dir)

    # A completed `kimi login` credential under the isolated $HOME's default
    # Kimi Code location (~/.kimi-code), so the host's launch gate
    # (harness_is_configured) admits the kimi launch and the run reaches turn
    # setup where the rejection fires. The default path is used (rather than
    # $KIMI_CODE_HOME) because the host daemon's env allowlist forwards HOME
    # but not KIMI_CODE_HOME.
    kimi_home = home / ".kimi-code"
    (kimi_home / "credentials").mkdir(parents=True)
    (kimi_home / "credentials" / "kimi-code.json").write_text(
        '{"access_token": "e2e-stub-token"}\n'
    )

    env = _base_env(home, bin_dir)

    agent_yaml = tmp_path / "kimi_auth_probe.yaml"
    agent_yaml.write_text(
        textwrap.dedent(
            """\
            name: kimi-auth-probe
            description: Kimi agent that declares an executor auth block.
            executor:
              harness: kimi
              auth:
                type: api_key
                api_key: sk-e2e-stub
            prompt: |
              You are a probe agent.
            """
        )
    )

    proc = subprocess.run(
        [sys.executable, "-m", "omnigent", "run", str(agent_yaml), "-p", "hi"],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=160,
    )
    output = proc.stdout + proc.stderr

    # Control: the journey reached the kimi auth rejection (not some earlier
    # gate like "harness 'kimi' is not configured on host").
    assert "does not support per-invocation provider" in output, (
        f"the run never reached the kimi executor.auth rejection; output was:\n{output[-3000:]}"
    )

    # The actual bug: the guidance names the legacy kimi-cli path instead of
    # Kimi Code's config location.
    assert "~/.kimi/config.toml" not in output, (
        "the kimi auth guidance points users at ~/.kimi/config.toml — the "
        "legacy kimi-cli path. Kimi Code reads $KIMI_CODE_HOME/config.toml "
        "(default ~/.kimi-code/config.toml, per `kimi doctor`), so this "
        "guidance sends users to a file their CLI never loads."
    )
    assert ".kimi-code" in output or "KIMI_CODE_HOME" in output, (
        "the kimi auth guidance no longer names Kimi Code's real config "
        "location (~/.kimi-code / $KIMI_CODE_HOME); output was:\n"
        f"{output[-3000:]}"
    )
