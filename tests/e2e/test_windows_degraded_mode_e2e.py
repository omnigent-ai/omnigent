"""End-to-end regression tests for the native-Windows degraded-mode subset.

The README promises that ``omnigent server``, the web UI, and the SDK-based
harnesses run on native Windows in degraded mode (no sandbox, no native tmux
wrappers). A user walking that documented path hits a chain of defects; each
test below pins one link of the chain, simulated faithfully on POSIX CI:

1. importing the tmux-based native bridges must not require ``os.getuid``;
2. the host tunnel's status prints must survive a cp1252 console (the
   Windows default), where non-cp1252 glyphs raise ``UnicodeEncodeError``
   inside the tunnel handler and wedge the daemon in a reconnect loop;
3. session workspace validation must accept Windows drive-absolute paths;
4. the host-daemon env build must not strip ``PYTHONUTF8``, so users can
   force UTF-8 stdio for daemon/runner processes;
5. the OS tool helper must start without an active sandbox (Windows never
   has one), instead of tripping the config-delivery branch's tmpdir
   precondition and returning bare error payloads for every OS tool call.

These tests need no live server or credentials::

    .venv/bin/python -m pytest tests/e2e/test_windows_degraded_mode_e2e.py -v
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


def _print_call_string_literals(source: str) -> list[str]:
    """Collect every string literal appearing inside a ``print(...)`` call.

    Includes the constant parts of f-strings, so a glyph embedded in an
    interpolated status message (e.g. a checkmark prefix) is captured.

    :param source: Python source text to scan.
    :returns: The string literals, in source order.
    """
    literals: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            for arg in node.args:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        literals.append(sub.value)
    return literals


def test_native_bridges_import_without_os_getuid() -> None:
    """The tmux-based native bridges must import on a platform without getuid.

    Windows has no ``os.getuid``; a module-level call kills ``omnigent server``
    startup with ``AttributeError`` before anything runs. Simulate by deleting
    the attribute in a child interpreter and importing all four bridges.
    """
    child = (
        "import os\n"
        "delattr(os, 'getuid')\n"
        "import omnigent.kiro_native_bridge\n"
        "import omnigent.hermes_native_bridge\n"
        "import omnigent.kimi_native_bridge\n"
        "import omnigent.qwen_native_bridge\n"
        "print('BRIDGES_IMPORT_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"native bridge import requires os.getuid (server startup would crash "
        f"on Windows):\n{proc.stderr}"
    )
    assert "BRIDGES_IMPORT_OK" in proc.stdout


def test_host_tunnel_status_prints_survive_cp1252_console(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tunnel's status prints must not crash a cp1252 (Windows) console.

    ``host/connect.py`` prints status messages (the post-handshake
    "Connected as ..." success line, the runner-started line) from inside the
    tunnel handler. On default Windows stdio (cp1252) a non-encodable glyph
    raises ``UnicodeEncodeError`` there, tearing down a healthy tunnel into an
    infinite reconnect loop, and ``omni run`` times out waiting for the host.

    Reproduce the console: print every literal used in the module's ``print``
    calls in a child interpreter whose stdio encodes exactly like the daemon's
    would on a default Windows box — cp1252, unless the daemon env itself
    forces UTF-8 mode.
    """
    import omnigent.host.connect as connect
    from omnigent.cli import _build_host_daemon_env

    source = Path(connect.__file__).read_text(encoding="utf-8")
    literals = _print_call_string_literals(source)
    assert any("Connected as" in s for s in literals), (
        "sanity: expected the tunnel's connected-status print in host/connect.py"
    )

    # The user has set nothing: a stock Windows shell has no UTF-8 overrides.
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    daemon_env = _build_host_daemon_env(server_url="https://server.example")

    child_env = {**daemon_env}
    for essential in ("PATH", "SYSTEMROOT", "HOME"):
        if essential in os.environ and essential not in child_env:
            child_env[essential] = os.environ[essential]
    # A default Windows console encodes stdio as the ANSI code page (cp1252)
    # unless the process runs in UTF-8 mode. Emulate that default here.
    if not child_env.get("PYTHONUTF8") and not child_env.get("PYTHONIOENCODING"):
        child_env["PYTHONIOENCODING"] = "cp1252"

    literals_file = tmp_path / "status-literals.json"
    literals_file.write_text(json.dumps(literals), encoding="utf-8")
    child = (
        "import json, sys\n"
        "for s in json.load(open(sys.argv[1], encoding='utf-8')):\n"
        "    print(s)\n"
        "print('STATUS_PRINTS_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", child, str(literals_file)],
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0 and "STATUS_PRINTS_OK" in proc.stdout, (
        f"a host-tunnel status print crashes on a cp1252 console (this kills "
        f"the tunnel on native Windows):\n{proc.stderr}"
    )


