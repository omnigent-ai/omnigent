"""Run the generated auth command with real SDK discovery and token exchange."""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from omnigent.inner.databricks_executor import databricks_bearer_token_command


@pytest.mark.posix_only
@pytest.mark.skipif(shutil.which("openssl") is None, reason="requires a local HTTPS certificate")
@pytest.mark.skipif(shutil.which("jq") is None, reason="the generated auth command requires jq")
@pytest.mark.parametrize("profile", ["cli", "service"])
def test_generated_command_uses_the_sdk_without_repeating_cli_auth(
    tmp_path: Path,
    profile: str,
) -> None:
    grants: list[dict[str, list[str]]] = []

    class Issuer(BaseHTTPRequestHandler):
        def reply(self, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/.well-known/databricks-config":
                self.reply({"oidc_endpoint": base + "/oidc"})
            elif self.path == "/oidc/.well-known/oauth-authorization-server":
                self.reply(
                    {
                        "issuer": base,
                        "authorization_endpoint": base + "/authorize",
                        "token_endpoint": base + "/oidc/token",
                    }
                )
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            assert self.path == "/oidc/token"
            grants.append(parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()))
            self.reply(
                {
                    "access_token": "fixture-service-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            )

        def log_message(self, format: str, *args: object) -> None:
            pass

    certificate, key = tmp_path / "issuer.crt", tmp_path / "issuer.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    issuer = ThreadingHTTPServer(("127.0.0.1", 0), Issuer)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    issuer.socket = context.wrap_socket(issuer.socket, server_side=True)
    base = f"https://127.0.0.1:{issuer.server_port}"
    thread = threading.Thread(target=issuer.serve_forever, daemon=True)
    thread.start()
    calls = tmp_path / "cli-calls"
    cli = tmp_path / "databricks"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "if sys.argv[1:] in (['-v'], ['--version']):\n"
        " print('Databricks CLI v0.999.0'); raise SystemExit(0)\n"
        "if '--help' in sys.argv:\n"
        " print('fixture CLI help'); raise SystemExit(0)\n"
        f"with pathlib.Path({str(calls)!r}).open('a') as out:\n"
        " out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "raise SystemExit(1)\n"
    )
    cli.chmod(0o700)
    config = tmp_path / "databrickscfg"
    config.write_text(
        f"[cli]\nhost = {base}\nauth_type = databricks-cli\ndatabricks_cli_path = {cli}\n"
        f"[service]\nhost = {base}\nauth_type = oauth-m2m\n"
        "client_id = fixture-client\nclient_secret = fixture-secret\n"
        "disable_async_token_refresh = true\n"
    )
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    env.update(
        {
            "PATH": os.pathsep.join(
                (str(tmp_path), str(Path(sys.executable).parent), env.get("PATH", ""))
            ),
            # The helper's python3 must import this worktree, not an installed copy.
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "DATABRICKS_CONFIG_FILE": str(config),
            "REQUESTS_CA_BUNDLE": str(certificate),
        }
    )
    try:
        result = subprocess.run(
            ["sh", "-c", databricks_bearer_token_command(base, profile)],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert calls.exists(), "the generated command never ran the fixture CLI"
        assert len(calls.read_text().splitlines()) == 1
        if profile == "service":
            assert result.stdout.strip() == "fixture-service-token", result.stderr
            assert len(grants) == 1
            assert grants[0]["grant_type"] == ["client_credentials"]
        else:
            assert result.stdout.strip() == ""
            assert grants == []
    finally:
        issuer.shutdown()
        issuer.server_close()
        thread.join(timeout=5)
