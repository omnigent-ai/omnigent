"""Shared launcher for Codex's mock Responses sidecar."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_MANIFEST = ROOT / "tests" / "codex_parity" / "sidecar" / "Cargo.toml"
SIDECAR_TARGET_DIR = ROOT / ".tmp-codex-parity-target"


class CodexResponsesSidecar:
    """Running mock Responses server backed by Codex's Rust test helpers."""

    def __init__(self, proc: subprocess.Popen[str], base_url: str) -> None:
        self._proc = proc
        self.base_url = base_url
        self._stderr_lines: list[str] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def requests(self, *, min_count: int = 0, timeout_ms: int = 5000) -> list[dict[str, Any]]:
        """Return captured stable request fields, waiting for ``min_count`` if requested."""
        response = self._command({"op": "requests", "min": min_count, "timeout_ms": timeout_ms})
        assert response["type"] == "requests"
        return response["requests"]

    def close(self) -> None:
        """Stop the sidecar process."""
        if self._proc.poll() is not None:
            return
        with contextlib.suppress(Exception):
            self._command({"op": "shutdown"})
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)

    def _command(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        line = self._read_stdout_line(timeout_s=10)
        return json.loads(line)

    def _read_stdout_line(self, *, timeout_s: float) -> str:
        assert self._proc.stdout is not None
        out: queue.Queue[str | None] = queue.Queue(maxsize=1)

        def read() -> None:
            out.put(self._proc.stdout.readline())

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        try:
            line = out.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise AssertionError(f"sidecar stdout timed out; stderr={self.stderr_tail()}") from exc
        if not line:
            raise AssertionError(f"sidecar exited early; stderr={self.stderr_tail()}")
        return line

    def _drain_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        for line in self._proc.stderr:
            self._stderr_lines.append(line.rstrip())
            if len(self._stderr_lines) > 200:
                self._stderr_lines.pop(0)

    def stderr_tail(self) -> str:
        """Return recent sidecar stderr for assertion messages."""
        return "\n".join(self._stderr_lines[-20:])


def build_sidecar_bin() -> Path:
    """Build the Rust sidecar binary and return its path."""
    if shutil.which("cargo") is None:
        raise RuntimeError("cargo is required for Codex parity sidecar")
    subprocess.run(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(SIDECAR_MANIFEST),
            "--target-dir",
            str(SIDECAR_TARGET_DIR),
            "--quiet",
        ],
        cwd=ROOT,
        check=True,
    )
    binary = SIDECAR_TARGET_DIR / "debug" / "codex-parity-sidecar"
    if os.name == "nt":
        binary = binary.with_suffix(".exe")
    return binary


def start_codex_responses_sidecar(
    sidecar_bin: Path,
    config_path: Path,
    responses: list[list[dict[str, Any]]],
) -> CodexResponsesSidecar:
    """Start a sidecar process serving ``responses`` in order."""
    config_path.write_text(json.dumps({"responses": responses}), encoding="utf-8")
    proc = subprocess.Popen(
        [str(sidecar_bin), "--config", str(config_path)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    sidecar = CodexResponsesSidecar(proc, base_url="")
    ready = json.loads(sidecar._read_stdout_line(timeout_s=15))
    assert ready["type"] == "ready"
    sidecar.base_url = ready["base_url"]
    return sidecar
