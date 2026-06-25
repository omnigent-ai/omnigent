"""Opt-in Codex parity fixtures.

These tests run Omnigent's real CodexExecutor against a real Codex CLI while
mocking only the upstream Responses API with Codex's own WireMock helpers.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.codex_parity.sidecar_harness import (
    CodexResponsesSidecar,
    build_sidecar_bin,
    start_codex_responses_sidecar,
)

ROOT = Path(__file__).resolve().parents[2]
PARITY_ROOT = ROOT / "tests" / "codex_parity"


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
    with contextlib.suppress(RuntimeError):
        return build_sidecar_bin()
    pytest.skip("cargo is required for Codex parity sidecar")


def _codex_bins(config: pytest.Config) -> list[str]:
    explicit = [str(Path(value)) for value in config.getoption("--codex-bin")]
    env_value = os.environ.get("CODEX_TEST_BINS", "").strip()
    from_env = [value for value in env_value.split(os.pathsep) if value]
    return explicit or from_env or ["codex"]


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
        capture_output=True,
        check=False,
    )
    if version.returncode != 0:
        pytest.skip(f"codex binary is not runnable: {codex_bin}")
    return resolved


@pytest.fixture
def codex_responses_sidecar(
    sidecar_bin: Path,
    tmp_path: Path,
) -> Iterator[Callable[[list[list[dict[str, Any]]]], CodexResponsesSidecar]]:
    started: list[CodexResponsesSidecar] = []

    def start(responses: list[list[dict[str, Any]]]) -> CodexResponsesSidecar:
        sidecar = start_codex_responses_sidecar(
            sidecar_bin,
            tmp_path / f"responses-{len(started)}.json",
            responses,
        )
        started.append(sidecar)
        return sidecar

    yield start

    for sidecar in started:
        sidecar.close()
