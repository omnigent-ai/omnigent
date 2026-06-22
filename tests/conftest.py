"""Shared pytest configuration and fixtures for Omnigent tests."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

try:
    import resource as _resource  # POSIX-only; absent on Windows.
except ImportError:
    _resource = None  # type: ignore[assignment]

# Skip the synchronous api.litellm.ai/model_catalog HTTP fallback during
# tests. Hardened CI runners can't reach the public internet, so every
# workflow startup that misses litellm's local registry would otherwise
# block 5 s on the timeout. ``setdefault`` so a developer can opt
# back in by exporting the var with any other value when exercising the
# catalog code path explicitly.
os.environ.setdefault("OMNIGENT_DISABLE_CATALOG_LOOKUP", "1")

# Pin header mode for the whole suite. Header is the env-unset default,
# but a developer's shell often has OMNIGENT_AUTH_ENABLED=1 set (the
# multi-user opt-in they use to test the login flow locally; the
# pre-rename OMNIGENT_ACCOUNTS_ENABLED is still honored too) — and that
# enable switch would flip the env-unset default to accounts (or oidc, if
# the shell also exports OMNIGENT_OIDC_ISSUER), booting every server in
# multi-user mode and failing loud with "Missing required environment
# variable OMNIGENT_ACCOUNTS_COOKIE_SECRET" / "Authentication required"
# (401). An explicit AUTH_PROVIDER always wins over the enable switch, so
# pinning it here keeps tests deterministic regardless of the ambient
# shell. Accounts/OIDC-specific tests still opt in by monkeypatching the
# vars inside their own fixtures (tests/server/test_accounts.py,
# tests/server/test_oidc.py). Module-level setdefault rather than a fixture
# so subprocess-spawning tests (e2e shells out to `omnigent run`) inherit
# the pin via env.
os.environ.setdefault("OMNIGENT_AUTH_PROVIDER", "header")

# Mark the whole suite a single-user local runtime. Header mode now
# fails closed on a missing X-Forwarded-Email: a request
# without the header is rejected with 401 instead of resolving to the
# shared "local" identity. Test servers and the subprocesses they spawn
# have no proxy injecting the header and drive headerless traffic
# (runner-status polls, REPL turns, session CRUD), so they need the
# single-user fallback that the managed local-server spawn paths set in
# production. Pinned here (not per-fixture) so every spawned server
# inherits it via os.environ — the same chokepoint as the header pin
# above. Tests that specifically verify the strict (deployed
# multi-user) posture opt OUT by constructing
# UnifiedAuthProvider(source="header", local_single_user=False) or by
# monkeypatch.delenv-ing this var.
os.environ.setdefault("OMNIGENT_LOCAL_SINGLE_USER", "1")

from omnigent.db.utils import _engine_cache, _engine_lock, get_or_create_engine
from tests import _model_pools

pytest_plugins = ["tests._token_usage"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Translate ``@pytest.mark.llm_flaky`` into a rerunfailures ``flaky``
    marker; each rerun resolves to a different model via
    :mod:`tests._model_pools` rotation.
    """
    for item in items:
        llm_flaky = item.get_closest_marker("llm_flaky")
        if llm_flaky is not None:
            # WARNING: never llm_flaky a heavy e2e test that can hit the
            # CI --timeout=180 cap: thread-timeout kill + loadscope +
            # rerun can crash the whole xdist shard.
            reruns = int(llm_flaky.kwargs.get("reruns", 2))
            delay = int(llm_flaky.kwargs.get("reruns_delay", 1))
            item.add_marker(pytest.mark.flaky(reruns=reruns, reruns_delay=delay))


# Tmpdir owned by *this* process (master or single worker) for the
# isolated MLflow SQLite store. None when we honored a caller-supplied
# `MLFLOW_TRACKING_URI` and didn't create one ourselves. Cleaned up
# in `pytest_unconfigure`.
_OWNED_MLFLOW_DIR: str | None = None

