"""
Pytest fixtures shared across ``tests/inner/`` test files.
"""

from __future__ import annotations

import asyncio
import faulthandler
import gc
import inspect
import logging
import os
import pathlib
import sys
import time

import pytest
import pytest_asyncio

from tests import _model_pools

# Diagnostic: dump every thread's stack every 90s. The dispatcher's
# stderr lands in the workflow log directly, but xdist workers route
# their stderr through execnet -- faulthandler writing to sys.stderr
# in a worker silently disappears. Route worker dumps to a file in
# PYTEST_PROGRESS_LOG_DIR (uploaded as an artifact by integration.yml)
# so each gw{N}'s stack frames are recoverable post-mortem.
_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
_log_dir_env = os.environ.get("PYTEST_PROGRESS_LOG_DIR")
if _log_dir_env:
    _log_dir = pathlib.Path(_log_dir_env)
    _log_dir.mkdir(parents=True, exist_ok=True)
    _faulthandler_file = open(  # noqa: SIM115  -- kept open for the process lifetime
        _log_dir / f"faulthandler-{_worker}.log", "w", buffering=1
    )
else:
    _faulthandler_file = sys.stderr
faulthandler.dump_traceback_later(90, repeat=True, file=_faulthandler_file)


# Diagnostic: dump asyncio.all_tasks() every 60s during async tests.
# faulthandler shows C frames for live threads, but async coroutines
# that have yielded sit in the loop's task list without any C frame --
# the main thread's stack just shows ``select()``. To see which awaits
# are stuck, schedule a periodic ``loop.call_later`` from inside an
# autouse fixture and dump every Task's stack.
if _log_dir_env:
    _asyncio_tasks_path: pathlib.Path | None = (
        pathlib.Path(_log_dir_env) / f"asyncio-tasks-{_worker}.log"
    )
else:
    _asyncio_tasks_path = None


def _coro_chain(coro: object) -> list[tuple[str, str, int]]:
    """Walk ``cr_await`` to list the full await chain for a coroutine.

    ``Task.print_stack`` only shows the outermost coroutine's frame --
    nested ``await`` calls aren't traversed. Walk the chain manually so
    we can see ``test -> session.call -> run_single_turn -> ...`` all
    the way to the actual blocking await.
    """
    frames: list[tuple[str, str, int]] = []
    seen: set[int] = set()
    cur: object | None = coro
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        # coroutine: cr_frame / cr_await
        # async generator: ag_frame / ag_await
        # generator-based coro: gi_frame / gi_yieldfrom
        frame = (
            getattr(cur, "cr_frame", None)
            or getattr(cur, "ag_frame", None)
            or getattr(cur, "gi_frame", None)
        )
        name = getattr(cur, "__qualname__", type(cur).__name__)
        if frame is not None:
            frames.append((name, frame.f_code.co_filename, frame.f_lineno))
        nxt = (
            getattr(cur, "cr_await", None)
            or getattr(cur, "ag_await", None)
            or getattr(cur, "gi_yieldfrom", None)
        )
        # async-generator __anext__ exposes its inner coroutine via
        # ``ag_await`` after the first send; before that, the iterator
        # protocol may surface a ``_GeneratorWrapper``-like object that
        # wraps the next coroutine via the same ``cr_await`` chain.
        cur = nxt
    return frames


def _dump_asyncio_tasks() -> None:
    if _asyncio_tasks_path is None:
        return
    try:
        with open(_asyncio_tasks_path, "a") as f:
            f.write(f"\n--- asyncio.all_tasks() at {time.time():.3f} ---\n")
            for task in asyncio.all_tasks():
                f.write(f"\nTask {task!r}\n")
                for name, filename, lineno in _coro_chain(task.get_coro()):
                    f.write(f"  {name} ({filename}:{lineno})\n")
            # ``cr_await`` doesn't traverse through ``async for`` --
            # the awaited ``async_generator_asend`` hides the wrapped
            # generator. Sweep gc for every live async generator and
            # walk its own await chain so suspended frames inside
            # ``stream_turn``, ``executor.run_turn``, etc. are visible.
            f.write("\n--- live async generators ---\n")
            for obj in gc.get_objects():
                if not inspect.isasyncgen(obj):
                    continue
                if obj.ag_frame is None:
                    continue
                f.write(f"\nAsyncGen {obj!r}\n")
                for name, filename, lineno in _coro_chain(obj):
                    f.write(f"  {name} ({filename}:{lineno})\n")
    except Exception as exc:  # pragma: no cover - diagnostic, never fail
        sys.stderr.write(f"_dump_asyncio_tasks failed: {exc!r}\n")