def test_workspace_validation_accepts_windows_drive_paths() -> None:
    """Session workspace validation must accept Windows drive-absolute paths.

    A bare ``workspace.startswith('/')`` check rejects every Windows
    workspace (e.g. ``D:\\myproject``) with HTTP 400
    "workspace must be an absolute path starting with /".

    Drives the real ``validate_workspace`` entry point (not a re-derived
    expression) with an empty host registry: an absolute Windows path must get
    PAST the absoluteness check (failing later on the offline host), while a
    relative path must be rejected by the absoluteness check itself.
    """
    import asyncio

    from omnigent.server.routes._workspace_validation import (
        WorkspaceValidationError,
        validate_workspace,
    )

    class _EmptyRegistry:
        def get(self, host_id: str) -> None:
            return None

    def _rejection(workspace: str) -> str:
        try:
            asyncio.run(
                validate_workspace(
                    host_registry=_EmptyRegistry(),
                    host_id="host_missing",
                    workspace=workspace,
                    spec_cwd=None,
                )
            )
        except WorkspaceValidationError as exc:
            return str(exc)
        return ""

    for workspace in ("D:\\myproject", "C:\\Users\\dev\\proj", "C:/Users/dev/proj"):
        message = _rejection(workspace)
        assert "absolute path" not in message, (
            f"absolute Windows workspace rejected as non-absolute: {workspace!r}: {message}"
        )
        assert "offline" in message, f"expected the offline-host failure, got: {message!r}"
    for workspace in ("myproject", "relative\\path"):
        message = _rejection(workspace)
        assert "absolute path" in message, (
            f"relative workspace passed the absoluteness check: {workspace!r}: {message!r}"
        )


def test_host_daemon_env_preserves_pythonutf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PYTHONUTF8`` set by the user must reach the spawned host daemon.

    The daemon env is built from an allowlist; dropping Python runtime-mode
    vars leaves cp1252 consoles with no workaround for glyph crashes.
    """
    from omnigent.cli import _build_host_daemon_env

    monkeypatch.setenv("PYTHONUTF8", "1")
    remote_env = _build_host_daemon_env(server_url="https://server.example")
    local_env = _build_host_daemon_env(server_url=None)
    assert remote_env.get("PYTHONUTF8") == "1", (
        "PYTHONUTF8 stripped from the remote host-daemon env"
    )
    assert local_env.get("PYTHONUTF8") == "1", (
        "PYTHONUTF8 stripped from the local host-daemon env"
    )


def test_os_tools_return_real_payloads_without_active_sandbox(
    tmp_path: Path,
) -> None:
    """OS tools must work when the sandbox is inactive and the platform is Windows.

    Native Windows never has an active sandbox, but the Windows config-delivery
    branch of the helper startup requires the private scratch tmpdir that only
    an *active* sandbox creates. The helper then can never start, and every
    ``sys_os_shell`` / ``sys_os_read`` call returns a bare error payload.

    Build that exact state — a ``none`` (inactive) sandbox with the platform
    flag forced to Windows — and drive a real shell op through the helper.
    """
    from omnigent.inner import os_env as os_env_mod
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.inner.sandbox import resolve_sandbox

    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    sandbox = resolve_sandbox(spec, tmp_path)
    assert not sandbox.active, "sanity: the 'none' sandbox must be inactive"

    client = os_env_mod._HelperProcessClient(
        cwd=tmp_path,
        shell_path="/bin/sh",
        sandbox=sandbox,
    )
    try:
        with mock.patch.object(os_env_mod, "IS_WINDOWS", True):
            result = client.request(
                {"op": "shell", "command": "echo degraded-mode-ok", "timeout": 30}
            )
    finally:
        client.close()

    assert isinstance(result, dict)
    assert not result.get("error"), (
        f"OS tool returned an error payload instead of running the command: "
        f"{result!r}"
    )
    assert "degraded-mode-ok" in json.dumps(result), (
        f"shell output missing from OS tool result: {result!r}"
    )
