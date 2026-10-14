"""Stand up a host page that embeds Omnigent and can switch between two Servers.

Two multi-user header-auth ``omnigent server`` processes run from this checkout.
A ``vite preview`` server serves the host page (``web/e2e-embed-host``, built into
the run's temp dir) and proxies ``/server-a`` and ``/server-b`` to them,
authenticating every proxied request as the host's signed-in user on that
Server — alice (an admin) on Server A, bob (not an admin) on Server B.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx

from tests._helpers.live_server import find_free_port, local_server_env, terminate_process

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIR = _REPO_ROOT / "web"
_VITE = _WEB_DIR / "node_modules" / ".bin" / "vite"
_HOST_VITE_CONFIG = "vite.e2e-embed-host.config.ts"
_READY_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.5

SERVER_A_USER = "alice@example.test"
SERVER_B_USER = "bob@example.test"
SERVER_A_STATUS = f"Server A — signed in as {SERVER_A_USER}"
SERVER_B_STATUS = f"Server B — signed in as {SERVER_B_USER}"


@dataclass(frozen=True)
class EmbedHost:
    """A running host page with the two Servers proxied behind it."""

    url: str


def build_host_page(dist: Path) -> None:
    """Build ``web/e2e-embed-host`` into *dist*, a directory private to this run."""
    if not _VITE.exists():
        raise RuntimeError(
            f"{_VITE} not found; run `pnpm install --frozen-lockfile --filter web` first."
        )
    subprocess.run(
        [str(_VITE), "build", "--config", _HOST_VITE_CONFIG],
        cwd=_WEB_DIR,
        check=True,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "OMNIGENT_E2E_HOST_DIST": str(dist),
            "OMNIGENT_E2E_SERVER_A_USER": SERVER_A_USER,
            "OMNIGENT_E2E_SERVER_B_USER": SERVER_B_USER,
        },
    )


def _wait_until_ready(
    proc: subprocess.Popen[bytes],
    log_path: Path,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    expect_text: str | None = None,
) -> httpx.Response:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last = "not polled"
    with httpx.Client(trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"{url}: process exited with code {proc.returncode}.\n"
                    f"Log tail:\n{log_path.read_text(errors='replace')[-4000:]}"
                )
            try:
                resp = client.get(url, headers=headers, timeout=2.0)
                if resp.status_code == 200 and (expect_text is None or expect_text in resp.text):
                    return resp
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(_POLL_INTERVAL_S)
    raise RuntimeError(
        f"{url} not ready within {_READY_TIMEOUT_S:.0f}s ({last}).\n"
        f"Log tail:\n{log_path.read_text(errors='replace')[-4000:]}"
    )


def _server_env(admin_list: Path) -> dict[str, str]:
    env = local_server_env({"OMNIGENT_ADMIN_LIST_PATH": str(admin_list)})
    # Multi-user: identity comes only from the gateway's trusted header.
    env.pop("OMNIGENT_LOCAL_SINGLE_USER", None)
    for name in list(env):
        if name.startswith(("OMNIGENT_RUNNER", "OMNIGENT_HOST_", "OMNIGENT_REPRO_")) or name in (
            "RUNNER_SERVER_URL",
            "OMNIGENT_REMOTE_AUTH_TOKEN",
        ):
            env.pop(name)
    return env


@contextmanager
def _omnigent_server(tmp_path: Path, name: str, user: str, admins: list[str]) -> Iterator[str]:
    server_dir = tmp_path / name
    server_dir.mkdir()
    admin_list = server_dir / "admins"
    admin_list.write_text("".join(f"{admin}\n" for admin in admins))
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = server_dir / "server.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{server_dir / 'chat.db'}",
                "--artifact-location",
                str(server_dir / "artifacts"),
            ],
            env=_server_env(admin_list),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            me = _wait_until_ready(
                proc, log_path, f"{base_url}/v1/me", headers={"X-Forwarded-Email": user}
            ).json()
            expected = {"user_id": user, "is_admin": user in admins}
            if not expected.items() <= me.items():
                raise RuntimeError(f"{name} /v1/me for {user} returned {me}, expected {expected}")
            yield base_url
        finally:
            terminate_process(proc)


@contextmanager
def _preview_server(
    tmp_path: Path, dist: Path, server_a_url: str, server_b_url: str
) -> Iterator[str]:
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "OMNIGENT_E2E_HOST_DIST": str(dist),
        "OMNIGENT_E2E_SERVER_A_URL": server_a_url,
        "OMNIGENT_E2E_SERVER_B_URL": server_b_url,
        "OMNIGENT_E2E_SERVER_A_USER": SERVER_A_USER,
        "OMNIGENT_E2E_SERVER_B_USER": SERVER_B_USER,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    log_path = tmp_path / "preview.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [
                str(_VITE),
                "preview",
                "--config",
                _HOST_VITE_CONFIG,
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--strictPort",
            ],
            cwd=_WEB_DIR,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_until_ready(proc, log_path, f"{base_url}/", expect_text="host-root")
            yield base_url
        finally:
            terminate_process(proc)


@contextmanager
def embed_host(tmp_path: Path) -> Iterator[EmbedHost]:
    """Run the host page with Server A (alice, admin) and Server B (bob) behind it."""
    dist = tmp_path / "host-dist"
    build_host_page(dist)
    with (
        _omnigent_server(tmp_path, "server-a", SERVER_A_USER, [SERVER_A_USER]) as server_a,
        _omnigent_server(tmp_path, "server-b", SERVER_B_USER, []) as server_b,
        _preview_server(tmp_path, dist, server_a, server_b) as url,
    ):
        yield EmbedHost(url=url)
