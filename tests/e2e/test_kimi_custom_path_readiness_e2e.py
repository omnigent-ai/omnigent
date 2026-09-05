"""E2E: Kimi readiness must honor a configured custom executable path.

Launch resolves the effective ``kimi`` executable via
``omnigent.kimi_native._configured_kimi_command`` — ``OMNIGENT_KIMI_PATH``
(legacy alias ``HARNESS_KIMI_PATH``) and the ``harness.kimi-native.command``
config are honored, so a user whose *only* Kimi Code install lives at a custom
path (nothing named ``kimi`` on ``PATH``) can launch ``omnigent kimi`` just
fine. Readiness, however, starts with a bare ``PATH`` lookup for the install
spec's ``kimi`` binary (``harness_cli_installed`` → ``resolve_cli_binary``
without the env override) and gives up when that returns nothing — the
custom-path install never even reaches the version gate.

Two user surfaces show the resulting lie, and both are driven for real here:

1. **Host readiness** — an ``omnigent host`` daemon started in that
   environment advertises ``configured_harnesses["kimi" / "kimi-native" /
   "native-kimi"] == false``, so the web picker reports Kimi as unavailable
   ("binary missing" / "isn't configured on <host>") on a machine where it
   genuinely works.
2. **``omni setup``** — the Kimi Code row reads ``✗ Not installed`` in the
   same environment.

Journey staged by both tests (the reported machine state):

1. install Kimi Code at a custom path only — a working ``kimi`` binary on a
   supported version, in a directory that is NOT on ``PATH``,
2. complete ``kimi login`` (credential file under ``~/.kimi-code``),
3. point Omnigent at the executable with ``OMNIGENT_KIMI_PATH`` (what the
   ``harness.kimi-native.command`` config threads into),
4. observe readiness: the host's harness map / the ``omni setup`` row.

The ``kimi`` binary is a shim (CI has no real Kimi Code install) that answers
``--version`` like a supported release; readiness only ever probes binary
presence + ``--version`` + file-based credentials, so the shim exercises the
real probe path. FAILS while readiness demands a ``PATH`` kimi; passes once
readiness resolves the effective executable the way launch does.

Usage::

    python -m pytest tests/e2e/test_kimi_custom_path_readiness_e2e.py -v --timeout=300
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A supported Kimi Code release (the declared floor is in the 0.x series), so
# the version gate would pass if readiness ever got as far as running it.
_CUSTOM_KIMI_VERSION = "0.39.1"

# Readiness spellings the host advertises for the Kimi surfaces. All three
# must be available when the configured custom-path install is usable.
_KIMI_READINESS_KEYS = ("kimi", "kimi-native", "native-kimi")

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so TUI rows
# can be matched as plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _write_custom_kimi(custom_dir: Path) -> Path:
    """Write a working ``kimi`` stand-in at a custom (off-``PATH``) location.

    The readiness layer only ever runs ``kimi --version`` (credential state is
    read from files), so a shim reporting a supported version is a faithful
    stand-in for a real custom-path install.

    :param custom_dir: Directory to hold the binary. Never added to ``PATH``.
    :returns: Path to the executable shim.
    """
    custom_dir.mkdir(parents=True, exist_ok=True)
    shim = custom_dir / "kimi"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "--version" ]; then\n'
        f'  echo "kimi-code {_CUSTOM_KIMI_VERSION}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "OK"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _kimi_free_path(path: str) -> str:
    """Return *path* with every directory that contains a ``kimi`` dropped.

    CI (and developer machines) may carry a real ``kimi`` on ``PATH``; the
    journey under test is a machine whose ONLY install is the custom-path one.

    :param path: The inherited ``PATH`` value.
    :returns: The filtered ``PATH``.
    """
    return os.pathsep.join(
        d for d in path.split(os.pathsep) if d and not (Path(d) / "kimi").exists()
    )


def _custom_path_only_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Build the reported machine state as a subprocess environment.

    - a working ``kimi`` at a custom path, NOT on ``PATH``;
    - no ``kimi`` anywhere on ``PATH`` (inherited entries filtered);
    - an isolated ``HOME`` so the resolver's global fallback dirs
      (``~/.local/bin`` etc.) cannot leak a machine-local install in;
    - a completed ``kimi login`` credential under that ``HOME``;
    - ``OMNIGENT_KIMI_PATH`` pointing at the custom executable.

    :param tmp_path: Per-test temp dir.
    :returns: ``(env, shim_path)``.
    """
    home = tmp_path / "home"
    home.mkdir()
    shim = _write_custom_kimi(tmp_path / "custom-tools" / "kimi-code")

    # A completed `kimi login` credential in the default Kimi Code home, so
    # the credential check passes and the PATH gate is the only failing probe.
    creds_dir = home / ".kimi-code" / "credentials"
    creds_dir.mkdir(parents=True)
    (creds_dir / "kimi-code.json").write_text('{"access_token": "e2e-custom-path-token"}\n')

    env = {**os.environ}
    # Drop any ambient runner/host identity (present when this test itself
    # runs inside a server-spawned runner) so spawned daemons start clean.
    for var in list(env):
        if var.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST", "OMNIGENT_ZYGOTE")):
            env.pop(var)
    # Isolate every ambient override that could redirect the journey away
    # from this test's fixture.
    for var in ("KIMI_CODE_HOME", "HARNESS_KIMI_PATH", "OMNIGENT_DATA_DIR"):
        env.pop(var, None)
    env["HOME"] = str(home)
    env["PATH"] = _kimi_free_path(env.get("PATH", ""))
    env["OMNIGENT_KIMI_PATH"] = str(shim)
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm"
    # Import the branch's source (and its in-repo SDK packages) rather than
    # whatever omnigent is installed in the venv — same reasoning as the
    # live_server fixture's PYTHONPATH.
    pythonpath = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return env, shim


