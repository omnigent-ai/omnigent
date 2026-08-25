"""Tests for the client-side debug-log sink."""

from __future__ import annotations

import json
import logging

import pytest

from omnigent import debug_logging as dl

_INSERT_URL = (
    "https://3272836215725701.zerobus.us-west-2.cloud.databricks.com"
    "/zerobus/v1/tables/omnigents.omnigent_daniel.omnigent_debug_logs/insert"
)
_TABLE = "omnigents.omnigent_daniel.omnigent_debug_logs"


@pytest.fixture
def _configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_ENV_VAR, "secret")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com/")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        dl.CLIENT_ID_ENV_VAR,
        dl.CLIENT_SECRET_ENV_VAR,
        dl.WORKSPACE_URL_ENV_VAR,
        dl.ENDPOINT_ENV_VAR,
        dl.USER_ID_ENV_VAR,
        dl.PRIMARY_SESSION_ID_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)


def test_disabled_without_env() -> None:
    assert dl.config_from_env() is None


def test_config_parses_table_and_workspace_id(_configured_env: None) -> None:
    config = dl.config_from_env()
    assert config is not None
    assert config.table == _TABLE
    assert config.workspace_id == "3272836215725701"
    # Trailing slash on the workspace URL is trimmed so token minting can append.
    assert config.workspace_url == "https://ws.cloud.databricks.com"


def test_malformed_endpoint_disables(
    monkeypatch: pytest.MonkeyPatch, _configured_env: None
) -> None:
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, "https://host.example.com/no-tables-segment")
    assert dl.config_from_env() is None


def test_authorization_details_splits_catalog_schema_table(_configured_env: None) -> None:
    config = dl.config_from_env()
    assert config is not None
    source = dl._TokenSource(config, client=None)  # type: ignore[arg-type]
    by_type = {
        entry["object_type"]: entry["object_full_path"]
        for entry in json.loads(source._authorization_details())
    }
    assert by_type == {
        "CATALOG": "omnigents",
        "SCHEMA": "omnigents.omnigent_daniel",
        "TABLE": _TABLE,
    }


def test_record_to_row_shape_and_coercions() -> None:
    record = logging.LogRecord(
        "omnigent.runner", logging.INFO, __file__, 10, "hello %s", ("world",), None, func="do_it"
    )
    record.event_name = "turn_started"
    record.attributes = {"model": "claude-opus-4-8", "count": 3, "skip": None}

    row = dl.record_to_row(record, source="runner")

    assert row["message"] == "hello world"
    assert row["source"] == "runner"
    assert row["event_name"] == "turn_started"
    assert row["app_version"] == dl.VERSION
    # TIMESTAMP column wants epoch microseconds as an integer, not an ISO string.
    assert isinstance(row["client_time"], int)
    assert row["client_time"] == int(record.created * 1_000_000)
    # MAP<STRING,STRING>: values coerced to str, null values dropped.
    assert row["attributes"] == {"model": "claude-opus-4-8", "count": "3"}
    assert set(row) == {
        "session_id",
        "turn_id",
        "source",
        "event_name",
        "level",
        "message",
        "client_time",
        "hostname",
        "logger_name",
        "func_name",
        "app_version",
        "stack_trace",
        "attributes",
        "log_id",
        "user_id",
    }


def test_record_to_row_reads_session_id_from_extra() -> None:
    # session_id is passed explicitly at the callsite via extra= and read off
    # the record; there is no ambient contextvar fallback.
    record = logging.LogRecord(
        "omnigent.runner", logging.INFO, __file__, 1, "hi", (), None, func="f"
    )
    record.session_id = "conv_row"
    row = dl.record_to_row(record, source="runner")
    assert row["session_id"] == "conv_row"


def test_record_to_row_null_correlation_without_extra() -> None:
    record = logging.LogRecord("omnigent", logging.INFO, __file__, 1, "hi", (), None)
    row = dl.record_to_row(record, source="server")
    assert row["session_id"] is None
    assert row["turn_id"] is None


def test_record_to_row_without_event_or_attributes() -> None:
    record = logging.LogRecord("omnigent", logging.DEBUG, __file__, 1, "freeform", (), None)
    row = dl.record_to_row(record, source="host")
    assert row["event_name"] is None
    assert row["attributes"] == {}


def test_record_to_row_captures_stack_trace() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "omnigent", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    row = dl.record_to_row(record, source="server")
    assert "ValueError: boom" in (row["stack_trace"] or "")


def test_debug_event_builds_extra() -> None:
    assert dl.debug_event("evt", a=1, b="x") == {
        "event_name": "evt",
        "attributes": {"a": 1, "b": "x"},
    }


def test_debug_event_includes_explicit_correlation() -> None:
    extra = dl.debug_event("evt", session_id="conv_1", turn_id="turn_1", a=1)
    assert extra == {
        "event_name": "evt",
        "attributes": {"a": 1},
        "session_id": "conv_1",
        "turn_id": "turn_1",
    }


def test_debug_event_includes_explicit_user_id() -> None:
    assert dl.debug_event("evt", user_id="u@x") == {
        "event_name": "evt",
        "attributes": {},
        "user_id": "u@x",
    }
    assert "user_id" not in dl.debug_event("evt")


def test_record_to_row_prefers_explicit_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit record.user_id wins over both ambient fallbacks.
    monkeypatch.setenv(dl.USER_ID_ENV_VAR, "env@x")
    record = logging.LogRecord("omnigent", logging.INFO, __file__, 1, "hi", (), None)
    record.user_id = "explicit@x"
    with dl.current_user_id_scope("ctx@x"):
        row = dl.record_to_row(record, source="server")
    assert row["user_id"] == "explicit@x"


