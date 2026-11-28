"""Credential-tool configuration uses persistent, caller-selected environments."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest

from omnigent.onboarding import credential_tool


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required for tool installation")
def test_saved_helper_survives_cache_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    wheel = cache / "credential_kit-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "credential_kit.py",
            "import json, os, shutil, sys\n"
            "from pathlib import Path\n"
            "def main():\n"
            "    if sys.argv[1] == 'configure':\n"
            "        Path(os.environ['TEST_STATE']).write_text(json.dumps({\n"
            "            'command': shutil.which('credential-helper'),\n"
            "            'interpreter': os.environ.get('PEX_PYTHON')}))\n"
            "    else:\n"
            "        print('synthetic-token')\n",
        )
        archive.writestr(
            "credential_kit-0.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: credential-kit\nVersion: 0.0.0\n",
        )
        archive.writestr(
            "credential_kit-0.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            "credential_kit-0.0.0.dist-info/entry_points.txt",
            "[console_scripts]\ncredential-helper = credential_kit:main\n",
        )
        archive.writestr("credential_kit-0.0.0.dist-info/RECORD", "")
    state = tmp_path / "state.json"
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("TEST_STATE", str(state))
    monkeypatch.setenv("PEX_PYTHON", str(tmp_path / "missing-interpreter"))
    original = ["uvx", "--from", str(wheel), "credential-helper", "configure"]
    command = credential_tool.persistent_configuration_command(original)
    subprocess.run(command, check=True, capture_output=True, timeout=30)
    auth = json.loads(state.read_text())
    assert auth["interpreter"] == os.environ["PEX_PYTHON"]
    result = subprocess.run(
        [auth["command"], "token"], capture_output=True, text=True, check=True, timeout=5
    )
    assert result.stdout.strip() == "synthetic-token"
    shutil.rmtree(cache)
    cache.mkdir()
    (cache / "replacement").write_text("different archive contents")
    result = subprocess.run(
        [auth["command"], "token"], capture_output=True, text=True, check=True, timeout=5
    )
    assert result.stdout.strip() == "synthetic-token"
    subprocess.run(command, check=True, capture_output=True, timeout=5)
    assert json.loads(state.read_text())["command"] == auth["command"]
    assert cache not in Path(auth["command"]).parents


@pytest.mark.parametrize(
    "command",
    [
        ["/custom/credential-helper", "configure"],
        ["sh", "-c", "custom-helper --selected-profile"],
        ["uvx", "--from", "package", "helper"],
        ["uvx", "--isolated", "helper", "configure", "--profile", "selected"],
        ["uvx", "--from", "example-kit==1.0", "--python", "3.12", "helper", "configure"],
        ["uvx", "--from", "example-kit==1.0", "--", "helper", "configure"],
    ],
)
def test_custom_command_is_not_replaced(command: list[str]) -> None:
    assert credential_tool.persistent_configuration_command(command) == command


def test_command_factory_preserves_arguments_and_does_not_install() -> None:
    @credential_tool.persistent_tool_configuration
    def configure(profile: str) -> list[str]:
        return ["uvx", "--from", "example-kit==1.0", "credential-helper", "configure", profile]

    with patch.object(credential_tool, "install_credential_tool") as install:
        command = configure("selected-profile")
    install.assert_not_called()
    assert command[-3:] == ["--", "configure", "selected-profile"]
    assert command[command.index("--distribution") + 1] == "example-kit==1.0"
    assert command[command.index("--entrypoint") + 1] == "credential-helper"


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout", "cancel"])
def test_installation_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setenv("OMNIGENT_HOST_TOKEN", "not-for-the-installer")
    monkeypatch.setenv("PEX_PYTHON", "/custom/python")

    def install(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[1:] == ["tool", "install", "--link-mode", "copy", "example-kit==1.0"]
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["PEX_PYTHON"] == "/custom/python"
        assert "OMNIGENT_HOST_TOKEN" not in env
        if failure == "exit":
            raise subprocess.CalledProcessError(1, argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 90)
        if failure == "cancel":
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(argv, 0)

    with patch.object(credential_tool.shutil, "which", return_value="/tools/uv"):
        with patch.object(credential_tool.subprocess, "run", install):
            with pytest.raises((ValueError, subprocess.SubprocessError, KeyboardInterrupt)):
                credential_tool.install_credential_tool(
                    "example-kit==1.0", "credential-helper", tmp_path
                )


@pytest.mark.parametrize("entrypoint", ["../helper", "/custom/helper", "", "helper/child"])
def test_installation_rejects_non_entrypoint_paths(tmp_path: Path, entrypoint: str) -> None:
    with pytest.raises(ValueError):
        credential_tool.install_credential_tool("example-kit==1.0", entrypoint, tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX termination signals")
def test_termination_reaps_the_running_installer(tmp_path: Path) -> None:
    installer = tmp_path / "uv"
    pid_file = tmp_path / "installer.pid"
    installer.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    installer.chmod(0o700)
    env = {**os.environ, "PATH": str(tmp_path)}
    command = [
        sys.executable,
        "-P",
        str(Path(credential_tool.__file__)),
        "--distribution",
        "example-kit==1.0",
        "--entrypoint",
        "credential-helper",
        "--storage",
        str(tmp_path / "tools"),
        "--",
        "configure",
    ]
    with subprocess.Popen(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists():
                if time.monotonic() >= deadline or process.poll() is not None:
                    raise TimeoutError("installer did not start")
                time.sleep(0.01)
            pid = int(pid_file.read_text())
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=5) == 128 + signal.SIGTERM
            assert not psutil.pid_exists(pid)
        finally:
            if process.poll() is None:
                process.kill()
