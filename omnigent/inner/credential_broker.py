"""Parent-side broker for non-HTTP credentialed tools.

Real secrets are resolved in the trusted parent — loaded once at session start
(the "unlock"), or per-call via a fallback source — and reach a single tool
invocation over an ``AF_UNIX`` socket in the helper's bound scratch dir,
authenticated by a per-handle token. Nothing credential-shaped sits in the
agent's ambient environment. Values are never logged.

See ``docs/superpowers/plans/2026-06-26-non-http-credential-broker.md``.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import secrets
import shutil
import socket
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .credential_proxy import _resolve_secret
from .datamodel import CredentialBrokerLoadSource, CredentialBrokerSpec

logger = logging.getLogger(__name__)


def _load_store(
    load_sources: list[CredentialBrokerLoadSource], *, parent_env: dict[str, str]
) -> dict[str, str]:
    """Resolve the load-at-unlock sources into an in-memory key→value store.

    ``file`` sources read ``KEY=VALUE`` lines (``#`` comments and blanks
    skipped). ``env`` sources lift named variables from *parent_env*. Values
    are held only in the parent process; never written back to disk.
    """
    store: dict[str, str] = {}
    for src in load_sources:
        if src.from_ == "file":
            if not src.path:
                raise ValueError("credential_broker load file source requires 'path'")
            p = Path(os.path.expanduser(src.path))
            if not p.is_file():
                raise ValueError(f"credential_broker load file not found: {p}")
            if p.stat().st_mode & 0o077:
                logger.warning(
                    "credential_broker load file %s is group/other-accessible (want 0600)", p
                )
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                store[k.strip()] = v.strip()
        elif src.from_ == "env":
            for name in src.names:
                val = parent_env.get(name)
                if val is not None:
                    store[name] = val
    return store


def _resolve_tool_env(
    spec: CredentialBrokerSpec,
    tool_name: str,
    store: dict[str, str],
    *,
    command_env: dict[str, str],
) -> dict[str, str]:
    """Resolve the env a brokered tool receives: store → fallback → optional-skip.

    ``command`` fallbacks run in the parent against *command_env* (a filtered
    baseline, never the full ambient env) so a fallback command can't enumerate
    the parent's secret-bearing environment.
    """
    tool = spec.tools[tool_name]
    out: dict[str, str] = {}
    for gname in tool.credentials:
        for field in spec.groups[gname].fields:
            val = store.get(field.key or field.env)
            if val is None and field.fallback is not None:
                try:
                    val = _resolve_secret(field.fallback, parent_env=command_env)
                except ValueError:
                    val = None
            if val is None:
                if field.optional:
                    continue
                raise ValueError(
                    f"required field {field.env} for tool {tool_name!r} could not be resolved"
                )
            out[field.env] = val
    return out


def _write_shims(tool_names: list[str], shim_dir: Path, *, socket_path: Path) -> Path:
    """Write a PATH shim per tool plus a self-contained copy of the client.

    The client is copied (not referenced via ``-m omnigent...``) so it needs
    only ``sys.executable`` reachable inside the sandbox, not the omnigent
    package tree.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    import omnigent.inner.cred_broker_client as _client_mod

    client_dst = shim_dir / "cred_broker_client.py"
    client_dst.write_text(Path(_client_mod.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    py = sys.executable
    for name in tool_names:
        shim = shim_dir / name
        shim.write_text(
            "#!/bin/bash\n"
            f'exec "{py}" "{client_dst}" --socket "{socket_path}" --tool "{name}" -- "$@"\n'
        )
        shim.chmod(0o755)
    return shim_dir


def _recv_line(conn: socket.socket) -> str:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8")


def _peer_uid(conn: socket.socket) -> int | None:
    """Return the connecting peer's uid, or ``None`` if it can't be determined.

    The parent broker lives outside the helper's PID namespace, so the kernel
    reports the peer's host-side uid here. ``None`` (unknown platform / failed
    readout) falls back to the socket's 0600-in-0700 filesystem permission.
    """
    if sys.platform.startswith("linux"):
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    if sys.platform == "darwin":
        sol_local, local_peercred = 0, 0x001  # <sys/un.h>; xucred: u_int version, u_int uid, ...
        try:
            raw = conn.getsockopt(sol_local, local_peercred, 8)
            _version, uid = struct.unpack("II", raw[:8])
            return uid
        except OSError:
            return None
    return None


class _BrokerServer:
    """Threaded AF_UNIX server that hands a tool's resolved creds to the shim."""

    def __init__(
        self,
        *,
        spec: CredentialBrokerSpec,
        store: dict[str, str],
        parent_env: dict[str, str],
        command_env: dict[str, str],
        socket_path: Path,
        auth_token: str,
    ) -> None:
        self._spec = spec
        self._store = store
        self._parent_env = parent_env
        self._command_env = command_env
        self.socket_path = socket_path
        self._auth_token = auth_token
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        old_umask = os.umask(0o077)
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(str(self.socket_path))
        finally:
            os.umask(old_umask)
        os.chmod(self.socket_path, 0o600)
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with conn:
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        try:
            if _peer_uid(conn) not in (None, os.getuid()):
                conn.sendall(b'{"error":"denied"}\n')
                return
            req = json.loads(_recv_line(conn))
            if not hmac.compare_digest(str(req.get("token", "")), self._auth_token):
                logger.warning("credential_broker: rejected request with bad token")
                conn.sendall(b'{"error":"denied"}\n')
                return
            tool = req.get("tool")
            if tool not in self._spec.tools:
                conn.sendall(b'{"error":"unknown tool"}\n')
                return
            env = _resolve_tool_env(self._spec, tool, self._store, command_env=self._command_env)
            binary = (
                self._spec.tools[tool].binary
                or shutil.which(tool, path=self._parent_env.get("PATH", os.defpath))
                or tool
            )
            # Log names only — never values.
            logger.info("credential_broker: served tool=%s keys=%s", tool, sorted(env))
            conn.sendall((json.dumps({"env": env, "binary": binary}) + "\n").encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — never leak a value-bearing message into the sandbox
            logger.warning("credential_broker: resolution failed: %s", exc)
            conn.sendall(b'{"error":"resolution failed"}\n')

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with contextlib.suppress(OSError):
            self.socket_path.unlink()


@dataclass
class CredentialBrokerRuntime:
    """Prepared broker assets for one helper/terminal handle."""

    shim_dir: Path
    socket_path: Path
    auth_token: str
    _server: _BrokerServer

    def stop(self) -> None:
        self._server.stop()
        shutil.rmtree(self.shim_dir, ignore_errors=True)


def prepare_credential_broker_runtime(
    spec: CredentialBrokerSpec | None,
    *,
    parent_env: dict[str, str],
    command_env: dict[str, str],
    scratch_dir: Path,
) -> CredentialBrokerRuntime | None:
    """Load the store, start the broker socket, and write the PATH shims.

    :param parent_env: Full parent env — used for ``load: env`` named reads and
        resolving each tool's real binary via ``PATH``.
    :param command_env: Filtered env (e.g. ``build_helper_env`` output) used to
        run ``command`` fallback sources, so they can't see the parent's
        secret-bearing environment.
    :param scratch_dir: The helper's bound scratch directory; the socket and
        shim dir live here so they are reachable inside the sandbox.
    """
    if spec is None:
        return None
    token = secrets.token_urlsafe(32)
    store = _load_store(spec.load, parent_env=parent_env)
    server = _BrokerServer(
        spec=spec,
        store=store,
        parent_env=parent_env,
        command_env=command_env,
        socket_path=scratch_dir / "cred-broker.sock",
        auth_token=token,
    )
    server.start()
    shim_dir = _write_shims(
        list(spec.tools), scratch_dir / "cred-shims", socket_path=server.socket_path
    )
    return CredentialBrokerRuntime(
        shim_dir=shim_dir, socket_path=server.socket_path, auth_token=token, _server=server
    )


__all__ = [
    "CredentialBrokerRuntime",
    "prepare_credential_broker_runtime",
]
