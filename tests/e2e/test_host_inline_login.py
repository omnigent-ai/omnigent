"""First-time host onboarding across the CLI, OAuth callback, and real host tunnel."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import pexpect
import pytest
from playwright.sync_api import sync_playwright

from tests._helpers.live_server import isolated_local_server, isolated_process_env
from tests._helpers.polling import poll_until
from tests.e2e._host_login_workspace import (
    ACCESS_TOKEN,
    MOUNT,
    USER_EMAIL,
    WORKSPACE_ID,
    HostLoginWorkspace,
)

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="browser opener uses Linux xdg-open"),
    pytest.mark.skipif(
        not shutil.which("openssl"),
        reason="openssl required for the local test CA",
    ),
    pytest.mark.timeout(180),
]

_CONNECTED = re.compile(r"Connected as '[^']+' \(([0-9a-f]{32})\)")


def test_login_rejects_truncated_workspace_selector(tmp_path: Path) -> None:
    """The real CLI checks an accepted bearer against the edge's workspace identity."""
    with (
        isolated_local_server(tmp_path / "server") as base_url,
        HostLoginWorkspace(tmp_path / "workspace", base_url) as workspace,
    ):
        home = tmp_path / "cli-home"
        data = home / ".omnigent"
        data.mkdir(parents=True)
        auth_file = data / "auth_tokens.json"
        auth_file.write_text(
            json.dumps(
                {
                    workspace.server_url: {
                        "auth_type": "databricks",
                        "workspace_host": workspace.url,
                        "org_id": WORKSPACE_ID,
                        "user_id": USER_EMAIL,
                    }
                }
            )
        )
        original_auth = auth_file.read_bytes()
        config_file = data / "config.yaml"
        config_file.write_text("server: http://127.0.0.1:1\n")
        original_config = config_file.read_bytes()
        profile_file = home / ".databrickscfg"
        profile_file.write_text(
            f"[local-test]\nhost = {workspace.url}\nauth_type = pat\ntoken = {ACCESS_TOKEN}\n"
        )
        env = isolated_process_env(
            home,
            {
                "OMNIGENT_CONFIG_HOME": str(data),
                "OMNIGENT_NO_UPDATE_CHECK": "1",
                "DATABRICKS_CONFIG_FILE": str(profile_file),
                "DATABRICKS_CONFIG_PROFILE": "local-test",
                "SSL_CERT_FILE": str(workspace.cert),
                "REQUESTS_CA_BUNDLE": str(workspace.cert),
            },
            None,
        )
        # Loopback probes disable trust_env (and hence SSL_CERT_FILE); provide
        # the test CA as the CLI process's default bundle, keeping TLS verified.
        cli_argv = [
            sys.executable,
            "-c",
            "import os, certifi\n"
            "certifi.where = lambda: os.environ['SSL_CERT_FILE']\n"
            "from omnigent.cli import main\n"
            "main()\n",
            "login",
        ]
        result = subprocess.run(
            [*cli_argv, f"{workspace.server_url}?o=123"],
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = result.stdout + result.stderr
        (tmp_path / "mismatched-login.txt").write_text(output)
        assert f"{MOUNT}/v1/me?o=123" in workspace.authenticated_requests
        assert result.returncode != 0, output
        assert "Requested workspace 123," in output
        assert f"reports workspace {WORKSPACE_ID}." in output
        assert "truncated" in output
        assert "omnigent stop" not in output
        assert auth_file.read_bytes() == original_auth
        assert config_file.read_bytes() == original_config

        retry = subprocess.run(
            [*cli_argv, f"{workspace.server_url}?o={WORKSPACE_ID}"],
            env=env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=60,
        )
        (tmp_path / "corrected-login.txt").write_text(retry.stdout + retry.stderr)
        assert retry.returncode == 0, retry.stdout + retry.stderr
        assert f"Logged in as {USER_EMAIL}" in retry.stdout
        assert json.loads(auth_file.read_text())[workspace.server_url]["org_id"] == WORKSPACE_ID
        assert workspace.authorizations == 0


@pytest.mark.skipif(not shutil.which("databricks"), reason="real Databricks CLI required")
def test_first_host_login_registers_and_reuses_credentials(tmp_path: Path) -> None:
    """Missing CLI gives an install hint; retry signs in, registers, and reuses its login."""
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).exists():
            pytest.skip("install this checkout's Playwright Chromium")
        with (
            isolated_local_server(tmp_path / "server") as base_url,
            HostLoginWorkspace(tmp_path / "workspace", base_url) as workspace,
            httpx.Client(base_url=base_url, headers={"X-Forwarded-Email": USER_EMAIL}) as client,
        ):
            home = tmp_path / "host-home"
            home.mkdir()
            browser_url = tmp_path / "browser-url.txt"
            browser_bin = tmp_path / "bin"
            browser_bin.mkdir()
            opener = browser_bin / "xdg-open"
            opener.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$HOST_LOGIN_BROWSER_URL"\n')
            opener.chmod(0o755)
            env = isolated_process_env(
                home,
                {
                    "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
                    "OMNIGENT_NO_UPDATE_CHECK": "1",
                    "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
                    "SSL_CERT_FILE": str(workspace.cert),
                    "REQUESTS_CA_BUNDLE": str(workspace.cert),
                    "PATH": str(browser_bin) + os.pathsep + os.environ["PATH"],
                    "HOST_LOGIN_BROWSER_URL": str(browser_url),
                    "TERM": "xterm",
                    "BROWSER": None,
                    "OPENAI_API_KEY": None,
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                },
                None,
            )
            host_id = None
            for attempt in ("missing-cli", "first-login", "saved-login"):
                with (tmp_path / f"{attempt}.txt").open("w") as transcript:
                    child = pexpect.spawn(
                        sys.executable,
                        ["-m", "omnigent", "host", workspace.server_url, "--no-open"],
                        env={**env, "PATH": str(browser_bin)} if attempt == "missing-cli" else env,
                        cwd=str(home),
                        encoding="utf-8",
                        timeout=60,
                        dimensions=(40, 80),
                    )
                    child.logfile_read = transcript
                    try:
                        if attempt == "missing-cli":
                            child.expect("Databricks CLI not found on PATH")
                            child.expect("Install it, then retry this command.")
                            child.expect("https://docs.databricks.com/dev-tools/cli/install")
                            child.expect(pexpect.EOF)
                            assert child.wait() != 0
                            transcript.flush()
                            assert "omnigent stop" not in Path(transcript.name).read_text()
                            assert not browser_url.exists()
                            assert workspace.authorizations == 0
                            assert not (home / ".omnigent/auth_tokens.json").exists()
                            assert client.get("/v1/hosts").json()["hosts"] == []
                            continue
                        if attempt == "first-login":
                            child.expect("Opening browser to log in")
                            poll_until(browser_url.exists, timeout_s=30, interval_s=0.1)
                            browser = playwright.chromium.launch()
                            try:
                                page = browser.new_page(ignore_https_errors=True)
                                page.goto(browser_url.read_text().strip(), timeout=30_000)
                            finally:
                                browser.close()
                            child.expect(f"Logged in as {USER_EMAIL}")
                        child.expect(_CONNECTED)
                        connected_id = child.match.group(1)
                        assert host_id is None or connected_id == host_id
                        host_id = connected_id

                        def registered_online(
                            expected_id: str = host_id,
                            evidence_path: Path = tmp_path / f"{attempt}-hosts.json",
                        ) -> bool:
                            response = client.get("/v1/hosts")
                            response.raise_for_status()
                            hosts = response.json()["hosts"]
                            evidence_path.write_text(json.dumps(hosts))
                            return any(
                                host["host_id"] == expected_id
                                and host["status"] == "online"
                                and host["owner"] == USER_EMAIL
                                for host in hosts
                            )

                        poll_until(registered_online, timeout_s=15, interval_s=0.1)
                        auth = json.loads((home / ".omnigent/auth_tokens.json").read_text())
                        assert auth[workspace.server_url]["auth_type"] == "databricks"
                        assert auth[workspace.server_url]["user_id"] == USER_EMAIL
                        assert workspace.authorizations == 1
                        assert workspace.token_exchanges >= 1
                    finally:
                        with contextlib.suppress(OSError):
                            child.kill(signal.SIGINT)
                        with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
                            child.expect(pexpect.EOF, timeout=15)
                        child.close(force=True)
                if attempt == "saved-login":
                    assert "Opening browser to log in" not in Path(transcript.name).read_text()
