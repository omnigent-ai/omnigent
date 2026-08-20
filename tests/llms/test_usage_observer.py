"""Unit tests for :mod:`omnigent.llms._usage_observer`.

Covers the observer registry contract (add/remove/exception-isolation)
and the auto-recorder that activates when ``OMNIGENT_TOKEN_USAGE_JSON``
is set.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from omnigent.llms import _usage_observer


@pytest.fixture(autouse=True)
def _reset_state() -> Any:
    """Reset shared module state before AND after each test so the
    write-through recorder doesn't mix leftover fixture rows from a
    previous test into the file the current test asserts on."""
    _usage_observer._RECORDS.clear()
    _usage_observer._CURRENT_NODEID = None
    yield
    _usage_observer._RECORDS.clear()
    _usage_observer._CURRENT_NODEID = None


# ── Recording (auto-recorder behavior) ──────────────────────────────


def test_notify_records_under_current_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test("test_a")
    _usage_observer.notify(model="m", input_tokens=10, output_tokens=5, total_tokens=15)
    _usage_observer.notify(model="m2", input_tokens=20, output_tokens=7, total_tokens=27)
    _usage_observer.set_current_test("test_b")
    _usage_observer.notify(model="m", input_tokens=3, output_tokens=1, total_tokens=4)

    a = _usage_observer._RECORDS["test_a"]
    assert a["input_tokens"] == 30
    assert a["output_tokens"] == 12
    assert a["total_tokens"] == 42
    assert a["calls"] == 2
    assert sorted(a["models"]) == ["m", "m2"]

    b = _usage_observer._RECORDS["test_b"]
    assert b["total_tokens"] == 4
    assert b["calls"] == 1

    # Per-model breakdown: each model's calls/tokens tracked separately
    # within the bucket so the aggregator can tally load per model. A
    # missing or merged entry would make the per-model tally lie about
    # which endpoint absorbed the calls.
    assert a["by_model"]["m"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "calls": 1,
    }
    assert a["by_model"]["m2"] == {
        "input_tokens": 20,
        "output_tokens": 7,
        "total_tokens": 27,
        "calls": 1,
    }


def test_notify_without_model_buckets_under_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Calls with no model attribution land under ``<unknown>``."""
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test("test_x")
    _usage_observer.notify(model=None, input_tokens=2, output_tokens=1, total_tokens=3)
    bucket = _usage_observer._RECORDS["test_x"]
    # model=None must not crash the per-model breakdown nor silently
    # drop the call from the tally.
    assert bucket["by_model"]["<unknown>"]["calls"] == 1
    assert bucket["models"] == []


def test_notify_without_current_test_buckets_under_no_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Subprocesses don't know the nodeid and land under ``<no-test>``."""
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test(None)
    _usage_observer.notify(model="m", input_tokens=10, output_tokens=5, total_tokens=15)
    assert "<no-test>" in _usage_observer._RECORDS
    assert _usage_observer._RECORDS["<no-test>"]["total_tokens"] == 15