@contextmanager
def _custom_path_kimi_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
) -> Iterator[subprocess.Popen[bytes]]:
    """Spawn an ``omnigent host`` daemon with a custom-path-only Kimi install.

    :param tmp_path: Per-test temp dir for the shim, homes, and daemon log.
    :param live_server: Test server URL the daemon registers with.
    :returns: The spawned daemon subprocess handle (terminated on exit).
    """
    env, _ = _custom_path_only_env(tmp_path)

    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        daemon = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    try:
        yield daemon
    finally:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


def _online_host_id(client: httpx.Client, timeout: float = 60.0) -> str:
    """Poll ``GET /v1/hosts`` until a host is online; return its id.

    :param client: HTTP client bound to the live server.
    :param timeout: Max seconds to wait for the daemon to register.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(1.0)
    raise AssertionError(f"No host came online within {timeout}s")


@pytest.mark.timeout(180)
def test_host_readiness_honors_custom_kimi_path(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A custom-path-only Kimi install must read available in host readiness.

    With ``OMNIGENT_KIMI_PATH`` pointing at a working, version-supported,
    logged-in Kimi Code install and nothing named ``kimi`` on ``PATH``,
    ``omnigent kimi`` launches — so the host's ``configured_harnesses`` map
    must not advertise the Kimi spellings as unavailable. On the buggy build
    readiness hard-gates on a ``PATH`` lookup and reports ``false`` /
    ``binary-missing``, hiding Kimi from the web picker on a machine where it
    genuinely works.
    """
    with _custom_path_kimi_host_daemon(tmp_path=tmp_path, live_server=live_server):
        host_id = _online_host_id(http_client)

        resp = http_client.get(f"/v1/hosts/{host_id}")
        assert resp.status_code == 200, resp.text
        host = resp.json()

        configured = host.get("configured_harnesses")
        assert configured is not None, (
            "host connected without a readiness map — the daemon's harness "
            "probe failed; check the daemon log"
        )
        missing_keys = [key for key in _KIMI_READINESS_KEYS if key not in configured]
        assert not missing_keys, (
            f"readiness map lacks the Kimi spellings {missing_keys!r}; cannot "
            f"assess the custom-path journey: {configured!r}"
        )

        # The credential check passes (seeded login) and the configured
        # executable is on a supported version, so an unavailable verdict here
        # can only come from the PATH-presence gate — the reported bug.
        wrongly_unavailable = {
            key: configured[key]
            for key in _KIMI_READINESS_KEYS
            if configured[key] is False or configured[key] == "binary-missing"
        }
        assert not wrongly_unavailable, (
            "Kimi readiness reports unavailable despite a working custom-path "
            f"install configured via OMNIGENT_KIMI_PATH: {wrongly_unavailable!r}. "
            "Readiness requires a PATH `kimi` (harness_cli_installed → "
            "resolve_cli_binary without the override) even though launch "
            "(_configured_kimi_command) resolves and uses the configured "
            "executable — resolve the effective executable first; PATH should "
            "be the fallback, not the precondition."
        )


@pytest.mark.timeout(240)
def test_setup_kimi_row_honors_custom_kimi_path(tmp_path: Path) -> None:
    """``omni setup`` must not read "Not installed" for a custom-path install.

    Same machine state as the host-readiness journey, driven through the setup
    TUI under a pseudo-TTY. On the buggy build the Kimi Code row reads
    ``✗ Not installed`` (the same PATH-only gate); on a fixed build the
    configured install is credited and the row reads ``Signed in`` (the seeded
    login credential) — never an absence state.
    """
    env, _ = _custom_path_only_env(tmp_path)

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
                for marker in ("Not installed", "Not configured", "Signed in", "Needs upgrade")
            ):
                break
        assert kimi_lines, "omni setup never rendered a Kimi Code row"
        joined = "\n".join(kimi_lines)
        assert "Not installed" not in joined, (
            "a working custom-path Kimi Code install (OMNIGENT_KIMI_PATH set, "
            "supported version, logged in) is marked 'Not installed' — the "
            f"readiness predicate only consults PATH:\n{joined}"
        )
        assert "Signed in" in joined, (
            f"unexpected Kimi Code row state for a logged-in custom-path install:\n{joined}"
        )
    finally:
        with contextlib.suppress(Exception):
            child.sendcontrol("c")
        child.close(force=True)
