"""Opt-in Codex parity fixtures.

These tests run Omnigent's real CodexExecutor against a real Codex CLI while
mocking only the upstream Responses API with Codex's own WireMock helpers.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PARITY_ROOT = ROOT / "tests" / "codex_parity"
SIDECAR_MANIFEST = ROOT / "tests" / "codex_parity" / "sidecar" / "Cargo.toml"
SIDECAR_TARGET_DIR = ROOT / ".tmp-codex-parity-target"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--codex-parity",
        action="store_true",
        default=False,
        help="Run real-Codex/mock-Responses parity tests.",
    )
    parser.addoption(
        "--codex-bin",
        action="append",
        default=[],
        help="Codex CLI binary to test. Repeatable. Defaults to PATH lookup.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--codex-parity"):
        return
    marker = pytest.mark.skip(reason="Codex parity tests require --codex-parity")
    for item in items:
        if _is_parity_item(item):
            item.add_marker(marker)


def _is_parity_item(item: pytest.Item) -> bool:
    path = Path(str(item.fspath)).resolve()
    return path == PARITY_ROOT or PARITY_ROOT in path.parents


@pytest.fixture(scope="session")
def sidecar_bin() -> Path:
    if shutil.which("cargo") is None:
        pytest.skip("cargo is required for Codex parity sidecar")
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


def _codex_bins(config: pytest.Config) -> list[str]:
    explicit = [str(Path(value)) for value in config.getoption("--codex-bin")]
    env_value = os.environ.get("CODEX_TEST_BINS", "").strip()
    from_env = [value for value in env_value.split(os.pathsep) if value]
    bins = explicit or from_env or ["codex"]
    return bins


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "codex_bin" not in metafunc.fixturenames:
        return
    bins = _codex_bins(metafunc.config)
    metafunc.parametrize("codex_bin", bins, ids=[Path(value).name for value in bins])


@pytest.fixture
def resolved_codex_bin(codex_bin: str) -> str:
    resolved = shutil.which(codex_bin) if os.sep not in codex_bin else codex_bin
    if resolved is None:
        pytest.skip(f"codex binary not found: {codex_bin}")
    version = subprocess.run(
        [resolved, "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if version.returncode != 0:
        pytest.skip(f"codex binary is not runnable: {codex_bin}")
    return resolved


class CodexResponsesSidecar:
    def __init__(self, proc: subprocess.Popen[str], base_url: str) -> None:
        self._proc = proc
        self.base_url = base_url
        self._stderr_lines: list[str] = []
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def requests(self, *, min_count: int = 0, timeout_ms: int = 5000) -> list[dict[str, Any]]:
        response = self._command({"op": "requests", "min": min_count, "timeout_ms": timeout_ms})
        assert response["type"] == "requests"
        return response["requests"]

    def close(self) -> None:
        if self._proc.poll() is not None:
            return
        try:
            self._command({"op": "shutdown"})
        except Exception:
            pass
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
        return "\n".join(self._stderr_lines[-20:])


@pytest.fixture
def codex_responses_sidecar(
    sidecar_bin: Path,
    tmp_path: Path,
) -> Iterator[Callable[[list[list[dict[str, Any]]]], CodexResponsesSidecar]]:
    started: list[CodexResponsesSidecar] = []

    def start(responses: list[list[dict[str, Any]]]) -> CodexResponsesSidecar:
        config_path = tmp_path / f"responses-{len(started)}.json"
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
        started.append(sidecar)
        return sidecar

    yield start

    for sidecar in started:
        sidecar.close()
