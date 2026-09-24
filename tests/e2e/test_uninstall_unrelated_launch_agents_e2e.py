"""
Uninstall e2e: ``omnigent uninstall`` must not delete unrelated launch agents.

Drives the real ``omnigent uninstall --yes`` CLI as a subprocess — the exact
command a user types — in an isolated HOME containing a *third-party* launch
agent whose filename merely contains "omnigent"
(``com.example.omnigent-handoff``). The uninstall ledger backfill globs
``~/Library/LaunchAgents/*omnigent*.plist`` and
``$XDG_CONFIG_HOME/systemd/user/*omnigent*.service`` by filename, so the
third-party unit is unloaded and its file deleted even though Omnigent never
created it.

``platform.system()`` is pinned to ``"Darwin"`` in the CLI subprocess via a
``sitecustomize`` module on ``PYTHONPATH`` for the launchd journey, and
scripted ``launchctl``/``systemctl`` stubs record every unload request, so the
macOS journey runs on any CI host without touching the real service managers.

The user journey under guard::

    # a third-party watcher, not created by Omnigent, that happens to have
    # "omnigent" in its filename
    ~/Library/LaunchAgents/com.example.omnigent-handoff.plist
    omnigent uninstall --yes        # must leave the third-party unit alone

Usage::

    python -m pytest tests/e2e/test_uninstall_unrelated_launch_agents_e2e.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_THIRD_PARTY_LABEL = "com.example.omnigent-handoff"

_THIRD_PARTY_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.example.omnigent-handoff</string>
  <key>ProgramArguments</key><array><string>/usr/local/bin/handoff-watcher</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""

_THIRD_PARTY_SERVICE = """\
[Unit]
Description=Third-party handoff watcher (calls the Omnigent API)
[Service]
ExecStart=/usr/local/bin/handoff-watcher
"""

_PROFILE_MARKER_BEGIN = "# >>> Omnigent installer >>>"
_PROFILE_BLOCK = (
    "# >>> Omnigent installer >>>\n"
    'export PATH="$HOME/.local/bin:$PATH"\n'
    "# <<< Omnigent installer <<<\n"
)

# Appends every invocation to a log so the test can assert which units the
# uninstaller asked the service manager to unload.
_LOGGING_STUB = """\
#!/bin/sh
echo "$@" >> {log}
exit 0
"""

# Imported at interpreter startup (site picks it up from PYTHONPATH), so the
# CLI subprocess takes the macOS code path on any CI host.
_SITECUSTOMIZE = 'import platform\nplatform.system = lambda: "Darwin"\n'


@dataclass(frozen=True)
class _UninstallHarness:
    """Isolated home + env for driving ``omnigent uninstall`` subprocesses."""

    home: Path
    env: dict[str, str]
    cwd: Path
    launchctl_log: Path
    systemctl_log: Path

    @property
    def third_party_plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{_THIRD_PARTY_LABEL}.plist"

    @property
    def third_party_service(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / f"{_THIRD_PARTY_LABEL}.service"

    def run_uninstall(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``omnigent uninstall <args>`` exactly as a user would."""
        return subprocess.run(
            [sys.executable, "-m", "omnigent.cli", "uninstall", *args],
            env=self.env,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def unload_requests(self, log: Path) -> str:
        try:
            return log.read_text()
        except OSError:
            return ""

    def assert_destructive_run_happened(self, result: subprocess.CompletedProcess[str]) -> None:
        """The journey is only meaningful if uninstall really acted."""
        assert result.returncode == 0, (
            f"omnigent uninstall --yes failed (exit={result.returncode}): "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "Preview only" not in result.stdout, (
            "uninstall ran as a dry-run; the destructive path was never exercised"
        )
        profile = (self.home / ".zshrc").read_text()
        assert _PROFILE_MARKER_BEGIN not in profile, (
            "uninstall did not remove the installer profile block, so the "
            "destructive CLI cleanup never ran"
        )


def _build_harness(tmp_path: Path, *, darwin: bool) -> _UninstallHarness:
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    # The shell-profile block a wheel install leaves behind: the install
    # signal that lets the ledger backfill and uninstall script proceed.
    (home / ".zshrc").write_text(_PROFILE_BLOCK)

    third_party_plist = home / "Library" / "LaunchAgents" / f"{_THIRD_PARTY_LABEL}.plist"
    third_party_plist.write_text(_THIRD_PARTY_PLIST)
    third_party_service = home / ".config" / "systemd" / "user" / f"{_THIRD_PARTY_LABEL}.service"
    third_party_service.write_text(_THIRD_PARTY_SERVICE)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    systemctl_log = tmp_path / "systemctl.log"
    for name, log in (("launchctl", launchctl_log), ("systemctl", systemctl_log)):
        stub = bin_dir / name
        stub.write_text(_LOGGING_STUB.format(log=log))
        stub.chmod(0o755)
    # No tmux sessions to sweep; keep the uninstaller away from any real ones.
    tmux = bin_dir / "tmux"
    tmux.write_text("#!/bin/sh\nexit 0\n")
    tmux.chmod(0o755)
    # Shadow any real uv so the wheel step cannot touch the actual install.
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/sh\necho "omnigent is not installed"\nexit 1\n')
    uv.chmod(0o755)

    cwd = tmp_path / "cwd"
    cwd.mkdir()

    pythonpath_entries = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
    if darwin:
        pysite = tmp_path / "pysite"
        pysite.mkdir()
        (pysite / "sitecustomize.py").write_text(_SITECUSTOMIZE)
        pythonpath_entries.insert(0, str(pysite))

    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": os.pathsep.join(entry for entry in pythonpath_entries if entry),
    }
    return _UninstallHarness(
        home=home,
        env=env,
        cwd=cwd,
        launchctl_log=launchctl_log,
        systemctl_log=systemctl_log,
    )


@pytest.fixture
def uninstall_cli_darwin(tmp_path: Path) -> _UninstallHarness:
    """macOS-shaped environment: launchd journey with a scripted launchctl."""
    return _build_harness(tmp_path, darwin=True)


@pytest.fixture
def uninstall_cli(tmp_path: Path) -> _UninstallHarness:
    """Host-native environment: systemd user-unit journey."""
    return _build_harness(tmp_path, darwin=False)


def test_uninstall_preserves_unrelated_launchd_plist(
    uninstall_cli_darwin: _UninstallHarness,
) -> None:
    """A third-party LaunchAgent matching ``*omnigent*.plist`` must survive.

    The uninstall ledger backfill selects launchd units by filename glob, so a
    watcher named ``com.example.omnigent-handoff`` — created by someone else,
    merely containing "omnigent" — is unloaded and its plist deleted by
    ``omnigent uninstall --yes``. Omnigent must remove only units it created.
    """
    harness = uninstall_cli_darwin
    result = harness.run_uninstall("--yes")
    harness.assert_destructive_run_happened(result)

    assert harness.third_party_plist.exists(), (
        f"omnigent uninstall --yes deleted the third-party LaunchAgent "
        f"{harness.third_party_plist} that Omnigent never created "
        f"(report: {result.stdout!r})"
    )
    unloads = harness.unload_requests(harness.launchctl_log)
    assert _THIRD_PARTY_LABEL not in unloads, (
        f"omnigent uninstall --yes asked launchd to unload the third-party "
        f"job {_THIRD_PARTY_LABEL}: launchctl was invoked with {unloads!r}"
    )


def test_uninstall_preserves_unrelated_systemd_user_unit(
    uninstall_cli: _UninstallHarness,
) -> None:
    """The same name-glob defect on the Linux path: ``*omnigent*.service``.

    A third-party systemd user unit whose filename contains "omnigent" is
    stopped and its unit file deleted by ``omnigent uninstall --yes``.
    """
    harness = uninstall_cli
    result = harness.run_uninstall("--yes")
    harness.assert_destructive_run_happened(result)

    assert harness.third_party_service.exists(), (
        f"omnigent uninstall --yes deleted the third-party systemd user unit "
        f"{harness.third_party_service} that Omnigent never created "
        f"(report: {result.stdout!r})"
    )
    unloads = harness.unload_requests(harness.systemctl_log)
    assert _THIRD_PARTY_LABEL not in unloads, (
        f"omnigent uninstall --yes asked systemd to stop the third-party "
        f"unit {_THIRD_PARTY_LABEL}: systemctl was invoked with {unloads!r}"
    )