@pytest_asyncio.fixture(autouse=True)
async def _hang_diagnostic_task_dumper() -> asyncio.AsyncGenerator[None, None]:
    """Schedule a periodic asyncio task-stack dump while a test runs."""
    if _asyncio_tasks_path is None:
        yield
        return
    loop = asyncio.get_running_loop()
    handle: asyncio.TimerHandle | None = None

    def _tick() -> None:
        nonlocal handle
        _dump_asyncio_tasks()
        handle = loop.call_later(60.0, _tick)

    handle = loop.call_later(60.0, _tick)
    try:
        yield
    finally:
        if handle is not None:
            handle.cancel()


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "model_name" not in metafunc.fixturenames:
        return
    marker = metafunc.definition.get_closest_marker("model")
    if marker and len(marker.args) > 1:
        metafunc.parametrize("model_name", marker.args, indirect=True)


@pytest.fixture
def model_name(request: pytest.FixtureRequest) -> str:
    """Resolve the model for this test: param > ``model`` marker > ``--model``.

    Explicit choices skip :mod:`tests._model_pools` spreading but still
    rotate on ``llm_flaky`` reruns; the ``--model`` default is also
    spread when ``OMNIGENT_TEST_MODEL_SPREAD`` is on.

    :param request: Pytest fixture request for the consuming test.
    :returns: The model name to use, e.g. ``"databricks-claude-sonnet-4-6"``.
    """
    if hasattr(request, "param") and request.param is not None:
        return _model_pools.resolve_model(request.param, spread=False)
    marker = request.node.get_closest_marker("model")
    if marker and marker.args:
        return _model_pools.resolve_model(marker.args[0], spread=False)
    return _model_pools.resolve_model(request.config.getoption("--model"))


# Harnesses whose model traffic flows through OpenAI's cyber_policy
# content classifier: gpt-5-4-mini (openai-agents) and gpt-5-5
# (codex). claude-sdk routes through Anthropic and is unaffected.
_OPENAI_CYBER_POLICY_HARNESSES = frozenset({"openai-agents", "codex"})


@pytest.fixture(autouse=True)
def _capture_codex_executor_diag(caplog: pytest.LogCaptureFixture) -> None:
    """Lower threshold so codex executor diag logs appear in junit failure reports."""
    caplog.set_level(logging.INFO, logger="omnigent.inner.codex_executor")


@pytest.fixture(autouse=True)
def _skip_openai_cyber_policy_blocked(request: pytest.FixtureRequest) -> None:
    """
    Skip tests marked ``openai_cyber_policy_blocked`` on harnesses
    that route through OpenAI's content classifier.
    """
    if request.node.get_closest_marker("openai_cyber_policy_blocked") is None:
        return
    if request.config.getoption("--harness") not in _OPENAI_CYBER_POLICY_HARNESSES:
        return
    pytest.skip("OpenAI cyber_policy classifier blocks this prompt")


def pytest_configure(config: pytest.Config) -> None:
    """Register the inner-suite's custom pytest markers.

    :param config: Pytest config object.
    """
    config.addinivalue_line(
        "markers",
        "openai_cyber_policy_blocked: skip on harnesses routed through OpenAI's "
        "content classifier (openai-agents, codex)",
    )
    config.addinivalue_line(
        "markers",
        "codex_async_handoff_flaky: marks a two-turn async-handoff test that "
        "intermittently flakes on codex; reruns up to 2x when --harness=codex.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """
    Attach a ``flaky(reruns=2)`` marker to every test bearing the
    ``codex_async_handoff_flaky`` marker, but only when the suite is
    running on the codex harness. claude-sdk and openai-agents have
    never reproduced this flake, so a failure there should fail loud
    on the first attempt rather than retry and potentially mask a real
    regression.

    :param config: Pytest config object.
    :param items: The collected test items pytest is about to run.
    """
    if config.getoption("--harness") != "codex":
        return
    for item in items:
        if item.get_closest_marker("codex_async_handoff_flaky") is None:
            continue
        item.add_marker(pytest.mark.flaky(reruns=2, reruns_delay=1))
