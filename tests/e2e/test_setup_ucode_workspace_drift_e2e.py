"""Verify that ``omni setup`` warns about ucode workspace drift without changing
the configured credential label.

``omni setup`` runs for real under a pseudo-TTY against a seeded HOME. ucode is
stood in by :mod:`tests.e2e._fake_ucode`; ``databricks`` and ``uvx`` shims fail
loudly so the host's CLIs and login state never take part.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FAKE_UCODE = Path(__file__).with_name("_fake_ucode.py")

WORKSPACE_A = "https://workspace-a.cloud.databricks.com"
WORKSPACE_B = "https://workspace-b.cloud.databricks.com"
PROFILE_A = "ai_devtools"
PROFILE_B = "fevm2"
DRIFT_MARKER = "ucode now points at"

_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")
_UG_SHIM = '#!/usr/bin/env bash\nexec "{python}" "{script}" "$@"\n'
_FORBIDDEN_SHIM = (
    "#!/usr/bin/env bash\n"
    'echo "{name} $*" >> "$HOME/forbidden-cli-calls.log"\n'
    'echo "{name} must not run in this test: $*" >&2\n'
    "exit 97\n"
)


def _write_shims(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shims = {"ug": _UG_SHIM.format(python=sys.executable, script=_FAKE_UCODE)}
    for name in ("databricks", "uvx"):
        shims[name] = _FORBIDDEN_SHIM.format(name=name)
    for name, content in shims.items():
        path = bin_dir / name
        path.write_text(content)
        path.chmod(0o755)


def _seed_home(home: Path) -> None:
    """Leave HOME as ``omni setup`` does after adding PROFILE_A for WORKSPACE_A."""
    (home / ".databrickscfg").write_text(
        f"[{PROFILE_A}]\nhost = {WORKSPACE_A}\nauth_type = databricks-cli\n"
    )
    (home / ".omnigent").mkdir()
    (home / ".omnigent" / "config.yaml").write_text(
        "providers:\n"
        "  databricks:\n"
        "    kind: databricks\n"
        "    default: true\n"
        f"    profile: {PROFILE_A}\n"
    )


def _env(home: Path, bin_dir: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DATABRICKS_", "OMNIGENT_"))
        and not k.endswith("_API_KEY")
        and k not in ("GH_TOKEN", "GITHUB_TOKEN")
    }
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        PYTHONPATH=str(_REPO_ROOT),
        NO_COLOR="1",
        TERM="xterm",
        OMNIGENT_CONFIG_HOME=str(home / ".omnigent"),
    )
    return env


def _strip(raw: bytes) -> str:
    return _ANSI_RE.sub(b"", raw).decode("utf-8", "replace")


def _frame(child: pexpect.spawn, settle: float = 1.5) -> str:
    """Return the ANSI-stripped output already buffered plus anything arriving within *settle*."""
    buf = bytes(child.buffer)
    child.buffer = b""
    deadline = time.time() + settle
    while time.time() < deadline:
        try:
            buf += child.read_nonblocking(4096, timeout=0.3)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
    return _strip(buf)


def _status_row(overview: str, harness: str) -> str:
    for line in reversed(overview.splitlines()):
        stripped = line.replace("❯", "").strip()
        if stripped.startswith(harness + " "):
            return " ".join(stripped.split())
    raise AssertionError(f"no {harness!r} row in the harness overview:\n{overview}")


def _quit(child: pexpect.spawn) -> None:
    with contextlib.suppress(Exception):
        child.send(b"\x1b")
        child.expect(pexpect.EOF, timeout=30)
    with contextlib.suppress(Exception):
        child.close(force=True)


def _setup_overview(env: dict[str, str]) -> str:
    """Launch ``omni setup`` and return everything it rendered up to the overview."""
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(40, 120),
        timeout=120,
    )
    try:
        child.expect(rb"Configure harnesses", timeout=120)
        # A banner renders above the title (child.before); the rows follow it.
        return _strip(bytes(child.before or b"")) + _frame(child, 2.5)
    finally:
        _quit(child)


def _ucode_configure(env: dict[str, str], url: str, profile: str | None = None) -> None:
    argv = ["ug", "configure", "--workspaces", url, "--agents", "claude,codex"]
    if profile:
        argv += ["--profile", profile]
    subprocess.run(argv, env=env, check=True, capture_output=True, text=True)


@pytest.mark.timeout(300)
def test_setup_warns_when_ucode_switches_workspace(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _seed_home(home)
    bin_dir = tmp_path / "bin"
    _write_shims(bin_dir)
    env = _env(home, bin_dir)
    _ucode_configure(env, WORKSPACE_A)

    shown = _setup_overview(env)
    assert f"Databricks ({PROFILE_A})" in _status_row(shown, "Claude"), shown
    assert DRIFT_MARKER not in shown, shown

    _ucode_configure(env, WORKSPACE_B, PROFILE_B)
    state = json.loads((home / ".ucode" / "state.json").read_text())
    assert state["current_workspace"] == WORKSPACE_B

    shown = _setup_overview(env)
    host_a = WORKSPACE_A.split("://", 1)[-1]
    host_b = WORKSPACE_B.split("://", 1)[-1]
    # The banner wraps in the 120-column PTY, so compare it whitespace-normalized.
    assert (
        f"⚠ Databricks: {DRIFT_MARKER} {host_b}, but Omnigent still uses the "
        f"'{PROFILE_A}' profile ({host_a}). Reconfigure Databricks to follow ucode."
    ) in " ".join(shown.split()), shown
    # The credential row keeps naming the profile sessions route through.
    assert f"Databricks ({PROFILE_A})" in _status_row(shown, "Claude"), shown
    forbidden = home / "forbidden-cli-calls.log"
    assert not forbidden.exists(), forbidden.read_text()
