"""Tests for the client-side debug-log sink."""

from __future__ import annotations

import json
import logging
import threading

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
        dl.ORIGIN_WORKSPACE_ID_ENV_VAR,
        dl.APP_NAME_ENV_VAR,
        dl.SERVER_URL_ENV_VAR,
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
        "workspace_id",
        "app_name",
    }


def test_record_to_row_redacts_token_like_text_fields() -> None:
    secret = "aB3" + "xY7" * 12
    try:
        raise ValueError(f"credential={secret}")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "omnigent.runner",
            logging.ERROR,
            __file__,
            10,
            "request failed: %s",
            (secret,),
            sys.exc_info(),
            func="do_it",
        )
    record.attributes = {"upstream_response": secret, "attempt": 3}

    row = dl.record_to_row(record, source="runner")

    attributes = row["attributes"]
    assert isinstance(attributes, dict)
    assert secret not in str(row["message"])
    assert secret not in str(row["stack_trace"])
    assert secret not in attributes["upstream_response"]
    assert attributes["attempt"] == "3"
    assert "[REDACTED]" in str(row)


def test_record_to_row_reads_session_id_from_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    # session_id is passed explicitly at the callsite via extra= and read off
    # the record. There is deliberately no ambient request-scoped fallback; an
    # explicit id also wins over the runner-primary env fallback.
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "conv_primary")
    record = logging.LogRecord(
        "omnigent.runner", logging.INFO, __file__, 1, "hi", (), None, func="f"
    )
    record.session_id = "conv_row"
    row = dl.record_to_row(record, source="runner")
    assert row["session_id"] == "conv_row"


def test_record_to_row_session_id_falls_back_to_primary_on_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a runner the primary-session env is set, so an unthreaded log is
    # attributed to the primary (parent) conversation. A co-located subagent's
    # unthreaded log can be mis-attributed to the parent — an accepted trade-off.
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "conv_primary")
    record = logging.LogRecord("omnigent.runner", logging.INFO, __file__, 1, "hi", (), None)
    assert dl.record_to_row(record, source="runner")["session_id"] == "conv_primary"


def test_record_to_row_null_correlation_without_extra() -> None:
    # The server never sets the primary-session env (the _clear_env fixture
    # mirrors that), so an unthreaded server log stays null rather than
    # borrowing another concurrent request's id — the deliberate no-ambient-
    # fallback property.
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


def test_record_to_row_session_id_falls_back_to_ambient_scope() -> None:
    # The server middleware binds an ambient session id for a session-scoped
    # request; unthreaded records inherit it, and only inside the scope.
    record = logging.LogRecord("omnigent", logging.INFO, __file__, 1, "hi", (), None)
    with dl.current_session_id_scope("conv_ambient"):
        assert dl.record_to_row(record, source="server")["session_id"] == "conv_ambient"
    assert dl.record_to_row(record, source="server")["session_id"] is None


def test_record_to_row_prefers_explicit_session_id_over_ambient() -> None:
    # An explicit record.session_id wins over the ambient request-scoped value.
    record = logging.LogRecord("omnigent", logging.INFO, __file__, 1, "hi", (), None)
    record.session_id = "conv_explicit"
    with dl.current_session_id_scope("conv_ambient"):
        assert dl.record_to_row(record, source="server")["session_id"] == "conv_explicit"


def test_record_to_row_session_ambient_beats_primary_but_explicit_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Priority on a runner: explicit extra > ambient scope > primary-session env.
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "conv_primary")
    record = logging.LogRecord("omnigent", logging.INFO, __file__, 1, "hi", (), None)
    with dl.current_session_id_scope("conv_ambient"):
        assert dl.record_to_row(record, source="runner")["session_id"] == "conv_ambient"
    assert dl.record_to_row(record, source="runner")["session_id"] == "conv_primary"


def test_request_audit_attrs_accumulate_and_reset() -> None:
    # Outside a request (no bag) add_audit_attrs is a no-op and the current
    # attrs are empty.
    assert dl.current_request_audit_attrs() == {}
    dl.add_audit_attrs(event_type="message")
    assert dl.current_request_audit_attrs() == {}
    # After a per-request reset, handlers accumulate attrs (coerced to str,
    # None dropped) that the middleware later reads.
    dl.reset_request_audit_attrs()
    dl.add_audit_attrs(event_type="message", item_id="it_1", ignored=None)
    dl.add_audit_attrs(count=3)
    assert dl.current_request_audit_attrs() == {
        "event_type": "message",
        "item_id": "it_1",
        "count": "3",
    }


