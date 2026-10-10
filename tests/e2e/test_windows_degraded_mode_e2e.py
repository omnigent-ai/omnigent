"""Regression tests for Windows degraded-mode startup behavior."""

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
    """Return string literals passed to print."""
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
    """Native bridges must import when os.getuid is unavailable."""
    child = (
        "import os\n"
        "delattr(os, 'getuid')\n"
        "import omnigent.harnesses.kiro_native.bridge\n"
        "import omnigent.harnesses.hermes_native.bridge\n"
        "import omnigent.harnesses.kimi_native.bridge\n"
        "import omnigent.harnesses.qwen_native.bridge\n"
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
    """Status prints must survive a cp1252 console."""
    import omnigent.host.connect as connect
    from omnigent.cli import _build_host_daemon_env

    source = Path(connect.__file__).read_text(encoding="utf-8")
    literals = _print_call_string_literals(source)
    assert any("Connected as" in s for s in literals), (
        "sanity: expected the tunnel's connected-status print in host/connect.py"
    )

    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    daemon_env = _build_host_daemon_env(server_url="https://server.example")

    child_env = {**daemon_env}
    for essential in ("PATH", "SYSTEMROOT", "HOME"):
        if essential in os.environ and essential not in child_env:
            child_env[essential] = os.environ[essential]
    # Emulate Windows' default ANSI encoding when UTF-8 mode is absent.
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
    """Windows drive-absolute paths must pass workspace validation."""
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
    """The user's UTF-8 mode must reach local and remote host daemons."""
    from omnigent.cli import _build_host_daemon_env

    monkeypatch.setenv("PYTHONUTF8", "1")
    remote_env = _build_host_daemon_env(server_url="https://server.example")
    local_env = _build_host_daemon_env(server_url=None)
    assert remote_env.get("PYTHONUTF8") == "1", (
        "PYTHONUTF8 stripped from the remote host-daemon env"
    )
    assert local_env.get("PYTHONUTF8") == "1", "PYTHONUTF8 stripped from the local host-daemon env"


def test_os_tools_return_real_payloads_without_active_sandbox(
    tmp_path: Path,
) -> None:
    """OS tools must start without an active sandbox on Windows."""
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
        f"OS tool returned an error payload instead of running the command: {result!r}"
    )
    assert "degraded-mode-ok" in json.dumps(result), (
        f"shell output missing from OS tool result: {result!r}"
    )
