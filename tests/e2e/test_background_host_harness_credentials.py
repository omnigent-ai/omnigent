"""Explicit host credentials must survive foreground-to-background hosting."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
import yaml

from omnigent._platform import IS_WINDOWS, resolve_cli_binary
from omnigent.host.daemon_lifecycle import daemon_record_path
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from tests._helpers.host import isolated_host
from tests._helpers.live_server import (
    isolated_local_server,
    isolated_process_env,
    terminate_process,
)
from tests._helpers.polling import poll_until


@pytest.mark.skipif(IS_WINDOWS, reason="Requires POSIX host lifecycle support")
@pytest.mark.parametrize(
    ("harness", "credential_source"),
    [
        ("codex-native", "provider-reference"),
        ("codex-native", "named-passthrough"),
        ("claude-sdk", "named-passthrough"),
    ],
)
@pytest.mark.timeout(180)
def test_background_host_preserves_explicit_harness_credentials(
    tmp_path: Path, harness: str, credential_source: str
) -> None:
    """Compare real host advertisements without sending a model request."""
    if harness == "codex-native" and resolve_cli_binary("codex") is None:
        pytest.skip("Requires installed Codex CLI")
    if harness == "claude-sdk":
        pytest.importorskip("claude_agent_sdk")
    base_env = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "TMPDIR", "OMNIGENT_HARNESS_TMP_PARENT")
        if name in os.environ
    }
    credential_env_name = (
        "CLAUDE_CODE_OAUTH_TOKEN"
        if harness == "claude-sdk"
        else (
            "REPRO_GATEWAY_TOKEN"
            if credential_source == "provider-reference"
            else "OPENAI_REPRO_GATEWAY_TOKEN"
        )
    )
    config: dict[str, object] = {}
    if credential_source == "provider-reference":
        config["providers"] = {
            "repro-gateway": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "http://127.0.0.1:1",
                    "api_key_ref": f"env:{credential_env_name}",
                },
            }
        }

    def host_env(home: Path) -> dict[str, str]:
        codex_home = home / ".codex"
        codex_home.mkdir(parents=True)
        if harness == "codex-native" and credential_source == "named-passthrough":
            (codex_home / "config.toml").write_text(
                'model_provider = "gateway"\n[model_providers.gateway]\n'
                'name = "Repro gateway"\nbase_url = "http://127.0.0.1:1"\n'
                f'env_key = "{credential_env_name}"\nwire_api = "responses"\n'
            )
        env = {
            "CODEX_HOME": str(codex_home),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            credential_env_name: "synthetic-not-a-real-credential",
            "GEMINI_API_KEY": "unreferenced-synthetic-credential",
        }
        if credential_source == "named-passthrough":
            env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"] = credential_env_name
        return env

    isolation: list[str] = []
    if harness == "claude-sdk":
        if any(path.exists() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
            if not sys.platform.startswith("linux") or shutil.which("bwrap") is None:
                pytest.skip("Requires bubblewrap to isolate managed Claude authentication")
            isolation = [
                "bwrap",
                "--bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--tmpfs",
                "/etc/claude-code",
                "--",
            ]
        probe_home = tmp_path / "auth-probe-home"
        probe_home.mkdir()
        probe = subprocess.run(
            [
                *isolation,
                sys.executable,
                "-c",
                "from omnigent.onboarding.harness_readiness import _harness_availability; "
                "print(_harness_availability('claude-sdk'))",
            ],
            env=isolated_process_env(probe_home, {}, base_env),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if probe.returncode or probe.stdout.strip() != "needs-auth":
            pytest.skip("Cannot isolate machine-managed Claude authentication")

    prefix = [*isolation, sys.executable, "-m", "omnigent"]
    with (
        isolated_local_server(tmp_path / "server", base_env=base_env) as base_url,
        httpx.Client(base_url=base_url, trust_env=False, timeout=3) as client,
    ):
        command = [*prefix, "host", "--server", base_url, "--non-interactive", "--no-open"]

        def stop_host(home: Path, env: dict[str, str], log_path: Path) -> None:
            record = daemon_record_path(base_url, base_dir=home / ".omnigent")
            children: list[psutil.Process] = []
            if record.exists():
                log_path.with_suffix(".json").write_bytes(record.read_bytes())
                with suppress(psutil.NoSuchProcess):
                    children = psutil.Process(json.loads(record.read_text())["pid"]).children(
                        recursive=True
                    )
            try:
                stop = subprocess.run(
                    [*prefix, "host", "stop", "--server", base_url, "--force"],
                    env=env,
                    cwd=home,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            finally:
                for child in children:
                    with suppress(psutil.NoSuchProcess):
                        child.terminate()
                _, remaining = psutil.wait_procs(children, timeout=3)
                for child in remaining:
                    with suppress(psutil.NoSuchProcess):
                        child.kill()
                psutil.wait_procs(remaining, timeout=2)
            log_path.write_text(stop.stdout + stop.stderr)
            assert stop.returncode == 0, stop.stderr

        foreground_root = tmp_path / "foreground"
        foreground_env = host_env(foreground_root / "home")
        with isolated_host(
            foreground_root,
            base_url,
            config=config,
            env=foreground_env,
            base_env=base_env,
            command=command,
        ) as host:
            try:
                foreground = host.wait_online(client, timeout=60)
                (tmp_path / "foreground-host.json").write_text(json.dumps(foreground, indent=2))
                assert foreground["configured_harnesses"][harness] is True
                assert foreground["configured_harnesses"]["antigravity"] is True
            finally:
                stop_host(
                    host.home,
                    isolated_process_env(
                        host.home,
                        {**foreground_env, "OMNIGENT_CONFIG_HOME": str(host.home / ".omnigent")},
                        base_env,
                    ),
                    tmp_path / "foreground-stop.txt",
                )

        home = tmp_path / "background-home"
        overrides = host_env(home)
        config_dir = home / ".omnigent"
        config_dir.mkdir()
        host_id = uuid.uuid4().hex
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({**config, "host": {"host_id": host_id}})
        )
        env = isolated_process_env(
            home, {**overrides, "OMNIGENT_CONFIG_HOME": str(config_dir)}, base_env
        )
        process = subprocess.Popen(
            [*command, "--background"],
            env=env,
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            output, _ = process.communicate(timeout=75)
            (tmp_path / "background-cli.txt").write_text(f"exit={process.returncode}\n{output}")
            assert process.returncode == 0, output

            def online() -> dict[str, Any] | None:
                response = client.get("/v1/hosts")
                response.raise_for_status()
                return next(
                    (
                        row
                        for row in response.json()["hosts"]
                        if row["host_id"] == host_id and row["status"] == "online"
                    ),
                    None,
                )

            poll_until(online, timeout_s=30)
            background = online()
            assert background is not None
            (tmp_path / "background-host.json").write_text(json.dumps(background, indent=2))
            assert background["configured_harnesses"]["antigravity"] == "needs-auth"
            assert background["configured_harnesses"][harness] is True
        finally:
            terminate_process(process)
            stop_host(home, env, tmp_path / "background-stop.txt")