# Per-worker progress log path; resolved from
# ``PYTEST_PROGRESS_LOG_DIR`` in :func:`pytest_configure`. ``None``
# when env var is unset (local dev).
_PROGRESS_LOG_PATH: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Isolate MLflow's tracking SQLite per pytest-xdist worker.

    Without this, every worker reads/writes the same default MLflow
    SQLite store. Two workers concurrently running alembic migrations
    on first init race on `_alembic_tmp_*` table creation and one
    crashes with `OperationalError: table already exists`.

    Workers inherit the master process's environment, so the master's
    `MLFLOW_TRACKING_URI` would propagate to every worker if we just
    set it once. Instead we always set it in the worker (overriding
    the inherited master value). Honor a caller-supplied URI only when
    we're running outside xdist (no `PYTEST_XDIST_WORKER`).

    Also resolves the per-worker progress log path used by the
    test-start/finish hooks below.
    """
    global _OWNED_MLFLOW_DIR, _PROGRESS_LOG_PATH
    worker_id = os.environ.get("PYTEST_XDIST_WORKER")
    if worker_id is None:
        # Master / serial run: respect caller-supplied URI.
        if "MLFLOW_TRACKING_URI" not in os.environ:
            worker_id = "master"
            tmpdir = tempfile.mkdtemp(prefix=f"mlflow-{worker_id}-")
            _OWNED_MLFLOW_DIR = tmpdir
            os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{tmpdir}/mlflow.db"
    else:
        # Workers always get their own DB; master's URI propagates
        # otherwise and every worker collides.
        tmpdir = tempfile.mkdtemp(prefix=f"mlflow-{worker_id}-")
        _OWNED_MLFLOW_DIR = tmpdir
        os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{tmpdir}/mlflow.db"

    log_dir = os.environ.get("PYTEST_PROGRESS_LOG_DIR")
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
        _PROGRESS_LOG_PATH = os.path.join(log_dir, f"progress-{worker}.log")

    _run_test_environment_guardrails(config)


def _run_test_environment_guardrails(config: pytest.Config) -> None:
    """Enforce test-environment guardrails at session start.

    Hard-fail: :func:`check_test_environment` raises on anything that
    looks like a real (non-test) DB or a base URL aimed at a dev/prod host
    or port. Set ``OMNIGENT_DISABLE_TEST_GUARDRAILS=1`` to temporarily
    downgrade violations to warn-only for deliberate integration runs.

    The resolved DB URI mirrors how a run would pick one: an explicit
    ``OMNIGENT_DATABASE_URI`` wins (so pointing the suite at a real DB
    warns loudly), else we fall back to the per-worker tmp MLflow SQLite
    set just above — a representative throwaway DB that passes cleanly.
    """
    from omnigent.testing.guardrails import check_test_environment

    db_uri = os.environ.get("OMNIGENT_DATABASE_URI") or os.environ.get("MLFLOW_TRACKING_URI", "")
    base_url = config.getoption("--omnigent-server-url", default=None)
    check_test_environment(db_uri=db_uri, base_url=base_url, warn_only=False)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Remove the per-worker MLflow tmpdir created in pytest_configure.

    `ignore_errors=True` because MLflow / SQLite may still hold open
    file handles at session end; on POSIX the unlink-while-open
    behavior is fine, but on Windows or NFS-style filesystems it can
    surface as a noisy traceback. We don't want a cleanup hiccup to
    fail the test session.
    """
    if _OWNED_MLFLOW_DIR is not None:
        shutil.rmtree(_OWNED_MLFLOW_DIR, ignore_errors=True)


# Per-worker progress logger: fsync'd START/END lines so a
# wedged worker leaves the last test on disk. END lines also carry
# peak RSS; `pytest_terminal_summary` prints the top tests by RSS
# delta to flag OOM-shaped hangs.


def _process_peak_rss_kb() -> int | None:
    """Peak RSS in KB. None on Windows. ru_maxrss is bytes on macOS."""
    if _resource is None:
        return None
    rss = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        rss //= 1024
    return int(rss)


_TEST_RSS_RECORDS: list[tuple[str, int]] = []