def test_mark_request_audit_suppressed_sets_reserved_flag() -> None:
    # Inside a request it sets the reserved _suppress key the middleware reads
    # to skip the envelope end-event.
    dl.reset_request_audit_attrs()
    assert "_suppress" not in dl.current_request_audit_attrs()
    dl.mark_request_audit_suppressed()
    assert dl.current_request_audit_attrs()["_suppress"] == "1"


def test_current_session_id_scope_resets() -> None:
    assert dl.current_session_id() is None
    dl.set_current_session_id("outer")
    try:
        with dl.current_session_id_scope("inner"):
            assert dl.current_session_id() == "inner"
        assert dl.current_session_id() == "outer"
    finally:
        dl.set_current_session_id(None)


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


def test_custom_send_receives_prepared_rows_without_zerobus_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    batches: list[list[dl.DebugLogRow]] = []
    delivered = threading.Event()

    def send(batch: list[dl.DebugLogRow]) -> None:
        batches.append(batch)
        delivered.set()

    sink = dl.DebugLogHandler("integration", send)
    try:
        record = logging.LogRecord(
            "integration.logger",
            logging.INFO,
            __file__,
            1,
            "hello %s",
            ("world",),
            None,
        )
        sink.emit(record)

        assert delivered.wait(timeout=1.0)
        assert len(batches) == 1
        assert len(batches[0]) == 1
        assert batches[0][0]["source"] == "integration"
        assert batches[0][0]["message"] == "hello world"
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


def test_attach_uses_custom_send_without_zerobus_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    monkeypatch.setattr(dl, "_active_sink", None)
    target = logging.getLogger("test.debug_logging.custom")
    target.handlers.clear()
    target.setLevel(logging.INFO)
    batches: list[list[dl.DebugLogRow]] = []
    delivered = threading.Event()

    def send(batch: list[dl.DebugLogRow]) -> None:
        batches.append(batch)
        delivered.set()

    dl.attach_debug_log_sink(
        [target],
        source="custom",
        level=logging.INFO,
        send=send,
    )
    sink = dl._active_sink
    assert sink is not None
    assert type(sink) is dl.DebugLogHandler
    try:
        target.info("custom delivery")
        assert delivered.wait(timeout=1.0)
        assert batches[0][0]["message"] == "custom delivery"
        # The audit-event logger is wired sink-only and non-propagating, so
        # request audit rows reach the table but never the on-disk/stderr logs.
        audit_logger = dl.audit_event_logger()
        assert audit_logger.propagate is False
        assert sink in audit_logger.handlers
    finally:
        target.removeHandler(sink)
        dl.sse_event_logger().removeHandler(sink)
        dl.audit_event_logger().removeHandler(sink)
        sink.close()


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


# ── origin workspace_id / app_name resolution ───────────────────────────────


def test_parse_app_host_basic() -> None:
    assert dl._parse_databricks_app_host(
        "https://omnigents-3272836215725701.aws.databricksapps.com/c/abc123"
    ) == ("omnigents", "3272836215725701")


def test_parse_app_host_hyphenated_app_name() -> None:
    # The app name may contain hyphens; only the final numeric segment is the
    # workspace id, so the split must be on the LAST hyphen.
    assert dl._parse_databricks_app_host(
        "https://my-cool-app-3272836215725701.aws.databricksapps.com"
    ) == ("my-cool-app", "3272836215725701")


def test_parse_app_host_non_apps_urls_yield_nothing() -> None:
    # Managed service (dbc-<hash> host), localhost, a custom domain, and empty
    # input carry no parseable Databricks Apps identity.
    assert dl._parse_databricks_app_host(
        "https://dbc-a5d4177a-49dc.cloud.databricks.com/omnigent"
    ) == (None, None)
    assert dl._parse_databricks_app_host("http://localhost:8000") == (None, None)
    assert dl._parse_databricks_app_host("https://omnigent.example.com") == (None, None)
    assert dl._parse_databricks_app_host(None) == (None, None)
    assert dl._parse_databricks_app_host("") == (None, None)