def test_record_to_row_falls_back_to_context_var() -> None:
    # No explicit user_id -> the request-scoped ContextVar (server), and only
    # inside the scope.
    record = logging.LogRecord("omnigent", logging.INFO, __file__, 1, "hi", (), None)
    with dl.current_user_id_scope("ctx@x"):
        assert dl.record_to_row(record, source="server")["user_id"] == "ctx@x"
    assert dl.record_to_row(record, source="server")["user_id"] is None


def test_record_to_row_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit user_id and no ContextVar -> the process-constant env (runner/host).
    monkeypatch.setenv(dl.USER_ID_ENV_VAR, "env@x")
    record = logging.LogRecord("omnigent.runner", logging.INFO, __file__, 1, "hi", (), None)
    assert dl.record_to_row(record, source="runner")["user_id"] == "env@x"


def test_current_user_id_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dl.current_user_id() is None
    monkeypatch.setenv(dl.USER_ID_ENV_VAR, "env@x")
    assert dl.current_user_id() == "env@x"
    with dl.current_user_id_scope("ctx@x"):
        assert dl.current_user_id() == "ctx@x"  # ContextVar beats env
    assert dl.current_user_id() == "env@x"
    # Empty values normalize to None and don't mask the lower-priority source.
    with dl.current_user_id_scope(""):
        assert dl.current_user_id() == "env@x"
    monkeypatch.setenv(dl.USER_ID_ENV_VAR, "")
    assert dl.current_user_id() is None


def test_current_user_id_scope_resets() -> None:
    with dl.current_user_id_scope("outer@x"):
        assert dl.current_user_id() == "outer@x"
        with dl.current_user_id_scope("inner@x"):
            assert dl.current_user_id() == "inner@x"
        assert dl.current_user_id() == "outer@x"
    assert dl.current_user_id() is None


def test_emit_revives_closed_uploader(_configured_env: None) -> None:
    # dictConfig() (uvicorn) calls logging.shutdown() → close() on the handler,
    # and os.fork() (the zygote) kills the thread — both leave it attached to
    # root. A subsequent emit must revive it so records keep getting delivered
    # instead of queuing forever.
    config = dl.config_from_env()
    assert config is not None
    sink = dl.ZerobusLogHandler(config, "server")
    try:
        first_thread = sink._thread
        assert first_thread.is_alive()
        # Simulate dictConfig's close() of the handler.
        sink.close()
        assert sink._closed
        assert not first_thread.is_alive()
        # A subsequent record revives the worker (fresh thread) and enqueues.
        record = logging.LogRecord("omnigent.x", logging.INFO, __file__, 1, "hi", (), None)
        sink.emit(record)
        assert not sink._closed
        assert sink._thread is not first_thread
        assert sink._thread.is_alive()
    finally:
        sink.close()


def test_ignored_loggers_are_dropped() -> None:
    # httpx/httpcore records are chatty HTTP-client noise and must be dropped;
    # everything else is kept.
    assert dl._is_ignored_logger("httpx")
    assert dl._is_ignored_logger("httpx._client")
    assert dl._is_ignored_logger("httpcore.connection")
    assert not dl._is_ignored_logger("omnigent.server.routes.sessions")
    assert not dl._is_ignored_logger("runner.native")


def test_attach_is_noop_when_disabled() -> None:
    target = logging.getLogger("test.debug_logging.disabled")
    target.handlers.clear()
    dl.attach_debug_log_sink([target], source="runner", level=logging.INFO)
    assert not any(isinstance(h, dl.ZerobusLogHandler) for h in target.handlers)


def test_runner_primary_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # The host sets this when it spawns a runner for a session; runner-level
    # callsites read it as the best-available attribution. Unset/empty is None.
    monkeypatch.delenv(dl.PRIMARY_SESSION_ID_ENV_VAR, raising=False)
    assert dl.runner_primary_session_id() is None
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "conv_primary")
    assert dl.runner_primary_session_id() == "conv_primary"
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "")
    assert dl.runner_primary_session_id() is None


def test_close_tears_down_captured_worker_not_a_concurrent_revive(
    _configured_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A concurrent emit() can revive the sink (fresh client/thread) during
    # close()'s join. close() must tear down the worker it captured at entry,
    # never the revived one — otherwise it closes a live client out from under
    # the new uploader thread.
    config = dl.config_from_env()
    assert config is not None
    sink = dl.ZerobusLogHandler(config, "server")
    old_client = sink._client
    old_thread = sink._thread
    orig_join = old_thread.join

    def _join_then_revive(timeout: float | None = None) -> None:
        orig_join(timeout)
        sink._closed = False
        sink._start_worker()  # stand in for a concurrent emit() revive

    monkeypatch.setattr(old_thread, "join", _join_then_revive)
    try:
        sink.close()
        assert old_client.is_closed  # the captured worker is torn down
        assert sink._client is not old_client  # the revived worker survives
        assert not sink._client.is_closed
        assert sink._thread is not old_thread
        assert sink._thread.is_alive()
    finally:
        sink._closed = False
        sink.close()


def test_attach_suppresses_handler_init_failure(
    _configured_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Handler construction (httpx client, uploader thread, probe timer) failing
    # must not break configure_process_logging(), per the module's best-effort
    # contract — attach swallows it and stays disabled.
    monkeypatch.setattr(dl, "_active_sink", None)

    def _boom(config: dl.DebugLogConfig, source: str) -> dl.ZerobusLogHandler:
        raise RuntimeError("handler init failed")

    monkeypatch.setattr(dl, "ZerobusLogHandler", _boom)
    target = logging.getLogger("test.debug_logging.initfail")
    target.handlers.clear()
    dl.attach_debug_log_sink([target], source="server", level=logging.INFO)
    assert target.handlers == []
    assert dl._active_sink is None
