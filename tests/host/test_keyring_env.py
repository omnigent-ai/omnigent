"""Pinned keyring selection survives daemon and runner process boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.cli import _build_host_daemon_env
from omnigent.host.connect import _build_runner_env
from omnigent.inner.os_env import build_helper_env
from omnigent.inner.sandbox import SandboxPolicy

_BACKEND_SOURCE = """\
import json
import os
from pathlib import Path

from keyring.backend import KeyringBackend


class FileKeyring(KeyringBackend):
    priority = 1

    @property
    def path(self):
        return Path(os.environ['OMNIGENT_CONFIG_HOME']) / 'test-keyring.json'

    def get_password(self, service, username):
        return json.loads(self.path.read_text()).get(service, {}).get(username)

    def set_password(self, service, username, password):
        self.path.write_text(json.dumps({service: {username: password}}))
"""


@pytest.mark.parametrize("server_url", [None, "https://server.example.test"])
def test_pinned_keyring_secret_resolves_in_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server_url: str | None
) -> None:
    (tmp_path / "test_keyring_backend.py").write_text(_BACKEND_SOURCE)
    project = Path(__file__).resolve().parents[2]
    source_paths = (tmp_path, project, project / "sdks/python-client", project / "sdks/ui")
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "OMNIGENT_CONFIG_HOME": str(tmp_path),
        "PYTHONPATH": os.pathsep.join(map(str, source_paths)),
        "PYTHON_KEYRING_BACKEND": "test_keyring_backend.FileKeyring",
    }
    stored = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omnigent.onboarding.secrets import store_secret; "
            "store_secret('test-provider', 'dummy-provider-key')",
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert stored.returncode == 0, stored.stderr
    assert not (tmp_path / "secrets.json").exists()

    monkeypatch.setattr(os, "environ", env)
    runner_env = _build_runner_env(
        _build_host_daemon_env(server_url=server_url),
        server_url=server_url or "http://localhost:6767",
        runner_id="test-keyring",
        binding_token="test-binding-token",
        workspace=str(tmp_path),
        parent_pid=os.getpid(),
    )
    resolved = subprocess.run(
        [
            sys.executable,
            "-c",
            "import keyring.core; "
            "assert keyring.core.load_env() is not None, 'missing keyring selector'; "
            "from omnigent.onboarding.provider_config import resolve_secret; "
            "assert resolve_secret('keychain:test-provider') == 'dummy-provider-key'",
        ],
        env=runner_env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert resolved.returncode == 0, resolved.stderr


def test_keyring_selector_excluded_from_default_sandbox_helper() -> None:
    env = {"PYTHON_KEYRING_BACKEND": "test_keyring_backend.FileKeyring"}
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=False,
    )
    assert "PYTHON_KEYRING_BACKEND" not in build_helper_env(env, policy)