def test_parse_app_host_rejects_malformed_labels() -> None:
    # A databricksapps host with no hyphen, or a non-numeric final segment, is
    # not a valid <app_name>-<workspace_id> label.
    assert dl._parse_databricks_app_host("https://noworkspaceid.aws.databricksapps.com") == (
        None,
        None,
    )
    assert dl._parse_databricks_app_host("https://app-notanumber.aws.databricksapps.com") == (
        None,
        None,
    )


def test_process_identity_from_databricks_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Databricks App: the platform-injected env is authoritative.
    monkeypatch.setenv(dl.ORIGIN_WORKSPACE_ID_ENV_VAR, "111222333")
    monkeypatch.setenv(dl.APP_NAME_ENV_VAR, "omnigents")
    assert dl._process_identity() == ("111222333", "omnigents")


def test_process_identity_from_server_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Runner/host connected to a Databricks App: both parsed from the server URL.
    monkeypatch.setenv(
        dl.SERVER_URL_ENV_VAR, "https://omnigents-3272836215725701.aws.databricksapps.com"
    )
    assert dl._process_identity() == ("3272836215725701", "omnigents")


def test_process_identity_env_beats_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit DATABRICKS_* env wins over a (possibly divergent) URL parse.
    monkeypatch.setenv(dl.ORIGIN_WORKSPACE_ID_ENV_VAR, "999")
    monkeypatch.setenv(dl.APP_NAME_ENV_VAR, "envapp")
    monkeypatch.setenv(dl.SERVER_URL_ENV_VAR, "https://urlapp-111.aws.databricksapps.com")
    assert dl._process_identity() == ("999", "envapp")


def test_process_identity_managed_service_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Managed service: no DATABRICKS_* env and the server URL is a dbc-<hash> host,
    # not an Apps URL. Identity arrives per-record, so the process constant is empty.
    monkeypatch.setenv(
        dl.SERVER_URL_ENV_VAR, "https://dbc-a5d4177a-49dc.cloud.databricks.com/omnigent"
    )
    assert dl._process_identity() == (None, None)


def test_process_identity_unset_is_none() -> None:
    # OSS / local: nothing set anywhere.
    assert dl._process_identity() == (None, None)


def test_record_to_row_prefers_record_workspace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # The managed service stamps record.workspace_id per request; it wins over
    # the process-constant fallback.
    monkeypatch.setenv(dl.ORIGIN_WORKSPACE_ID_ENV_VAR, "process999")
    record = logging.LogRecord("omnigent.server", logging.INFO, __file__, 1, "hi", (), None)
    record.workspace_id = "perrequest111"
    assert dl.record_to_row(record, source="server")["workspace_id"] == "perrequest111"


def test_record_to_row_blank_record_workspace_id_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Outside a request the managed filter sets record.workspace_id = "" — an
    # empty value must fall through to the process constant, not win.
    monkeypatch.setenv(dl.ORIGIN_WORKSPACE_ID_ENV_VAR, "process999")
    record = logging.LogRecord("omnigent.server", logging.INFO, __file__, 1, "hi", (), None)
    record.workspace_id = ""
    assert dl.record_to_row(record, source="server")["workspace_id"] == "process999"


def test_record_to_row_origin_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Databricks App: both columns come from the process constant when the record
    # carries none.
    monkeypatch.setenv(dl.ORIGIN_WORKSPACE_ID_ENV_VAR, "3272836215725701")
    monkeypatch.setenv(dl.APP_NAME_ENV_VAR, "omnigents")
    record = logging.LogRecord("omnigent.server", logging.INFO, __file__, 1, "hi", (), None)
    row = dl.record_to_row(record, source="server")
    assert row["workspace_id"] == "3272836215725701"
    assert row["app_name"] == "omnigents"


def test_record_to_row_app_name_null_on_managed() -> None:
    # Managed service: workspace_id arrives per-record, but there is no per-request
    # app_name and no process constant, so app_name is null.
    record = logging.LogRecord("omnigent.server", logging.INFO, __file__, 1, "hi", (), None)
    record.workspace_id = "3272836215725701"
    row = dl.record_to_row(record, source="server")
    assert row["workspace_id"] == "3272836215725701"
    assert row["app_name"] is None


def test_record_to_row_origin_columns_null_on_oss() -> None:
    # OSS / local: nothing set anywhere → both columns null.
    record = logging.LogRecord("omnigent", logging.INFO, __file__, 1, "hi", (), None)
    row = dl.record_to_row(record, source="host")
    assert row["workspace_id"] is None
    assert row["app_name"] is None
