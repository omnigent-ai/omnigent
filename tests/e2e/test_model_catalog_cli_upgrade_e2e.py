"""E2E: a harness CLI upgrade must refresh the shared model catalog.

Omnigent caches each native harness's model catalog on disk, keyed by a
launch-config fingerprint (``omnigent/model_catalog_store.py``). The key
covers the launch config only — not the binary that answered the probe — so
upgrading the CLI keeps the same key, and every consumer (the pre-launch
picker, launch resolution, the session gear) serves the PREVIOUS build's
model names until the 1h staleness TTL passes.

The journey, per harness, exactly as a user hits it:

1. Omnigent runs with the harness CLI at build A; the host boots and its
   boot probe fills the catalog — the picker shows build A's model names.
2. The CLI auto-updates **in place** to build B (same path, new binary).
3. The user reopens the app — the host restarts and serves the picker
   again. Expected: the picker lists build B's models. Bug: it still lists
   build A's.

Hermetic: the "CLI" is a fake claude / codex app-server whose model names
carry a build marker (``OLD`` / ``NEW``), so no real harness CLI, network,
or credentials are needed — only this repo's own server and host daemon.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="boots POSIX server/host daemons with fake CLIs"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 90.0
#: First (cold) catalog fill: one probe run against the fake CLI.
_CATALOG_TIMEOUT_S = 90.0
#: How long the upgraded CLI's models get to appear after the host restart.
#: On fixed code the changed fingerprint is a store miss and the re-probe
#: answers in a few seconds; on buggy code the old entry serves forever
#: (within its 1h TTL), so this wait expiring IS the reproduction.
_REFRESH_TIMEOUT_S = 45.0

_FAKE_CLAUDE_TEMPLATE = """#!/usr/bin/env python3
'''Fake Claude Code build {version}: answers the headless /model probes.'''
import json
import sys

ALIASES = {{
    "sonnet": ("{sonnet_model}", "{sonnet_label}"),
    "opus": ("{opus_model}", "{opus_label}"),
}}

args = sys.argv[1:]
if "--version" in args:
    print("{version}")
    raise SystemExit(0)
model = args[args.index("--model") + 1] if "--model" in args else None
alias = model if model in ALIASES else "sonnet"
mid, label = ALIASES[alias]
print(json.dumps({{"type": "system", "subtype": "init", "model": mid}}))
print(json.dumps({{"type": "result", "result":
    "Usage: /model <name>. Available: sonnet, opus, default, "
    "or a full model ID.\\nCurrent model: " + label}}))
"""

_FAKE_CODEX_TEMPLATE = """#!{python}
'''Fake Codex CLI build {marker}: an app-server with a fixed model/list.'''
import asyncio
import json
import sys

MODELS = [
    {{"id": "gpt-6-codex", "model": "gpt-6-codex",
      "displayName": "GPT-6 Codex {marker}", "isDefault": True}},
    {{"id": "gpt-6-mini", "model": "gpt-6-mini",
      "displayName": "GPT-6 Mini {marker}"}},
]


async def _serve(listen_url):
    import websockets

    host, _, port = listen_url.removeprefix("ws://").partition(":")

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if "id" not in msg:
                continue  # notification (e.g. "initialized")
            method = msg.get("method")
            if method == "initialize":
                result = {{"serverInfo": {{"name": "fake-codex",
                                           "version": "{marker}"}}}}
            elif method == "model/list":
                result = {{"data": MODELS, "nextCursor": None}}
            else:
                result = {{}}
            await ws.send(json.dumps({{"id": msg["id"], "result": result}}))

    async with websockets.serve(handler, host, int(port)):
        await asyncio.Future()


args = sys.argv[1:]
if "--version" in args:
    print("codex-cli 0.0.0+{marker}")
elif args and args[0] == "app-server" and "--listen" in args:
    asyncio.run(_serve(args[args.index("--listen") + 1]))
else:
    sys.exit(2)