def _write_progress_event(event: str, nodeid: str, rss_kb: int | None = None) -> None:
    """Append ``<timestamp>\\t<event>\\t<nodeid>[\\t<rss_kb>]\\n`` and fsync."""
    if _PROGRESS_LOG_PATH is None:
        return
    line = f"{time.time():.3f}\t{event}\t{nodeid}"
    if rss_kb is not None:
        line += f"\t{rss_kb}"
    line += "\n"
    with open(_PROGRESS_LOG_PATH, "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    _write_progress_event("START", nodeid)


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int | None, str]) -> None:
    rss_kb = _process_peak_rss_kb()
    if rss_kb is not None:
        _TEST_RSS_RECORDS.append((nodeid, rss_kb))
    _write_progress_event("END", nodeid, rss_kb=rss_kb)
    # Clear so resolutions outside any test pass through unchanged.
    _model_pools.set_current_test(None)


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Stamp the model-pool context for this test attempt.

    Runs once per rerunfailures attempt (``item.execution_count`` is
    bumped before each), so reruns rotate to a different model.

    :param item: The test item about to run.
    """
    # execution_count is 1-based and absent without a flaky marker.
    attempt = getattr(item, "execution_count", 1) - 1
    _model_pools.set_current_test(
        item.nodeid,
        attempt=attempt,
        pinned=item.get_closest_marker("model_pinned") is not None,
    )


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    """Top tests by peak-RSS delta -- per worker."""
    if len(_TEST_RSS_RECORDS) < 2:
        return
    baseline = 0
    deltas: list[tuple[str, int, int]] = []
    for nodeid, rss_kb in _TEST_RSS_RECORDS:
        delta = rss_kb - baseline
        if delta > 0:
            deltas.append((nodeid, rss_kb, delta))
        baseline = rss_kb
    if not deltas:
        return
    deltas.sort(key=lambda row: row[2], reverse=True)
    terminalreporter.write_sep("=", "Top tests by peak-RSS delta")
    for nodeid, rss_kb, delta_kb in deltas[:20]:
        terminalreporter.write_line(
            f"+{delta_kb / 1024:7.1f} MB  (now {rss_kb / 1024:8.1f} MB)  {nodeid}"
        )


def pytest_addoption(parser):
    """Register CLI flags consumed across the suite.

    :param parser: the pytest option parser.
    """
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires real LLM credentials)",
    )
    parser.addoption(
        "--model",
        action="store",
        default="databricks-claude-sonnet-4-6",
        help="Model name for integration tests (default: databricks-claude-sonnet-4-6)",
    )
    parser.addoption(
        "--harness",
        action="store",
        default="databricks",
        help=(
            "Harness type: 'databricks', 'claude-sdk', 'open-responses', "
            "'openai-agents', or 'codex' (default: databricks)"
        ),
    )
    parser.addoption(
        "--profile",
        action="store",
        default="",
        help="Databricks config profile for integration tests",
    )
    parser.addoption(
        "--llm-api-key",
        action="store",
        default=None,
        help=(
            "LLM API key for integration / e2e tests. Required when running "
            "tests/e2e/ (those tests assert it's non-None via the live_server "
            "fixture); optional for tests/frontends/ integration tests "
            "(those skip gracefully when the key is absent)."
        ),
    )
    parser.addoption(
        "--omnigent-server-url",
        action="store",
        default=None,
        help=(
            "Base URL of an externally-managed `omnigent.cli server` to run "
            "e2e tests against, e.g. `http://localhost:8080`. When set, "
            "server-fixtures skip the spawn step and yield this URL. Useful "
            "for iterating on tests against a long-running dev server "
            "(server logs stay visible, breakpoints stick across runs). When "
            "unset, fixtures spawn a fresh subprocess as before. The fixture "
            "consumer is responsible for ensuring the external server is "
            "configured with the credentials/profile the test needs."
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_claude_native_state(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Redirect claude-native client-side persistent state to a tmp dir.

    The ``omnigent claude`` wrapper writes per-conversation
    launch state (the cwd a session was created in) under
    ``~/.omnigent/claude-native/<hash>/launch.json``. Any test
    that drives the wrapper -- directly or indirectly via test
    fakes that invoke its helpers -- would otherwise write to the
    developer's real ``~/.omnigent`` directory and pollute it
    across test runs.

    The state module honors :data:`OMNIGENT_CLAUDE_NATIVE_STATE_DIR`
    as a root override. ``autouse=True`` because the alternative
    (opt-in fixture per test) leaves us one missed test away from
    re-polluting the user's home; the override has no side effects
    on tests that don't touch claude-native state at all.

    Using ``tmp_path_factory.mktemp`` rather than the request-scoped
    ``tmp_path`` so the override fires before any other fixture or
    test body picks up the env -- ``tmp_path`` materializes lazily
    per test, and we want the redirect to be in effect from the
    moment the test session starts.

    :param tmp_path_factory: Pytest's session-scoped temp factory.
    :param monkeypatch: Pytest monkeypatch fixture; auto-restores
        the env var at teardown.
    :returns: None.
    """
    state_dir = tmp_path_factory.mktemp("claude-native-state")
    monkeypatch.setenv("OMNIGENT_CLAUDE_NATIVE_STATE_DIR", str(state_dir))


