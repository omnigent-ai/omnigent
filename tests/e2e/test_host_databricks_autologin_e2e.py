"""`omnigent host` auto-login against a Databricks workspace mount, end to end.

Drives the real CLI under a pseudo-TTY against a fake workspace edge (401
``DatabricksRealm`` challenge on the mount, ``Server: databricks`` web root,
account-acting ``/.well-known/databricks-config``) with a stub ``databricks``
CLI standing in for the browser login. Two behaviors are pinned: the token
verify inherits the workspace selector the CLI recorded during login, and a
genuine token rejection (HTTP 403) is not followed by the stale-host hint.

Usage::

    python -m pytest tests/e2e/test_host_databricks_autologin_e2e.py -v --timeout=300
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pexpect = pytest.importorskip("pexpect")
pytest.importorskip("databricks.sdk")

_WORKSPACE_ID = "1234567890123456"
_USER = "test-user@example.com"

# Seconds for the spawned CLI to finish the whole auto-login flow (server
# probes, stub browser login, SDK token resolution, verify round-trip).
_LOGIN_TIMEOUT_S = 120


class _FakeDatabricksEdge(http.server.BaseHTTPRequestHandler):
    """A Databricks workspace edge hosting omnigent at ``/api/2.0/omnigent``.

    ``mode`` is set per-server via the class attribute:

    - ``"no-o-403"``: an authenticated mount request succeeds only when it
      carries ``?o=<_WORKSPACE_ID>``; without the selector the account host
      rejects the workspace token with 403.
    - ``"always-403"``: the mount rejects every bearer with 403 (the user
      has no access to the app).
    """

    mode = "no-o-403"
    # The real edge identifies itself with ``Server: databricks``; the CLI's
    # workspace-URL expansion keys off that header, so replace BaseHTTP's
    # default server signature instead of sending a duplicate header.
    server_version = "databricks"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # keep pexpect transcripts clean

    def _json(
        self, code: int, body: dict[str, object], headers: dict[str, str] | None = None
    ) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        if split.path == "/.well-known/databricks-config":
            # Account-acting host: an account_id but no workspace_id.
            self._json(200, {"account_id": "acc-1965"})
            return
        if split.path == "/api/2.0/omnigent/v1/me":
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                self._json(
                    401,
                    {"error_code": 401, "message": "Credential was not sent"},
                    headers={"WWW-Authenticate": 'Bearer realm="DatabricksRealm"'},
                )
                return
            if self.mode == "always-403":
                self._json(403, {"error_code": "PERMISSION_DENIED", "message": "no app access"})
                return
            org = parse_qs(split.query).get("o", [None])[0]
            if org == _WORKSPACE_ID:
                self._json(200, {"user_id": _USER})
            else:
                # Unrouted request resolves to the account, which rejects
                # the workspace token.
                self._json(403, {"error_code": "PERMISSION_DENIED", "message": "account host"})
            return
        # Any other path: the workspace web app answers 404 with the
        # ``Server: databricks`` signature (what makes the CLI adopt the
        # ``/api/2.0/omnigent`` mount).
        self._json(404, {"error_code": "NOT_FOUND"})


@pytest.fixture(scope="module")
def edge_cert(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Self-signed localhost cert the fake edge serves TLS with."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")
    cert_dir = tmp_path_factory.mktemp("edge_cert")
    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(cert_dir / "key.pem"),
            "-out",
            str(cert_dir / "cert.pem"),
            "-days",
            "2",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"openssl cert generation failed: {result.stderr.decode()[:200]}")
    return cert_dir


def _serve_edge(cert_dir: Path, mode: str) -> tuple[http.server.ThreadingHTTPServer, str]:
    """Start the fake edge on an ephemeral HTTPS port; return (server, base URL)."""
    handler = type("Handler", (_FakeDatabricksEdge,), {"mode": mode})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_dir / "cert.pem", cert_dir / "key.pem")
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"https://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture()
def edge_no_o_403(edge_cert: Path) -> Iterator[str]:
    """Edge where the mount 403s unless the request carries ``?o=``."""
    httpd, url = _serve_edge(edge_cert, "no-o-403")
    yield url
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def edge_always_403(edge_cert: Path) -> Iterator[str]:
    """Edge where the mount rejects every bearer with 403."""
    httpd, url = _serve_edge(edge_cert, "always-403")
    yield url
    httpd.shutdown()
    httpd.server_close()


_STUB_DATABRICKS_CLI = """#!/usr/bin/env bash
# Stub Databricks CLI: `auth login --host H --profile P` simulates the real
# browser flow -- auto-selects the single accessible workspace, records its id
# in the profile, and caches a grant (a PAT-style token the SDK resolves).
set -u
if [ "${1:-}" = "auth" ] && [ "${2:-}" = "login" ]; then
  host=""; profile="DEFAULT"
  args=("$@"); i=0
  while [ $i -lt ${#args[@]} ]; do
    case "${args[$i]}" in
      --host) host="${args[$((i+1))]}"; i=$((i+2));;
      --profile) profile="${args[$((i+1))]}"; i=$((i+2));;
      *) i=$((i+1));;
    esac
  done
  cfg_host="${host%%\\?*}"
  echo "Auto-selected workspace \\"ai-devtools-prod\\" (__WORKSPACE_ID__)"
  echo "Profile $profile was successfully saved"
  cat > "${DATABRICKS_CONFIG_FILE:?}" <<EOF
