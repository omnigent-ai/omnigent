"""Content-free pytest phase and worker-down diagnostics; no runtime journey."""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytest_plugins = ["pytester"]


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
        patch.setattr(progress_plugin, "_PROGRESS_OPEN", forbidden_open, raising=False)
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
        patch.setattr(progress_plugin, "_PROGRESS_OPEN", unavailable_open, raising=False)
        with pytest.raises(RuntimeError) as caught:
            with progress_plugin._record_test_phase("test_owned.py::test_example", "call"):
                raise original
        progress_plugin.pytest_testnodedown(
            SimpleNamespace(gateway=SimpleNamespace(id="gw2")), original
        )
    assert caught.value is original


@pytest.mark.parametrize("primitive", ["clock", "open"])
def test_progress_hook_dispatch_isolates_sut_mocks_and_preserves_failure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, primitive: str
) -> None:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("PYTEST_PLUGINS", "")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    pytester.makeini("[pytest]\n")
    pytester.makeconftest(
        """
import json
from pathlib import Path
from types import SimpleNamespace

from tests import conftest as progress

progress._PROGRESS_LOG_PATH = str(Path(__file__).parent / "progress.log")
contexts = []

def record_context(nodeid, **kwargs):
    last = Path(progress._PROGRESS_LOG_PATH).read_text().splitlines()[-1].split("\\t")
    contexts.append({"nodeid": nodeid, "kwargs": kwargs, "last": last[1:]})

progress._model_pools = SimpleNamespace(set_current_test=record_context)
pytest_runtest_logstart = progress.pytest_runtest_logstart
pytest_runtest_setup = progress.pytest_runtest_setup
pytest_runtest_setup_progress = progress.pytest_runtest_setup_progress
pytest_runtest_call = progress.pytest_runtest_call
pytest_runtest_teardown = progress.pytest_runtest_teardown
pytest_runtest_logfinish = progress.pytest_runtest_logfinish

def pytest_sessionfinish():
    Path(__file__).with_name("contexts.json").write_text(json.dumps(contexts))
"""
    )
    pytester.makepyfile(
        test_dispatch="""
import time
from unittest.mock import Mock

import pytest

MODE = "__MODE__"

@pytest.fixture
def sut_mock(monkeypatch):
    if MODE == "clock":
        timestamps = iter([1000.0, 1000.050])
        mock = Mock(side_effect=lambda: next(timestamps))
        monkeypatch.setattr(time, "time", mock)
    else:
        mock = Mock(side_effect=AssertionError("SUT file spy must stay untouched"))
        monkeypatch.setattr("builtins.open", mock)
    yield mock
    if MODE == "clock":
        assert mock.call_count == 2
    else:
        mock.assert_not_called()

def exercise_sut(mock):
    private = "private-diagnostic-sentinel"
    if MODE == "clock":
        assert [time.time(), time.time()] == [1000.0, 1000.050]
        assert mock.call_count == 2
    else:
        mock.assert_not_called()

def test_pass(sut_mock):
    exercise_sut(sut_mock)

def test_original_failure(sut_mock):
    exercise_sut(sut_mock)
    raise AssertionError("owned-original-failure")
""".replace("__MODE__", primitive)
    )
    report = pytester.path / "junit.xml"
    result = pytester.runpytest_subprocess("-q", "--tb=short", f"--junitxml={report}")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(passed=1, failed=1, errors=0)
    failure = ET.parse(report).findall(".//testcase/failure")
    assert len(failure) == 1
    assert "owned-original-failure" in (failure[0].text or "")
    assert "StopIteration" not in result.stdout.str()
    assert "SUT file spy must stay untouched" not in result.stdout.str()
    assert "private-diagnostic-sentinel" not in report.read_text()

    log = (pytester.path / "progress.log").read_text()
    records = [line.split("\t") for line in log.splitlines()]
    assert len(records) == 16
    nodes = ["test_dispatch.py::test_pass", "test_dispatch.py::test_original_failure"]
    for node in nodes:
        rows = [row for row in records if row[2] == node]
        assert [row[1] for row in rows] == [
            "START",
            "PHASE_START",
            "PHASE_END",
            "PHASE_START",
            "PHASE_END",
            "PHASE_START",
            "PHASE_END",
            "END",
        ]
        assert [row[3] for row in rows[1:-1]] == [
            "phase=setup",
            "phase=setup",
            "phase=call",
            "phase=call",
            "phase=teardown",
            "phase=teardown",
        ]
        assert all(
            row[4] == "worker=main" and int(row[5].removeprefix("pid=")) > 0 for row in rows[1:-1]
        )
        assert len(rows[0]) == 3 and len(rows[-1]) in (3, 4)
    assert "private-diagnostic-sentinel" not in log
    assert "owned-original-failure" not in log
    contexts = json.loads((pytester.path / "contexts.json").read_text())
    assert len(contexts) == 4
    for index, node in enumerate(nodes):
        setup, clear = contexts[2 * index : 2 * index + 2]
        assert setup["nodeid"] == node
        assert setup["kwargs"] == {"attempt": 0, "pinned": False}
        assert setup["last"][:3] == ["PHASE_START", node, "phase=setup"]
        assert clear["nodeid"] is None
        assert clear["last"][:2] == ["END", node]
