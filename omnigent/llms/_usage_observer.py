"""Per-turn token usage notification + auto-recording.

A single notification point that LLM call paths invoke when a turn
completes with usage data. Two consumers:

1. **In-process subscribers** via :func:`add_observer` (used by tests
   and diagnostics).
2. **Auto-recorder** that activates whenever
   ``OMNIGENT_TOKEN_USAGE_JSON=<path>`` is set in the environment.
   The recorder accumulates each :func:`notify` call into a per-test
   bucket and rewrites the JSON file after every recorded call
   (write-through). Because subprocesses inherit the env var, harness
   subprocesses and server subprocesses each produce their own file;
   the workflow-side aggregator merges them by nodeid at the end of
   the run. The write is per-call rather than at-exit because CI
   tears those subprocesses down with SIGTERM/SIGKILL, which never
   runs ``atexit`` hooks; an exit-time writer loses everything.

Filenames include the xdist worker id (if any) plus the process pid so
parallel writers never collide.

Wired from:

- :meth:`omnigent.llms.client._ResponsesNamespace.create` (in-process
  LLM client).
- the harness HTTP executor
  when a ``response.completed`` SSE event carries usage forwarded from
  a harness subprocess.

Test attribution: the pytest plugin (``tests._token_usage``) calls
:func:`set_current_test` before each test, so notifications fired in
the parent test process get keyed under the right nodeid. The same
call also mirrors the nodeid into a per-worker sidecar file next to
the output path; subprocesses spawned by the test process (server,
runner, harness) inherit both ``OMNIGENT_TOKEN_USAGE_JSON`` and
``PYTEST_XDIST_WORKER``, so they resolve the same sidecar and
attribute their notifications to the running test too. Only when no
sidecar exists (no test running, or a non-pytest parent) does usage
land under ``"<no-test>"``. Sidecar attribution is wall-clock-based
and diagnostic-grade: a turn that completes after its test already
finished is credited to the next test or ``"<no-test>"``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, TypedDict

logger = logging.getLogger(__name__)

_ENV_VAR = "OMNIGENT_TOKEN_USAGE_JSON"


class UsageObserver(Protocol):
    def __call__(
        self,
        *,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None: ...


class _ModelUsage(TypedDict):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    calls: int


class _UsageBucket(_ModelUsage):
    models: list[str]
    by_model: dict[str, _ModelUsage]


_OBSERVERS: list[UsageObserver] = []
_RECORDS: dict[str, _UsageBucket] = {}
_CURRENT_NODEID: str | None = None
# Serializes mutations of ``_RECORDS`` so notify() calls from concurrent
# threads (or asyncio executors backed by threads) don't lose updates on
# the non-atomic ``bucket[k] += n`` read-modify-write.
_RECORDS_LOCK = threading.Lock()


def set_current_test(nodeid: str | None) -> None:
    """Mark which test is active for subsequent :func:`notify` calls.

    Called by the pytest plugin's ``pytest_runtest_protocol`` hook so
    that in-process LLM calls get attributed to the running test. When
    the recorder env var is set, the nodeid is also published to the
    per-worker sidecar file (see :func:`_current_test_path`) so server
    / runner / harness subprocesses spawned by the test process can
    attribute their own notifications; ``None`` removes the sidecar so
    between-test usage falls back to ``"<no-test>"``.

    :param nodeid: The running test's pytest nodeid, e.g.
        ``"tests/e2e/test_sub_agents.py::test_spawn"``, or ``None``
        when no test is active.
    """
    global _CURRENT_NODEID
    _CURRENT_NODEID = nodeid
    path = _current_test_path()
    if path is None:
        return
    try:
        if nodeid is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic publish: a subprocess reading mid-update sees either
        # the previous nodeid or the new one, never a truncated line.
        tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
        tmp.write_text(nodeid)
        os.replace(tmp, path)
    except OSError:
        logger.exception("failed to update current-test sidecar at %s", path)


def add_observer(callback: UsageObserver) -> Callable[[], None]:
    """Register *callback* to receive usage notifications.

    Returns a zero-arg function that removes the callback. Safe to call
    twice (idempotent removal).
    """
    _OBSERVERS.append(callback)

    def remove() -> None:
        with contextlib.suppress(ValueError):
            _OBSERVERS.remove(callback)

    return remove


def notify_from_dict(
    *,
    model: str | None,
    usage: Mapping[str, object] | None,
) -> None:
    """Convenience wrapper for callers that already have a ``usage`` dict.

    Inner executors yield ``TurnComplete(..., usage={"input_tokens": N,
    "output_tokens": M, "total_tokens": T, ...})``. This unpacks the
    standard keys and calls :func:`notify`. ``None`` and empty dicts are
    no-ops.
    """
    if not isinstance(usage, Mapping):
        return
    notify(
        model=model,
        input_tokens=_usage_int(usage.get("input_tokens")),
        output_tokens=_usage_int(usage.get("output_tokens")),
        total_tokens=_usage_int(usage.get("total_tokens")),
    )


def _usage_int(value: object) -> int:
    if isinstance(value, (int, float, str)):
        return int(value or 0)
    return 0


def notify(
    *,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
) -> None:
    """Dispatch usage to observers and (if enabled) the auto-recorder.

    Never raises: a misbehaving observer is logged and skipped.
    """
    if _ENV_VAR in os.environ and (input_tokens or output_tokens or total_tokens):
        _record(model, input_tokens, output_tokens, total_tokens)
        _write_records()
    for cb in list(_OBSERVERS):
        try:
            cb(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        except Exception:
            logger.exception("usage observer raised; continuing")


def _current_test_path() -> Path | None:
    """Sidecar file holding the currently-running test's nodeid.

    Derived from the ``OMNIGENT_TOKEN_USAGE_JSON`` base path plus the
    xdist worker id, both of which spawned subprocesses inherit, so a
    worker's test process and its server / runner / harness
    subprocesses all resolve the same file while parallel workers
    never collide. ``.txt`` keeps it out of the aggregator's and the
    artifact-upload ``tokens*.json`` globs.

    :returns: The sidecar path, e.g. ``/tmp/.../tokens-current-test-gw0.txt``,
        or ``None`` when the recorder env var is unset.
    """
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        return None
    base = Path(raw)
    # "main" labels the non-xdist single-process run; it only needs to
    # be stable between the test process and its subprocesses (both see
    # the same absent PYTEST_XDIST_WORKER), not globally unique.
    worker = os.environ.get("PYTEST_XDIST_WORKER") or "main"
    return base.with_name(f"{base.stem}-current-test-{worker}.txt")


def _current_test_from_sidecar() -> str | None:
    """Read the test nodeid published by the parent test process.

    :returns: The nodeid from the sidecar file, or ``None`` when no
        sidecar exists (no test running, recorder env var unset, or a
        non-pytest parent process).
    """
    path = _current_test_path()
    if path is None:
        return None
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    # An empty file carries no nodeid; normalize to None so the caller
    # falls through to "<no-test>" instead of keying a "" bucket.
    return text or None


def _record(model: str | None, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
    """Accumulate one usage notification into the current test's bucket.

    In the pytest process ``_CURRENT_NODEID`` wins; subprocesses
    (where it is always ``None``) fall back to the sidecar published
    by the test process, then to ``"<no-test>"``.
    """
    key = _CURRENT_NODEID or _current_test_from_sidecar() or "<no-test>"
    with _RECORDS_LOCK:
        bucket = _RECORDS.setdefault(
            key,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
                "models": [],
                "by_model": {},
            },
        )
        bucket["input_tokens"] += input_tokens
        bucket["output_tokens"] += output_tokens
        bucket["total_tokens"] += total_tokens
        bucket["calls"] += 1
        if model and model not in bucket["models"]:
            bucket["models"].append(model)
        # Per-model breakdown for the aggregator's calls-per-model tally.
        per_model = bucket["by_model"].setdefault(
            model or "<unknown>",
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0},
        )
        per_model["input_tokens"] += input_tokens
        per_model["output_tokens"] += output_tokens
        per_model["total_tokens"] += total_tokens
        per_model["calls"] += 1


def _output_path() -> Path | None:
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        return None
    base = Path(raw)
    parts: list[str] = []
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        parts.append(worker)
    parts.append(f"pid{os.getpid()}")
    suffix = "-".join(parts)
    return base.with_name(f"{base.stem}-{suffix}{base.suffix}")


def _write_records() -> None:
    """Rewrite the per-process JSON file from the accumulated records.

    Called by :func:`notify` after every recorded call so the on-disk
    file is always current; deferring to ``atexit`` would lose all data
    when CI tears the process down with SIGTERM/SIGKILL (signals never
    run exit hooks). The temp-file + ``os.replace`` write means a kill
    mid-write leaves the previous complete file, never a truncated one.
    Held under ``_RECORDS_LOCK`` end to end so concurrent ``notify()``
    threads can't replace a newer snapshot with an older one.
    """
    path = _output_path()
    with _RECORDS_LOCK:
        if path is None or not _RECORDS:
            return
        totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0}
        totals_by_model: dict[str, dict[str, int]] = {}
        for bucket in _RECORDS.values():
            totals["input_tokens"] += bucket["input_tokens"]
            totals["output_tokens"] += bucket["output_tokens"]
            totals["total_tokens"] += bucket["total_tokens"]
            totals["calls"] += bucket["calls"]
            for model, per_model in bucket["by_model"].items():
                model_totals = totals_by_model.setdefault(
                    model,
                    {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "calls": 0},
                )
                model_totals["input_tokens"] += per_model["input_tokens"]
                model_totals["output_tokens"] += per_model["output_tokens"]
                model_totals["total_tokens"] += per_model["total_tokens"]
                model_totals["calls"] += per_model["calls"]
        payload = json.dumps(
            {"totals": totals, "totals_by_model": totals_by_model, "by_test": _RECORDS},
            indent=2,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Path is per-pid, so only same-process threads contend on
            # the temp name; the lock serializes them.
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(payload)
            os.replace(tmp, path)
        except OSError:
            logger.exception("failed to write token-usage records to %s", path)


__all__ = [
    "UsageObserver",
    "add_observer",
    "notify",
    "notify_from_dict",
    "set_current_test",
]
