"""CreateOS sandbox os_env provider (type='createos').

Creates a remote createos VM, runs file I/O and shell commands inside it,
and destroys it on close().

Endpoints used:
  POST   /v1/sandboxes                   — create
  GET    /v1/sandboxes/:id               — poll status
  POST   /v1/sandboxes/:id/exec          — run command (buffered)
  GET    /v1/sandboxes/:id/files?path=…  — download file bytes
  PUT    /v1/sandboxes/:id/files?path=…  — upload file bytes
  DELETE /v1/sandboxes/:id               — destroy
"""

from __future__ import annotations

import atexit
import logging
import os
import shlex
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .async_utils import run_sync_on_thread
from .datamodel import OSEnvSpec
from .os_env import EditEntry, OpResult, OSEnvironment

_DEFAULT_BASE_URL = "https://api.sb.createos.sh"
_DEFAULT_SHAPE = "s-4vcpu-4gb"
_POLL_INTERVAL_S = 1.0
_READY_TIMEOUT_S = 120.0
_TERMINAL_STATUSES = frozenset({"error", "failed", "destroyed", "destroying"})

logger = logging.getLogger(__name__)


class _CreateosError(RuntimeError):
    pass


class _Http:
    """Thin sync httpx wrapper for the createos control-plane API."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60.0,
        )

    def _unwrap(self, resp: httpx.Response) -> Any:
        resp.raise_for_status()
        body = resp.json()
        # JSend success envelope: {"status": "success", "data": …}
        if isinstance(body, dict) and body.get("status") == "success":
            return body["data"]
        return body

    def post(self, path: str, body: Any = None) -> Any:
        resp = self._client.post(f"{self._base}{path}", json=body)
        return self._unwrap(resp)

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        resp = self._client.get(f"{self._base}{path}", params=params)
        return self._unwrap(resp)

    def put_bytes(self, path: str, data: bytes, params: dict[str, str]) -> None:
        resp = self._client.put(
            f"{self._base}{path}",
            content=data,
            params=params,
            headers={"Content-Type": "application/octet-stream"},
        )
        resp.raise_for_status()

    def get_bytes(self, path: str, params: dict[str, str]) -> bytes:
        resp = self._client.get(f"{self._base}{path}", params=params)
        resp.raise_for_status()
        return resp.content

    def delete(self, path: str) -> None:
        resp = self._client.delete(f"{self._base}{path}")
        if resp.status_code not in (200, 202, 204, 404):
            resp.raise_for_status()

    def close(self) -> None:
        self._client.close()


def _poll_until_running(http: _Http, sandbox_id: str) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while True:
        view = http.get(f"/v1/sandboxes/{sandbox_id}")
        status: str = view.get("status", "")
        if status == "running":
            return
        if status in _TERMINAL_STATUSES:
            raise _CreateosError(f"sandbox {sandbox_id} in terminal state: {status!r}")
        if time.monotonic() >= deadline:
            raise _CreateosError(
                f"sandbox {sandbox_id} not running after {_READY_TIMEOUT_S}s (status={status!r})"
            )
        time.sleep(_POLL_INTERVAL_S)


@dataclass
class CreateosOSEnvironment(OSEnvironment):
    """OSEnvironment backed by a remote createos sandbox VM.

    Do not construct directly — use :meth:`create_sync`.
    """

    _sandbox_id: str
    _http: _Http
    _closed: bool = False

    @classmethod
    def create_sync(cls, spec: OSEnvSpec) -> CreateosOSEnvironment:
        """Create a sandbox, poll until running, return an env handle."""
        base_url = (
            (spec.createos_base_url or "").strip()
            or os.environ.get("CREATEOS_BASE_URL", "").strip()
            or _DEFAULT_BASE_URL
        )
        api_key = (spec.createos_api_key or "").strip() or os.environ.get(
            "CREATEOS_API_KEY", ""
        ).strip()
        if not api_key:
            raise ValueError(
                "os_env type='createos' requires an API key — "
                "set CREATEOS_API_KEY or os_env.api_key in the agent YAML"
            )

        http = _Http(base_url, api_key)
        try:
            body: dict[str, Any] = {"shape": (spec.createos_shape or _DEFAULT_SHAPE)}
            if spec.createos_rootfs:
                body["rootfs"] = spec.createos_rootfs
            created = http.post("/v1/sandboxes", body)
            sandbox_id: str = created["id"]
            logger.debug("[createos] created sandbox %s", sandbox_id)
            _poll_until_running(http, sandbox_id)
            logger.debug("[createos] sandbox %s running", sandbox_id)
        except Exception:
            http.close()
            raise

        env = cls(
            spec=spec,
            cwd=Path(spec.cwd or "/root"),
            _sandbox_id=sandbox_id,
            _http=http,
        )
        # __del__ may not run at interpreter exit; close() is idempotent.
        atexit.register(env.close)
        return env

    def _abs(self, path: str) -> str:
        p = Path(path)
        return str(p if p.is_absolute() else self.cwd / p)

    # ── sync impls ────────────────────────────────────────────────────────

    def _read_sync(self, path: str, offset: int, limit: int | None) -> OpResult:
        if offset < 1:
            return {"error": "offset must be >= 1"}
        if limit is not None and limit < 1:
            return {"error": "limit must be >= 1"}
        try:
            raw = self._http.get_bytes(
                f"/v1/sandboxes/{self._sandbox_id}/files",
                {"path": self._abs(path)},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {"error": f"File not found: {path}"}
            return {"error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — IO failure surfaced via error dict
            return {"error": str(exc)}

        lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
        start = offset - 1
        selected = lines[start:] if limit is None else lines[start : start + limit]
        return {
            "ok": True,
            "content": "".join(selected),
            "offset": offset,
            "total_lines": len(lines),
        }

    def _write_sync(self, path: str, content: str) -> OpResult:
        if not path:
            return {"error": "path must be a non-empty string"}
        try:
            data = content.encode("utf-8")
            self._http.put_bytes(
                f"/v1/sandboxes/{self._sandbox_id}/files",
                data,
                {"path": self._abs(path)},
            )
        except Exception as exc:  # noqa: BLE001 — IO failure surfaced via error dict
            return {"error": str(exc)}
        return {"ok": True, "bytes_written": len(content.encode("utf-8"))}

    def _edit_sync(
        self,
        path: str,
        old_text: str | None,
        new_text: str | None,
        edits: Sequence[EditEntry] | None,
    ) -> OpResult:
        if not path:
            return {"error": "path must be a non-empty string"}

        read_result = self._read_sync(path, 1, None)
        if "error" in read_result:
            return read_result
        current: str = read_result["content"]

        pairs: list[tuple[str, str]] = []
        if edits:
            for e in edits:
                pairs.append((e["oldText"], e["newText"]))
        elif old_text is not None and new_text is not None:
            pairs.append((old_text, new_text))
        else:
            return {"error": "edit requires old_text+new_text or a non-empty edits list"}

        updated = current
        for old, new in pairs:
            if old not in updated:
                return {"error": f"Could not find oldText in '{path}': {old[:80]!r}"}
            updated = updated.replace(old, new, 1)

        return self._write_sync(path, updated)

    def _shell_sync(
        self,
        command: str,
        timeout: int | None,
        max_output: int | None,
    ) -> OpResult:
        if not command:
            return {"error": "command must be a non-empty string"}
        if timeout is not None and timeout < 1:
            return {"error": "timeout must be >= 1"}

        # Run bash -c under the configured cwd
        full_cmd = f"cd {shlex.quote(str(self.cwd))} && {command}"
        body: dict[str, Any] = {"cmd": "bash", "args": ["-c", full_cmd]}
        if timeout is not None:
            body["timeout"] = timeout

        try:
            resp = self._http.post(f"/v1/sandboxes/{self._sandbox_id}/exec", body)
        except Exception as exc:  # noqa: BLE001 — exec failure surfaced via error dict
            return {"error": str(exc)}

        # ExecResponse: {"result": {"stdout", "stderr", "exit_code"}, "exec_ms": …}
        result = resp.get("result", resp)
        stdout: str = result.get("stdout", "")
        stderr: str = result.get("stderr", "")
        exit_code: int = result.get("exit_code", -1)

        if max_output is not None:
            stdout = stdout[:max_output]
            stderr = stderr[:max_output]

        out: OpResult = {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}
        if exit_code != 0:
            detail = (stderr or stdout).strip()
            out["error"] = (
                f"Command exited with status {exit_code}: {detail[:200]}"
                if detail
                else f"Command exited with status {exit_code}"
            )
        return out

    # ── OSEnvironment async interface ─────────────────────────────────────

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> OpResult:
        return await run_sync_on_thread(self._read_sync, path, offset, limit)

    async def write(self, path: str, content: str) -> OpResult:
        return await run_sync_on_thread(self._write_sync, path, content)

    async def edit(
        self,
        path: str,
        *,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: Sequence[EditEntry] | None = None,
    ) -> OpResult:
        return await run_sync_on_thread(self._edit_sync, path, old_text, new_text, edits)

    async def shell(
        self,
        command: str,
        timeout: int | None = None,
        max_output: int | None = None,
    ) -> OpResult:
        return await run_sync_on_thread(self._shell_sync, command, timeout, max_output)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._http.delete(f"/v1/sandboxes/{self._sandbox_id}")
            logger.debug("[createos] destroyed sandbox %s", self._sandbox_id)
        except Exception as exc:  # noqa: BLE001 — best-effort destroy on close
            logger.warning("[createos] failed to destroy sandbox %s: %s", self._sandbox_id, exc)
        finally:
            self._http.close()

    def __del__(self) -> None:
        self.close()