@pytest.fixture(autouse=True)
def _isolate_codex_native_state(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Redirect codex-native client-side persistent state to a tmp dir.

    The ``omnigent codex`` wrapper writes per-conversation launch
    state under ``~/.omnigent/codex-native/<hash>/launch.json``.
    Tests that drive the wrapper should never write to or read from
    the developer's real persistent resume state.

    The state module honors :data:`OMNIGENT_CODEX_NATIVE_STATE_DIR`
    as a root override. ``autouse=True`` keeps test isolation as the
    default even for indirect wrapper tests that do not explicitly
    request a Codex state fixture.

    :param tmp_path_factory: Pytest's session-scoped temp factory.
    :param monkeypatch: Pytest monkeypatch fixture; auto-restores
        the env var at teardown.
    :returns: None.
    """
    state_dir = tmp_path_factory.mktemp("codex-native-state")
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(state_dir))


@pytest.fixture()
def db_uri(tmp_path: Path) -> str:
    """
    Return a test database URI backed by a file in tmp_path.

    Uses get_or_create_engine() which runs Alembic migrations on first
    engine creation — same path as production. File-based (not in-memory)
    because DBOS needs a real file to create its system tables. Cleaned
    up after each test.

    :param tmp_path: pytest tmp_path fixture (per-test temp dir).
    :returns: a ``sqlite:///…`` URI string.
    """
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    # Creates the engine AND runs migrations (once, cached).
    engine = get_or_create_engine(uri)

    yield uri

    with _engine_lock:
        _engine_cache.pop(uri, None)
    engine.dispose()


@pytest.fixture()
def lowered_idle_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Lower the terminal-idle thresholds so watcher tests don't burn
    ten real seconds per assertion.

    Mirrors :class:`tests.unit.inner.test_terminal.TestTerminalIdleNotifications.setUp`
    from the legacy class-based suite. Defaults marker substrings
    to empty so tests that don't exercise the marker track see
    pure diff semantics regardless of the production list — tests
    that DO exercise markers can override locally with another
    ``monkeypatch.setattr``.

    Shared between ``tests/inner/test_terminal.py`` (threaded /
    asyncio watcher mechanics) and
    ``tests/tools/builtins/test_sys_terminal.py`` (AP-side
    ``notify_when_idle`` end-to-end). Promoted to root conftest
    rather than duplicated per file so the threshold values stay
    in lockstep — a future tuning change touches one location.

    :param monkeypatch: Pytest's monkeypatch fixture; auto-restores
        the original constants at teardown.
    """
    from omnigent.inner import terminal as terminal_module

    monkeypatch.setattr(terminal_module, "_IDLE_THRESHOLD_SECONDS", 0.4)
    monkeypatch.setattr(terminal_module, "_IDLE_POLL_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(terminal_module, "_IDLE_MARKER_SUBSTRINGS", [])
    monkeypatch.setattr(terminal_module, "_IDLE_MARKER_THRESHOLD_SECONDS", 0.4)
