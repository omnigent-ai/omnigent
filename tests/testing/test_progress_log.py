"""Content-free pytest phase and worker-down diagnostics; no runtime journey."""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def progress_plugin(request: pytest.FixtureRequest) -> ModuleType:
    """Use the already-loaded owning plugin without importing its fixture stack."""
    source = Path(__file__).parents[1] / "conftest.py"
    return next(
        plugin
        for plugin in request.config.pluginmanager.get_plugins()
        if isinstance(plugin, ModuleType) and getattr(plugin, "__file__", None) == str(source)
    )


def test_legacy_start_end_columns_are_unchanged(
    progress_plugin: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log = tmp_path / "progress.log"
    nodeid = "test_owned.py::test_example"
    with monkeypatch.context() as patch:
        patch.setattr(progress_plugin, "_PROGRESS_LOG_PATH", str(log))
        progress_plugin._write_progress_event("START", nodeid)
        progress_plugin._write_progress_event("END", nodeid, rss_kb=123)
    start, end = (line.split("\t") for line in log.read_text().splitlines())
    assert float(start[0]) <= float(end[0])
    assert start[1:] == ["START", nodeid]
    assert end[1:] == ["END", nodeid, "123"]


@pytest.mark.parametrize("phase", ["setup", "call", "teardown"])
def test_phase_boundaries_preserve_original_exception_without_content(
    progress_plugin: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    log = tmp_path / "progress.log"
    nodeid = "test_owned.py::test_example"
    original = RuntimeError("private-diagnostic-sentinel")
    with monkeypatch.context() as patch:
        patch.setattr(progress_plugin, "_PROGRESS_LOG_PATH", str(log))
        patch.setenv("PYTEST_XDIST_WORKER", "gw2")
        with pytest.raises(RuntimeError) as caught:
            with progress_plugin._record_test_phase(nodeid, phase):
                raise original
    assert caught.value is original
    lines = [line.split("\t") for line in log.read_text().splitlines()]
    for event, fields in zip(("PHASE_START", "PHASE_END"), lines, strict=True):
        assert float(fields[0]) > 0
        assert fields[1:] == [
            event,
            nodeid,
            f"phase={phase}",
            "worker=gw2",
            f"pid={os.getpid()}",
        ]
    assert "private-diagnostic-sentinel" not in log.read_text()


@pytest.mark.parametrize("has_error", [False, True])
def test_worker_down_never_renders_or_interprets_error(
    progress_plugin: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_error: bool,
) -> None:
    class PrivateError:
        def __str__(self) -> str:
            raise AssertionError("error content must not be inspected")

        def __repr__(self) -> str:
            raise AssertionError("error content must not be inspected")

        def __bool__(self) -> bool:
            raise AssertionError("error content must not be inspected")

    node = SimpleNamespace(gateway=SimpleNamespace(id="gw2"))
    log = tmp_path / "progress.log"
    with monkeypatch.context() as patch:
        patch.setattr(progress_plugin, "_PROGRESS_LOG_PATH", str(log))
        progress_plugin.pytest_testnodedown(node, PrivateError() if has_error else None)
    fields = log.read_text().strip().split("\t")
    assert float(fields[0]) > 0
    assert fields[1:] == ["WORKER_DOWN", "gw2", f"error_present={int(has_error)}"]


@pytest.mark.parametrize(
    "worker",
    ["private-diagnostic-sentinel", "gw2\tprivate-diagnostic-sentinel", "gw" + "1" * 33],
)
def test_noncanonical_worker_identity_is_not_exported(
    progress_plugin: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker: str,
) -> None:
    log = tmp_path / "progress.log"
    with monkeypatch.context() as patch:
        patch.setattr(progress_plugin, "_PROGRESS_LOG_PATH", str(log))
        patch.setenv("PYTEST_XDIST_WORKER", worker)
        with progress_plugin._record_test_phase("test_owned.py::test_example", "call"):
            pass
    lines = [line.split("\t") for line in log.read_text().splitlines()]
    assert len(lines) == 2
    assert all(fields[4] == "worker=unknown" for fields in lines)
    assert worker not in log.read_text()


def test_disabled_progress_does_not_open_files(
    progress_plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("disabled progress must not open files")

    with monkeypatch.context() as patch:
        patch.setattr(progress_plugin, "_PROGRESS_LOG_PATH", None)
        patch.setattr(progress_plugin, "open", forbidden_open, raising=False)
        with progress_plugin._record_test_phase("test_owned.py::test_example", "call"):
            pass
        progress_plugin.pytest_testnodedown(
            SimpleNamespace(gateway=SimpleNamespace(id="gw2")), None
        )


def test_diagnostic_io_failure_does_not_replace_original_error(
    progress_plugin: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unavailable_open(*args: object, **kwargs: object) -> None:
        raise OSError("private-diagnostic-sentinel")

    original = RuntimeError("original assertion remains unchanged")
    with monkeypatch.context() as patch:
        patch.setattr(progress_plugin, "_PROGRESS_LOG_PATH", str(tmp_path / "progress.log"))
        patch.setattr(progress_plugin, "open", unavailable_open, raising=False)
        with pytest.raises(RuntimeError) as caught:
            with progress_plugin._record_test_phase("test_owned.py::test_example", "call"):
                raise original
        progress_plugin.pytest_testnodedown(
            SimpleNamespace(gateway=SimpleNamespace(id="gw2")), original
        )
    assert caught.value is original
