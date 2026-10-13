"""Verify host and setup readiness honor a configured Kimi executable."""

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

_CUSTOM_KIMI_VERSION = "0.39.1"

_KIMI_READINESS_KEYS = ("kimi", "kimi-native", "native-kimi")

# Strip terminal escapes before matching setup rows.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def _write_custom_kimi(custom_dir: Path) -> Path:
    """Create a version-compatible Kimi shim outside PATH."""
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
    """Remove directories containing a Kimi binary from PATH."""
    return os.pathsep.join(
        d for d in path.split(os.pathsep) if d and not (Path(d) / "kimi").exists()
    )


def _custom_path_only_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Build an isolated logged-in environment with Kimi only by override."""
    home = tmp_path / "home"
    home.mkdir()
    shim = _write_custom_kimi(tmp_path / "custom-tools" / "kimi-code")

    # Seed the file-based login check.
    creds_dir = home / ".kimi-code" / "credentials"
    creds_dir.mkdir(parents=True)
    (creds_dir / "kimi-code.json").write_text('{"access_token": "e2e-custom-path-token"}\n')

    env = {**os.environ}
    # Keep spawned daemons independent from a parent runner or host.
    for var in list(env):
        if var.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST", "OMNIGENT_ZYGOTE")):
            env.pop(var)
    # Keep ambient overrides from redirecting the fixture.
    for var in ("KIMI_CODE_HOME", "HARNESS_KIMI_PATH", "OMNIGENT_DATA_DIR"):
        env.pop(var, None)
    env["HOME"] = str(home)
    env["PATH"] = _kimi_free_path(env.get("PATH", ""))
    env["OMNIGENT_KIMI_PATH"] = str(shim)
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm"
    # Import this worktree and its in-repo SDK packages.
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
    """Run a host daemon with the custom-path-only Kimi environment."""
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
    """Wait for the host daemon to register."""
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
    """Advertise custom-path Kimi as available in host readiness."""
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
    """Show a logged-in custom-path Kimi install as signed in during setup."""
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
        # Include data already consumed by expect while waiting for row status.
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