def test_notify_skips_recording_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env var -> no records (production code path is a no-op)."""
    monkeypatch.delenv(_usage_observer._ENV_VAR, raising=False)
    _usage_observer.set_current_test("test_x")
    _usage_observer.notify(model="m", input_tokens=10, output_tokens=5, total_tokens=15)
    assert _usage_observer._RECORDS == {}


def test_notify_skips_zero_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test("test_x")
    _usage_observer.notify(model="m", input_tokens=0, output_tokens=0, total_tokens=0)
    assert _usage_observer._RECORDS == {}
    # Zero-usage calls must not trigger the write-through either: an
    # all-zero file would make the aggregator count empty suites.
    # tokens*.json is what the aggregator and the artifact upload glob;
    # the current-test sidecar (.txt) is expected to exist.
    assert list(tmp_path.glob("tokens*.json")) == []


# ── Output path (filename suffix logic) ─────────────────────────────


def test_output_path_includes_xdist_worker_and_pid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "tokens.json"
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(target))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    path = _usage_observer._output_path()
    assert path is not None
    assert path.name.startswith("tokens-gw3-pid")
    assert path.name.endswith(".json")


def test_output_path_falls_back_to_pid_only_without_xdist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "tokens.json"
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(target))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    path = _usage_observer._output_path()
    assert path is not None
    assert path.name.startswith("tokens-pid")


def test_output_path_unset_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_usage_observer._ENV_VAR, raising=False)
    assert _usage_observer._output_path() is None


# ── write-through writer ───────────────────────────────────────────


def test_notify_writes_file_through_without_exit_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each recorded ``notify()`` rewrites the on-disk file in place.

    The file must be complete and current after the call returns, with
    no exit-time flush involved. On revert to an atexit-only writer
    the glob below finds nothing.
    """
    target = tmp_path / "tokens.json"
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(target))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    _usage_observer.set_current_test("test_a")
    _usage_observer.notify(model="m", input_tokens=10, output_tokens=5, total_tokens=15)
    _usage_observer.set_current_test("test_b")
    _usage_observer.notify(model="m", input_tokens=3, output_tokens=1, total_tokens=4)

    written = list(tmp_path.glob("tokens-pid*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["totals"] == {
        "input_tokens": 13,
        "output_tokens": 6,
        "total_tokens": 19,
        "calls": 2,
    }
    assert set(payload["by_test"]) == {"test_a", "test_b"}
    # Cross-test per-model roll-up: both notifications used model "m",
    # so its row carries the full file totals. The aggregator reads
    # this key to build the calls-per-model tally.
    assert payload["totals_by_model"] == {
        "m": {"input_tokens": 13, "output_tokens": 6, "total_tokens": 19, "calls": 2},
    }
    # No leftover temp file: the atomic write must finish with the
    # os.replace, or the aggregator's tokens*.json glob would pick up
    # a stale .tmp alongside the real file.
    assert list(tmp_path.glob("*.tmp")) == []


def test_records_survive_sigkill(tmp_path: Path) -> None:
    """A hard-killed process still leaves a complete tokens file.

    This is the production failure mode that left e2e CI with zero
    token artifacts: server and harness subprocesses are torn down
    with SIGTERM/SIGKILL, and signals never run ``atexit`` hooks. On
    revert to an exit-time writer the child dies before writing and
    the glob below finds nothing.
    """
    target = tmp_path / "tokens.json"
    script = (
        "import os, signal\n"
        "from omnigent.llms import _usage_observer\n"
        "_usage_observer.notify(model='m', input_tokens=7, output_tokens=3, total_tokens=10)\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    env = {**os.environ, _usage_observer._ENV_VAR: str(target)}
    # The child is not an xdist worker; drop the inherited worker id so
    # the output filename has the pid-only shape the glob expects.
    env.pop("PYTEST_XDIST_WORKER", None)
    proc = subprocess.run([sys.executable, "-c", script], env=env, timeout=60)
    # -SIGKILL proves the child died from the kill, not a clean exit
    # (a clean exit would mean the test never exercised hard-kill
    # durability) and not an import/notify crash (positive code).
    assert proc.returncode == -signal.SIGKILL
    written = list(tmp_path.glob("tokens-pid*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["totals"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "total_tokens": 10,
        "calls": 1,
    }
    # The child has no in-process nodeid and no sidecar file next to
    # its tokens path, so usage lands in <no-test>.
    assert payload["by_test"]["<no-test>"]["total_tokens"] == 10


# ── Subprocess attribution via the current-test sidecar ─────────────


def test_set_current_test_publishes_and_clears_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``set_current_test`` mirrors the nodeid to the per-worker sidecar.

    The sidecar is the only channel subprocesses have for test
    attribution; if publish or cleanup breaks, e2e usage regresses to
    one giant ``<no-test>`` bucket per shard.
    """
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    _usage_observer.set_current_test("tests/e2e/test_x.py::test_y")
    sidecar = tmp_path / "tokens-current-test-gw2.txt"
    # Worker id in the name keeps parallel xdist workers (each with its
    # own spawned server) from clobbering each other's attribution.
    assert sidecar.read_text() == "tests/e2e/test_x.py::test_y"
    # ``.txt`` so neither the aggregator nor the artifact upload step
    # (both glob tokens*.json) picks the sidecar up as a usage file.
    assert list(tmp_path.glob("tokens*.json")) == []

    _usage_observer.set_current_test(None)
    # Cleared between tests: a leftover sidecar would misattribute
    # late-arriving subprocess usage to a test that already finished.
    assert not sidecar.exists()


def test_record_falls_back_to_sidecar_when_no_inprocess_nodeid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A process with no in-process nodeid attributes via the sidecar.

    Simulates the subprocess side: ``_CURRENT_NODEID`` is ``None``
    (the pytest plugin never runs there) and the sidecar was written
    by the parent test process. On revert to ``_CURRENT_NODEID or
    "<no-test>"`` the bucket key degrades to ``<no-test>``.
    """
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    # _CURRENT_NODEID is None (autouse reset fixture); the sidecar
    # below simulates the parent test process having published one.
    (tmp_path / "tokens-current-test-main.txt").write_text("tests/e2e/test_x.py::test_y")
    _usage_observer.notify(model="m", input_tokens=10, output_tokens=5, total_tokens=15)
    assert "<no-test>" not in _usage_observer._RECORDS
    assert _usage_observer._RECORDS["tests/e2e/test_x.py::test_y"]["total_tokens"] == 15


def test_subprocess_attributes_usage_to_parent_test_via_sidecar(tmp_path: Path) -> None:
    """End-to-end: a spawned process inherits the env and reads the sidecar.

    This is the production e2e shape that produced the all-``<no-test>``
    sticky tables: real LLM calls notify() inside server / runner
    subprocesses where the pytest plugin never runs. The parent
    publishes the nodeid, the child process must key its tokens file
    by it.
    """
    target = tmp_path / "tokens.json"
    (tmp_path / "tokens-current-test-main.txt").write_text("tests/e2e/test_x.py::test_y")
    script = (
        "from omnigent.llms import _usage_observer\n"
        "_usage_observer.notify(model='m', input_tokens=7, output_tokens=3, total_tokens=10)\n"
    )
    env = {**os.environ, _usage_observer._ENV_VAR: str(target)}
    # The child is not an xdist worker; drop any inherited worker id so
    # it resolves the same "-main" sidecar the parent wrote above.
    env.pop("PYTEST_XDIST_WORKER", None)
    proc = subprocess.run([sys.executable, "-c", script], env=env, timeout=60)
    assert proc.returncode == 0
    written = list(tmp_path.glob("tokens-pid*.json"))
    # Exactly one file: the child's pid-suffixed write-through output.
    # Zero means the recorder never activated in the child; more than
    # one means an unexpected extra process wrote into the temp dir.
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    # The nodeid crossed the process boundary: the child's bucket key
    # is the parent's test, not <no-test>. A <no-test> key here means
    # the sidecar read fell through and per-test attribution is lost.
    assert payload["by_test"]["tests/e2e/test_x.py::test_y"]["total_tokens"] == 10
    assert "<no-test>" not in payload["by_test"]


# ── Observer registry contract (unchanged from PR-B) ────────────────


def test_add_observer_remove_unsubscribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_usage_observer._ENV_VAR, raising=False)
    calls: list[dict[str, Any]] = []

    def cb(*, model: str | None, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        calls.append(
            {"model": model, "input": input_tokens, "output": output_tokens, "total": total_tokens}
        )

    remove = _usage_observer.add_observer(cb)
    _usage_observer.notify(model="m1", input_tokens=1, output_tokens=2, total_tokens=3)
    remove()
    _usage_observer.notify(model="m2", input_tokens=4, output_tokens=5, total_tokens=9)

    assert calls == [{"model": "m1", "input": 1, "output": 2, "total": 3}]


def test_notify_isolates_observer_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_usage_observer._ENV_VAR, raising=False)
    bad_called = False
    good_called = False

    def bad(**_: Any) -> None:
        nonlocal bad_called
        bad_called = True
        raise RuntimeError("boom")

    def good(**_: Any) -> None:
        nonlocal good_called
        good_called = True

    remove_bad = _usage_observer.add_observer(bad)
    remove_good = _usage_observer.add_observer(good)
    try:
        _usage_observer.notify(model="m", input_tokens=1, output_tokens=2, total_tokens=3)
    finally:
        remove_bad()
        remove_good()

    assert bad_called
    assert good_called


# ── End-to-end through Client.responses.create ──────────────────────


@pytest.mark.asyncio
async def test_client_notify_records_with_active_test(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``Client().responses.create()`` runs through :func:`notify` and
    lands in the auto-recorder when the env var is set."""
    from omnigent.llms.client import Client, _ResponsesNamespace
    from omnigent.llms.types import MessageOutput, OutputText, Response, Usage

    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    fake = Response(
        output=[MessageOutput(content=[OutputText(text="hi")])],
        model="claude-test",
        usage=Usage(input_tokens=11, output_tokens=4, total_tokens=15),
    )

    async def _fake_do_create(self: Any, *args: Any, **kwargs: Any) -> Response:
        return fake

    monkeypatch.setattr(_ResponsesNamespace, "_do_create", _fake_do_create, raising=True)

    _usage_observer.set_current_test("test_nonstream")
    await Client().responses.create(input=[], model="claude-test")

    assert _usage_observer._RECORDS["test_nonstream"]["total_tokens"] == 15


# ── Thread safety ───────────────────────────────────────────────────


def test_concurrent_notifies_do_not_lose_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``notify()`` from many threads must sum exactly, not lose increments.

    Without the lock around ``_record``, the ``bucket[k] += n`` pattern
    is a non-atomic read-modify-write across LOAD/ADD/STORE bytecodes.
    Concurrent threads racing on the same bucket would silently drop
    increments. This test runs 8 threads x 500 notifies each and asserts
    the totals match the analytical sum.

    On revert (drop the lock): expect intermittent failures where
    ``total_tokens`` is less than the analytical sum, with the gap
    growing as thread count increases.
    """
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test("test_concurrent")

    threads = 8
    iters = 500

    def hammer() -> None:
        for _ in range(iters):
            _usage_observer.notify(
                model="m",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
            )

    workers = [threading.Thread(target=hammer) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    bucket = _usage_observer._RECORDS["test_concurrent"]
    assert bucket["calls"] == threads * iters
    assert bucket["input_tokens"] == threads * iters * 1
    assert bucket["output_tokens"] == threads * iters * 2
    assert bucket["total_tokens"] == threads * iters * 3

    # The write-through file must also land on the final sum: a stale
    # snapshot overwriting a newer one (write outside the lock) would
    # leave the file behind the in-memory records.
    written = list(tmp_path.glob("tokens-*.json"))
    assert len(written) == 1
    payload = json.loads(written[0].read_text())
    assert payload["totals"]["calls"] == threads * iters


# ── notify_from_dict ─────────────────────────────────────────────


def test_notify_from_dict_unpacks_usage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """notify_from_dict unpacks standard keys and delegates to notify."""
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test("test_from_dict")
    _usage_observer.notify_from_dict(
        model="m",
        usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    bucket = _usage_observer._RECORDS["test_from_dict"]
    assert bucket["total_tokens"] == 15
    assert bucket["calls"] == 1


def test_notify_from_dict_none_usage_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """notify_from_dict with None usage is a no-op."""
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test("test_none")
    _usage_observer.notify_from_dict(model="m", usage=None)
    assert _usage_observer._RECORDS == {}


def test_notify_from_dict_empty_dict_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """notify_from_dict({}) resolves to notify(..., 0, 0, 0) — records stay empty."""
    monkeypatch.setenv(_usage_observer._ENV_VAR, str(tmp_path / "tokens.json"))
    _usage_observer.set_current_test("test_empty")
    _usage_observer.notify_from_dict(model="m", usage={})
    assert _usage_observer._RECORDS == {}


def test_notify_from_dict_non_dict_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notify_from_dict with a non-dict value is a no-op."""
    monkeypatch.delenv(_usage_observer._ENV_VAR, raising=False)
    _usage_observer.notify_from_dict(model="m", usage="not a dict")  # type: ignore[arg-type]
    assert _usage_observer._RECORDS == {}


# ── Double remove is idempotent ──────────────────────────────────


def test_observer_remove_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling remove() twice does not raise."""
    monkeypatch.delenv(_usage_observer._ENV_VAR, raising=False)

    def cb(**_: Any) -> None:
        pass

    remove = _usage_observer.add_observer(cb)
    remove()
    remove()  # second call should not raise