"""


def _write_fake_claude(path: Path, *, version: str, marker: str, gen: int) -> None:
    """(Re)write the fake ``claude`` binary in place, like an auto-update.

    :param path: The installed CLI path (constant across builds).
    :param version: The build's ``--version`` answer, e.g. ``"2.1.247"``.
    :param marker: Build marker carried in every model name (``OLD``/``NEW``).
    :param gen: Model generation this build ships, e.g. ``5``.
    """
    path.write_text(
        _FAKE_CLAUDE_TEMPLATE.format(
            version=version,
            sonnet_model=f"claude-sonnet-{gen}-20250929",
            sonnet_label=f"Sonnet {gen} {marker}",
            opus_model=f"claude-opus-{gen}",
            opus_label=f"Opus {gen} {marker}",
        )
    )
    path.chmod(0o755)


def _write_fake_codex(path: Path, *, marker: str) -> None:
    """(Re)write the fake ``codex`` binary in place, like an auto-update.

    :param path: The installed CLI path (constant across builds).
    :param marker: Build marker carried in every model name (``OLD``/``NEW``).
    """
    path.write_text(_FAKE_CODEX_TEMPLATE.format(python=sys.executable, marker=marker))
    path.chmod(0o755)


def _free_port() -> int:
    """Return an ephemeral loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _CatalogRig:
    """A sandboxed ``omnigent server`` + restartable ``omnigent host``.

    Isolated ``HOME`` / ``OMNIGENT_CONFIG_HOME`` / ``OMNIGENT_DATA_DIR`` and
    a ``PATH`` whose only harness CLI is the fake under test, so the host's
    boot probe and the shared on-disk catalog see nothing but this rig.
    """

    def __init__(self, root: Path, bin_dir: Path, extra_env: dict[str, str]) -> None:
        self.root = root
        self.bin_dir = bin_dir
        self.extra_env = extra_env
        self.base_url = ""
        self.host_id = ""
        self._server: subprocess.Popen[bytes] | None = None
        self._host: subprocess.Popen[bytes] | None = None
        self._client = httpx.Client(trust_env=False, timeout=10.0)

    def _env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            # An omnigent-managed test session leaks runner/host identity;
            # inherited, it would point the rig's daemons at the WRONG server.
            if not key.startswith(("OMNIGENT", "CLAUDECODE", "RUNNER_SERVER_URL"))
        }
        env.update(
            {
                "HOME": str(self.root / "home"),
                "PATH": f"{self.bin_dir}:/usr/bin:/bin",
                "OMNIGENT_CONFIG_HOME": str(self.root / "config-home"),
                "OMNIGENT_DATA_DIR": str(self.root / "data"),
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(_REPO_ROOT),
                        str(_REPO_ROOT / "sdks" / "python-client"),
                        str(_REPO_ROOT / "sdks" / "ui"),
                    ]
                ),
            }
        )
        env.update(self.extra_env)
        return env

    def start_server(self) -> None:
        """Boot the rig server and wait for ``/health``."""
        port = _free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        for sub in ("home", "config-home", "data"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        log = open(self.root / "server.log", "w")  # noqa: SIM115 — subprocess lifetime
        self._server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{self.root / 'rig.db'}",
                "--artifact-location",
                str(self.root / "artifacts"),
            ],
            env=self._env(),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self._wait(
            lambda: self._get_ok(f"{self.base_url}/health"),
            timeout=_SERVER_HEALTH_TIMEOUT_S,
            what="the rig server /health",
            proc=self._server,
            log_path=self.root / "server.log",
        )

    def start_host(self) -> None:
        """Boot (or reboot) the rig host daemon and wait for it to register."""
        self.stop_host()
        log = open(self.root / "host.log", "a")  # noqa: SIM115 — subprocess lifetime
        self._host = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                self.base_url,
            ],
            env=self._env(),
            stdout=subprocess.DEVNULL,
            stderr=log,
        )

        def _online() -> bool:
            hosts = self._get_json(f"{self.base_url}/v1/hosts").get("hosts", [])
            online = [h for h in hosts if h.get("status") == "online"]
            if online:
                self.host_id = str(online[0]["host_id"])
                return True
            return False

        self._wait(
            _online,
            timeout=_HOST_ONLINE_TIMEOUT_S,
            what="the rig host to register",
            proc=self._host,
            log_path=self.root / "host.log",
        )

    def model_display_names(self, harness: str, *, timeout: float) -> list[str]:
        """Poll the pre-launch picker rows until non-empty, or return ``[]``.

        This is the endpoint the SPA's landing screen (and session gear)
        reads — the user-facing answer under test.

        :param harness: ``"claude-native"`` or ``"codex-native"``.
        :param timeout: Max seconds to poll for a non-empty catalog.
        :returns: The rows' display names in catalog order.
        """
        url = f"{self.base_url}/v1/hosts/{self.host_id}/harnesses/{harness}/model-options"
        deadline = time.monotonic() + timeout
        names: list[str] = []
        while time.monotonic() < deadline:
            payload = self._get_json(url)
            names = [
                str(row.get("displayName") or row.get("id")) for row in payload.get("models") or []
            ]
            if names:
                return names
            time.sleep(1.0)
        return names

    def stop_host(self) -> None:
        """Stop the host daemon, if one is running."""
        self._terminate(self._host)
        self._host = None

    def stop(self) -> None:
        """Tear the whole rig down."""
        self.stop_host()
        self._terminate(self._server)
        self._server = None
        self._client.close()

    def _get_ok(self, url: str) -> bool:
        try:
            return self._client.get(url).status_code == 200
        except httpx.HTTPError:
            return False

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self._client.get(url)
        except httpx.HTTPError:
            return {}
        if response.status_code != 200:
            return {}
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _wait(
        self,
        predicate: Any,
        *,
        timeout: float,
        what: str,
        proc: subprocess.Popen[bytes],
        log_path: Path,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log_path.read_text(errors="replace")[-3000:] if log_path.exists() else ""
                raise RuntimeError(f"{what}: process exited early; log tail:\n{tail}")
            if predicate():
                return
            time.sleep(1.0)
        tail = log_path.read_text(errors="replace")[-3000:] if log_path.exists() else ""
        raise RuntimeError(f"timed out waiting for {what}; log tail:\n{tail}")

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@contextmanager
def _booted_rig(root: Path, bin_dir: Path, extra_env: dict[str, str]) -> Iterator[_CatalogRig]:
    """A rig with its server booted; the caller starts/restarts the host.

    :param root: Sandbox directory (home, config, data, logs, db).
    :param bin_dir: Directory holding the fake harness CLI.
    :param extra_env: Extra env for the daemons, e.g. ``OMNIGENT_CODEX_PATH``.
    :yields: The booted rig.
    """
    rig = _CatalogRig(root, bin_dir, extra_env)
    try:
        rig.start_server()
        yield rig
    finally:
        rig.stop()


def _assert_upgrade_refreshed(before: list[str], after: list[str], harness: str) -> None:
    """Assert the post-upgrade picker serves the new build's models.

    :param before: Display names served while build A (``OLD``) was installed.
    :param after: Display names served after the in-place upgrade to build B.
    :param harness: Harness name, for the failure message.
    """
    assert before and all("OLD" in name for name in before), (
        f"setup: the pre-upgrade {harness} catalog should list build A's models, got {before!r}"
    )
    assert after, f"the post-upgrade {harness} picker served no models at all"
    assert any("NEW" in name for name in after), (
        f"a {harness} CLI upgrade did not refresh the model catalog: the "
        f"picker still serves the previous build's models {after!r} (the "
        f"launch-config fingerprint ignores the binary, so the upgraded CLI "
        f"hits the old cache entry until the 1h TTL)"
    )


@pytest.mark.timeout(400)
def test_claude_cli_upgrade_refreshes_model_catalog(tmp_path: Path) -> None:
    """A Claude Code upgrade must re-probe the claude-native model catalog.

    Journey: host boots with claude 2.1.247 (models named ``… OLD``) and the
    boot probe fills the catalog → claude auto-updates in place to 2.1.250
    (models named ``… NEW``) → the user reopens the app (host restarts) →
    the pre-launch picker must list the NEW models, not the cached OLD ones.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    _write_fake_claude(claude, version="2.1.247", marker="OLD", gen=5)

    with _booted_rig(tmp_path / "rig", bin_dir, {}) as rig:
        rig.start_host()
        before = rig.model_display_names("claude-native", timeout=_CATALOG_TIMEOUT_S)

        # The auto-updater replaces the binary at the same path.
        _write_fake_claude(claude, version="2.1.250", marker="NEW", gen=6)

        rig.start_host()  # the user reopens the app
        after = rig.model_display_names("claude-native", timeout=_REFRESH_TIMEOUT_S)
        deadline = time.monotonic() + _REFRESH_TIMEOUT_S
        while after and not any("NEW" in name for name in after) and time.monotonic() < deadline:
            time.sleep(2.0)
            after = rig.model_display_names("claude-native", timeout=5.0)

    _assert_upgrade_refreshed(before, after, "claude-native")


@pytest.mark.timeout(400)
def test_codex_cli_upgrade_refreshes_model_catalog(tmp_path: Path) -> None:
    """A Codex CLI upgrade must re-probe the codex-native model catalog.

    Same journey as the claude twin — ``codex_catalog_fingerprint`` keys on
    profile/model/config overrides only, so it has the identical gap.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    _write_fake_codex(codex, marker="OLD")

    with _booted_rig(tmp_path / "rig", bin_dir, {"OMNIGENT_CODEX_PATH": str(codex)}) as rig:
        rig.start_host()
        before = rig.model_display_names("codex-native", timeout=_CATALOG_TIMEOUT_S)

        # The auto-updater replaces the binary at the same path.
        _write_fake_codex(codex, marker="NEW")

        rig.start_host()  # the user reopens the app
        after = rig.model_display_names("codex-native", timeout=_REFRESH_TIMEOUT_S)
        deadline = time.monotonic() + _REFRESH_TIMEOUT_S
        while after and not any("NEW" in name for name in after) and time.monotonic() < deadline:
            time.sleep(2.0)
            after = rig.model_display_names("codex-native", timeout=5.0)

    _assert_upgrade_refreshed(before, after, "codex-native")
