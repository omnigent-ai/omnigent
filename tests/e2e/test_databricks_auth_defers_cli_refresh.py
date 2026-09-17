"""
Eager Databricks SDK auth initialization bypasses centralized token refresh.

A runner that (re-)authenticates — e.g. after its host bootstrap bearer is
rejected — resolves Databricks credentials through
``omnigent.inner.databricks_executor._resolve_databricks_auth``. Constructing
the databricks-sdk ``Config`` there initializes SDK auth eagerly: the SDK's
``databricks-cli`` credential strategy launches a ``databricks auth token``
subprocess at construction time, before anything has asked the resolved
credential for a bearer. A central credential provider composed in front of
this resolver (managed hosts serialize token refreshes through one) therefore
cannot prevent independent, uncoordinated CLI refreshes against the shared
OAuth cache — and when the eager SDK auth step fails, resolution dies before a
healthy central provider could have served the request at all.

Regression contract (what the fix must make true):

- For both selectors (profile and host), resolving auth launches zero
  ``databricks auth token`` subprocesses until a request actually needs a
  bearer; the first authenticated request may then mint through the CLI.
- Resolving with a broken legacy CLI must not raise at resolve time, so a
  central-first consumer can still serve requests from its central provider.

The journey runs in a subprocess against the real resolver, the real
databricks-sdk, and a real (intercepted) ``databricks`` executable: a staged
HOME carries a synthetic ``databricks-cli`` profile, the intercepted CLI
appends every invocation to an order log and mints a synthetic token, and an
``httpx.AsyncClient`` drives an authenticated request against a loopback HTTP
stub. No live credentials, hosts, or network are touched.

Usage::

    python -m pytest tests/e2e/test_databricks_auth_defers_cli_refresh.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="bash CLI stub")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROFILE = "example"
_SYNTHETIC_HOST = "https://synthetic-workspace.cloud.databricks.com"
_SYNTHETIC_BEARER = "Bearer synthetic-cli-token"

_DRIVER_TIMEOUT_S = 120

# Records every invocation to the order log, then mints a synthetic token
# (or fails, when CLI_FAIL=1, to model a broken legacy refresh).
_CLI_STUB = """\
#!/usr/bin/env bash
printf 'sdk_subprocess: %s\\n' "$*" >> "$ORDER_LOG"
if [ "${CLI_FAIL:-0}" = "1" ]; then
  echo "Error: token refresh failed (synthetic)" >&2
  exit 1
fi
exp=$(date -u -d "+1 hour" +"%Y-%m-%dT%H:%M:%S")
printf '{"access_token": "synthetic-cli-token", "token_type": "Bearer", "expiry": "%s"}\\n' "$exp"
"""

_DRIVER = """\
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = sys.argv[1]
DO_REQUEST = sys.argv[2] == "request"
LOG = os.environ["ORDER_LOG"]


def read_log():
    with open(LOG) as f:
        return [line.rstrip("\\n") for line in f]


def cli_calls():
    return sum(1 for line in read_log() if line.startswith("sdk_subprocess"))


class Handler(BaseHTTPRequestHandler):
    auth_header = None

    def do_GET(self):
        type(self).auth_header = self.headers.get("Authorization")
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    from omnigent.inner.databricks_executor import (
        DatabricksAuthError,
        _resolve_databricks_auth,
    )

    open(LOG, "a").close()
    result = {
        "resolve_ok": False,
        "resolve_error": "",
        "cli_calls_at_resolve": 0,
        "cli_calls_total": 0,
        "status_code": None,
        "authorization": None,
        "order_log": [],
    }
    auth = None
    try:
        if MODE == "host":
            auth, _host = _resolve_databricks_auth(host=os.environ["SELECTOR_HOST"])
        else:
            auth, _host = _resolve_databricks_auth(os.environ["SELECTOR_PROFILE"])
        result["resolve_ok"] = True
    except DatabricksAuthError as exc:
        result["resolve_error"] = str(exc)
    result["cli_calls_at_resolve"] = cli_calls()

    if auth is not None and DO_REQUEST:
        import httpx

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/api/2.0/probe"

        async def probe():
            async with httpx.AsyncClient(auth=auth) as client:
                return await client.get(url)

        response = asyncio.run(probe())
        server.shutdown()
        result["status_code"] = response.status_code
        result["authorization"] = Handler.auth_header

    result["cli_calls_total"] = cli_calls()
    result["order_log"] = read_log()
    print(json.dumps(result))


