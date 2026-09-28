"""GitLab credentials for Git HTTPS and ``glab``.

The user's GitLab OAuth token is brokered on demand for git. Managed sandboxes
write glab's host-scoped config in their disposable home; local hosts use an
isolated Omnigent-owned config directory so the developer's glab login is never
modified.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import yaml

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

_TIMEOUT_S = 15.0
_GLAB_REFRESH_INTERVAL_ENV_VAR = "OMNIGENT_GLAB_REFRESH_INTERVAL_S"
_GLAB_REFRESH_DEFAULT_S = 1800


def _in_sandbox() -> bool:
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


def _credential_url(server: str, host_id: str) -> str:
    return f"{server.rstrip('/')}/v1/hosts/{host_id}/credentials/gitlab"


def _fetch(server: str, host_id: str, host_token: str) -> dict | None:
    try:
        response = httpx.get(
            _credential_url(server, host_id),
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
            timeout=_TIMEOUT_S,
        )
        data = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _git_config(*args: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", "config", "--global", *args],
            check=False,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )


def _git_helper_key(instance_url: str) -> str | None:
    parsed = urlsplit(instance_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        return None
    return f"credential.https://{parsed.netloc}.helper"


def _install_broker_helper(
    server_url: str, host_id: str, host_token: str, instance_url: str
) -> bool:
    helper_key = _git_helper_key(instance_url)
    if helper_key is None:
        return False
    helper = (
        "!python3 -m omnigent.git_credential_gitlab "
        f"--server {shlex.quote(server_url)} --host-id {shlex.quote(host_id)} "
        f"--host-token {shlex.quote(host_token)}"
    )
    # Scope the helper to the connected GitLab instance. This prevents an
    # ambient/shared helper from answering first for that host while preserving
    # helpers for GitHub and unrelated remotes.
    _git_config("--replace-all", helper_key, "")
    _git_config("--add", helper_key, helper)
    return True


def _prepare_glab_config_dir(host_id: str) -> None:
    """Give a local host a private glab config inherited by its runners."""
    if _in_sandbox():
        return
    data_dir = Path(os.environ.get("OMNIGENT_DATA_DIR") or Path.home() / ".omnigent")
    config_dir = data_dir / "hosts" / host_id / "glab-cli"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_dir, 0o700)
    os.environ["GLAB_CONFIG_DIR"] = str(config_dir)


def _glab_hosts_path() -> Path:
    base = (os.environ.get("GLAB_CONFIG_DIR") or "").strip()
    return (
        Path(base) / "hosts.yml"
        if base
        else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "glab-cli"
        / "hosts.yml"
    )


def _write_glab_hosts(host: str, login: str, token: str) -> bool:
    hostname = urlsplit(host).hostname
    if not hostname:
        return False
    path = _glab_hosts_path()
    hosts: dict = {}
    with contextlib.suppress(OSError, yaml.YAMLError):
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            hosts = loaded
    entry = hosts.get(hostname)
    if not isinstance(entry, dict):
        entry = {}
    entry.update({"token": token, "user": login, "git_protocol": "https"})
    hosts[hostname] = entry
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hosts.", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            yaml.safe_dump(hosts, output, default_flow_style=False, sort_keys=True)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        return True
    except (OSError, yaml.YAMLError):
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False


def configure_host_gitlab(server_url: str, host_id: str) -> bool:
    if not _in_sandbox() or not (host_token := (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()):
        return False
    data = _fetch(server_url, host_id, host_token)
    if data is not None and not data.get("connected"):
        return False
    if not data or not data.get("host"):
        return False
    return _install_broker_helper(server_url, host_id, host_token, str(data["host"]))


def configure_host_glab(server_url: str, host_id: str) -> bool:
    if not (host_token := (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()):
        return False
    _prepare_glab_config_dir(host_id)
    data = _fetch(server_url, host_id, host_token)
    if not data or not data.get("connected") or not data.get("token") or not data.get("host"):
        return False
    return _write_glab_hosts(
        str(data["host"]), str(data.get("login") or "oauth2"), str(data["token"])
    )


def _glab_refresh_interval_s() -> int:
    raw = (os.environ.get(_GLAB_REFRESH_INTERVAL_ENV_VAR) or "").strip()
    try:
        return int(raw) if raw else _GLAB_REFRESH_DEFAULT_S
    except ValueError:
        return _GLAB_REFRESH_DEFAULT_S


def start_host_glab_refresh(server_url: str, host_id: str) -> threading.Thread | None:
    interval = _glab_refresh_interval_s()
    if interval <= 0:
        return None

    def loop() -> None:
        while True:
            time.sleep(interval)
            with contextlib.suppress(Exception):
                configure_host_glab(server_url, host_id)

    thread = threading.Thread(target=loop, name="glab-token-refresh", daemon=True)
    thread.start()
    return thread


def _read_request() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--server", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--host-token", required=True)
    parser.add_argument("operation", nargs="?", default="get")
    args, _ = parser.parse_known_args(argv)
    if args.operation != "get":
        return 0
    request = _read_request()
    data = _fetch(args.server, args.host_id, args.host_token)
    configured_host = urlsplit(str((data or {}).get("host") or "")).netloc
    if (
        request.get("protocol") != "https"
        or not configured_host
        or request.get("host", "").lower() != configured_host.lower()
    ):
        return 0
    if not data or not data.get("connected") or not data.get("token"):
        return 0
    sys.stdout.write(f"username={data.get('username') or 'oauth2'}\npassword={data['token']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