[$profile]
host = $cfg_host
token = fake-workspace-token
workspace_id = __WORKSPACE_ID__
EOF
  exit 0
fi
echo "Error: not logged in" >&2
exit 1
"""


@pytest.fixture()
def host_env(edge_cert: Path, tmp_path: Path) -> dict[str, str]:
    """Env for the spawned CLI: logged-out state, stub `databricks` on PATH.

    - Fresh ``HOME`` / ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR`` so no
      prior login record, default server or runtime record leaks in.
    - Fresh ``DATABRICKS_CONFIG_FILE`` (no cached workspace grant), which the
      stub CLI populates when the auto-login invokes it.
    - The edge's self-signed cert as the trust root for httpx and the
      Databricks SDK.
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "databricks"
    stub.write_text(_STUB_DATABRICKS_CLI.replace("__WORKSPACE_ID__", _WORKSPACE_ID))
    stub.chmod(0o755)

    fake_home = tmp_path / "home"
    config_home = fake_home / ".omnigent"
    config_home.mkdir(parents=True)
    (config_home / "config.yaml").write_text("auto_open_conversation: false\n")

    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    # Ambient Databricks/Omnigent credentials must not change which auth path runs.
    for ambient in (
        "OMNIGENT_REMOTE_AUTH_TOKEN",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_BEARER",
    ):
        env.pop(ambient, None)
    # Runner/host vars from a server-spawned agent shell would make the
    # spawned CLI take the zygote-fork path; the user's machine has none.
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST")):
            env.pop(key, None)
    env.update(
        {
            "HOME": str(fake_home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(config_home),
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OMNIGENT_SKIP_ONBOARD": "1",
            "DATABRICKS_CONFIG_FILE": str(tmp_path / "databrickscfg"),
            "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
            "SSL_CERT_FILE": str(edge_cert / "cert.pem"),
            "REQUESTS_CA_BUNDLE": str(edge_cert / "cert.pem"),
            # Loopback must bypass any egress proxy the CI sandbox injects.
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            # Resolve omnigent + its SDKs from THIS worktree, not a sibling
            # editable install (mirrors tests/e2e/test_repl_approval_e2e.py).
            "PYTHONPATH": os.pathsep.join(
                [
                    str(repo_root),
                    str(repo_root / "sdks" / "python-client"),
                    str(repo_root / "sdks" / "ui"),
                ]
            ),
        }
    )
    return env


def _omnigent_cli() -> str:
    """Path to the omnigent CLI of the running venv (skip when absent)."""
    venv_omnigent = Path(sys.executable).parent / "omnigent"
    if venv_omnigent.exists():
        return str(venv_omnigent)
    path = shutil.which("omnigent")
    if path is None:
        pytest.skip("omnigent CLI not on PATH")
    return path


@pytest.mark.timeout(300)
def test_host_autologin_inherits_workspace_selector(
    edge_no_o_403: str, host_env: dict[str, str]
) -> None:
    """`omnigent host --server <ws>/omnigent?o=<id>` logs in unattended: the
    token verify routes with the workspace selector recorded during login."""
    child = pexpect.spawn(
        _omnigent_cli(),
        ["host", "--server", f"{edge_no_o_403}/omnigent?o={_WORKSPACE_ID}"],
        env=host_env,
        encoding="utf-8",
        timeout=_LOGIN_TIMEOUT_S,
    )
    try:
        idx = child.expect(
            [
                rf"Logged in as {_USER}",
                r"rejected the token \(HTTP \d+\)",
                pexpect.EOF,
            ]
        )
        transcript = (child.before or "") + (child.after if isinstance(child.after, str) else "")
        assert idx == 0, (
            "host auto-login did not complete: the token verify was rejected "
            "(the ?o= workspace selector was dropped) or the CLI "
            f"exited early.\nTranscript:\n{transcript}"
        )
    finally:
        child.terminate(force=True)


@pytest.mark.timeout(300)
def test_login_403_error_omits_stale_host_hint(
    edge_always_403: str, host_env: dict[str, str]
) -> None:
    """A login-time HTTP 403 is an access failure; the stale-host hint must not follow it."""
    child = pexpect.spawn(
        _omnigent_cli(),
        ["host", "--background", "--server", f"{edge_always_403}/omnigent?o={_WORKSPACE_ID}"],
        env=host_env,
        encoding="utf-8",
        timeout=_LOGIN_TIMEOUT_S,
    )
    try:
        child.expect(r"rejected the token \(HTTP 403\)")
        child.expect(pexpect.EOF)
        tail = child.before or ""
    finally:
        child.terminate(force=True)
    assert "runner tunnel rejection" not in tail, (
        "the login-403 error was followed by the misleading stale-host / "
        "HTTP 401 hint; a login-time 403 is an access failure, not a "
        f"runner tunnel rejection.\nOutput after the error:\n{tail}"
    )