main()
"""


@pytest.fixture
def staged(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    log_dir = tmp_path / "log"
    for directory in (home, bin_dir, log_dir):
        directory.mkdir()

    (home / ".databrickscfg").write_text(
        f"[{_PROFILE}]\nhost = {_SYNTHETIC_HOST}\nauth_type = databricks-cli\n"
    )

    cli = bin_dir / "databricks"
    # The SDK's CLI discovery rejects executables under 1MB (old-CLI
    # heuristic), so pad the stub with trailing comment bytes.
    pad = b"# " + b"x" * 78 + b"\n"
    stub = _CLI_STUB.encode()
    cli.write_bytes(stub + pad * ((1024 * 1024 + 4096 - len(stub)) // len(pad) + 1))
    cli.chmod(0o755)

    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)

    return {"home": home, "bin": bin_dir, "log": log_dir, "driver": driver}


def _run_journey(
    staged: dict[str, Path],
    mode: str,
    *,
    request: bool = True,
    cli_fail: bool = False,
) -> dict:
    order_log = staged["log"] / f"{mode}-{'fail' if cli_fail else 'ok'}.log"
    # A from-scratch environment: ambient DATABRICKS_*/proxy vars from the CI
    # host must not leak into the staged credential chain.
    env = {
        "HOME": str(staged["home"]),
        "PATH": f"{staged['bin']}:/usr/bin:/bin",
        "DATABRICKS_CONFIG_FILE": str(staged["home"] / ".databrickscfg"),
        "PYTHONPATH": str(_REPO_ROOT),
        "ORDER_LOG": str(order_log),
        "CLI_FAIL": "1" if cli_fail else "0",
        "SELECTOR_PROFILE": _PROFILE,
        "SELECTOR_HOST": _SYNTHETIC_HOST,
    }
    proc = subprocess.run(
        [sys.executable, str(staged["driver"]), mode, "request" if request else "no-request"],
        capture_output=True,
        text=True,
        timeout=_DRIVER_TIMEOUT_S,
        env=env,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, (
        f"driver exited {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.timeout(_DRIVER_TIMEOUT_S + 60)
def test_profile_selector_defers_cli_refresh_until_token_requested(
    staged: dict[str, Path],
) -> None:
    result = _run_journey(staged, "profile")
    assert result["cli_calls_at_resolve"] == 0, (
        "resolving Databricks auth (profile selector) launched a direct "
        "'databricks auth token' subprocess before any token was requested — "
        "a central credential provider cannot serialize this refresh:\n"
        + "\n".join(result["order_log"])
    )
    assert result["status_code"] == 200
    assert result["authorization"] == _SYNTHETIC_BEARER
    assert result["cli_calls_total"] >= 1


@pytest.mark.timeout(_DRIVER_TIMEOUT_S + 60)
def test_host_selector_defers_cli_refresh_until_token_requested(
    staged: dict[str, Path],
) -> None:
    result = _run_journey(staged, "host")
    assert result["cli_calls_at_resolve"] == 0, (
        "resolving Databricks auth (host selector) launched a direct "
        "'databricks auth token' subprocess before any token was requested — "
        "a central credential provider cannot serialize this refresh:\n"
        + "\n".join(result["order_log"])
    )
    assert result["status_code"] == 200
    assert result["authorization"] == _SYNTHETIC_BEARER
    assert result["cli_calls_total"] >= 1


@pytest.mark.timeout(_DRIVER_TIMEOUT_S + 60)
def test_broken_legacy_cli_does_not_block_resolution(staged: dict[str, Path]) -> None:
    result = _run_journey(staged, "profile", request=False, cli_fail=True)
    assert result["resolve_ok"], (
        "eager SDK auth failure killed credential resolution before a healthy "
        f"central provider could serve the request: {result['resolve_error']}\n"
        + "\n".join(result["order_log"])
    )
    assert result["cli_calls_at_resolve"] == 0, (
        "resolving Databricks auth ran the legacy CLI eagerly:\n" + "\n".join(result["order_log"])
    )
