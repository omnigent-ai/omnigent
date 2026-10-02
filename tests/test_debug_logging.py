"""Tests for the client-side debug-log sink."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
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
        dl.CLIENT_SECRET_COMMAND_ENV_VAR,
        dl.WORKSPACE_URL_ENV_VAR,
        dl.ENDPOINT_ENV_VAR,
        dl.USER_ID_ENV_VAR,
        dl.PRIMARY_SESSION_ID_ENV_VAR,
        dl.RUNNER_ID_ENV_VAR,
        dl.ORIGIN_WORKSPACE_ID_ENV_VAR,
        dl.APP_NAME_ENV_VAR,
        dl.SERVER_URL_ENV_VAR,
        dl.SSE_LOG_TO_FILE_ENV_VAR,
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


def test_config_accepts_client_secret_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper --format raw")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)

    config = dl.config_from_env()

    assert config is not None
    assert config.client_secret is None
    assert config.client_secret_command == ("credential-helper", "--format", "raw")


def test_config_rejects_two_client_secret_sources(
    monkeypatch: pytest.MonkeyPatch, _configured_env: None
) -> None:
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper")
    assert dl.config_from_env() is None


def test_client_secret_command_is_lazy_and_memory_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper --format raw")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)
    config = dl.config_from_env()
    assert config is not None

    command_calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> object:
        command_calls.append(command)
        assert kwargs["stdin"] is dl.subprocess.DEVNULL
        return type("Completed", (), {"returncode": 0, "stdout": "secret-from-provider\n"})()

    class Client:
        def post(self, _url: str, **kwargs: object) -> httpx.Response:
            assert kwargs["auth"] == ("cid", "secret-from-provider")
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    monkeypatch.setattr(dl.subprocess, "run", run)
    source = dl._TokenSource(config, Client())  # type: ignore[arg-type]
    assert command_calls == []

    assert source.token() == "token"
    assert source.token() == "token"
    assert command_calls == [("credential-helper", "--format", "raw")]


@pytest.mark.parametrize(
    "error",
    [
        dl.subprocess.TimeoutExpired("credential-helper", 30),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["timeout", "invalid-encoding"],
)
def test_client_secret_command_errors_do_not_escape(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)
    config = dl.config_from_env()
    assert config is not None

    def run(*_: object, **__: object) -> object:
        raise error

    monkeypatch.setattr(dl.subprocess, "run", run)
    source = dl._TokenSource(config, client=None)  # type: ignore[arg-type]
    assert source.token() is None


@pytest.mark.parametrize(("returncode", "stdout"), [(1, "secret\n"), (0, " \n")])
def test_client_secret_command_rejects_unsuccessful_or_empty_output(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)
    config = dl.config_from_env()
    assert config is not None

    def run(*_: object, **__: object) -> object:
        return type("Completed", (), {"returncode": returncode, "stdout": stdout})()

    monkeypatch.setattr(dl.subprocess, "run", run)
    source = dl._TokenSource(config, client=None)  # type: ignore[arg-type]
    assert source.token() is None


def test_token_mint_auth_rejection_reruns_client_secret_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)
    config = dl.config_from_env()
    assert config is not None
    secrets = iter(("old-secret", "new-secret"))
    command_calls = 0

    def run(*_: object, **__: object) -> object:
        nonlocal command_calls
        command_calls += 1
        return type("Completed", (), {"returncode": 0, "stdout": next(secrets)})()

    class Client:
        def __init__(self) -> None:
            self.auth: list[object] = []

        def post(self, _url: str, **kwargs: object) -> httpx.Response:
            self.auth.append(kwargs["auth"])
            if len(self.auth) == 1:
                return httpx.Response(401, text="invalid client")
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})

    monkeypatch.setattr(dl.subprocess, "run", run)
    client = Client()
    source = dl._TokenSource(config, client)  # type: ignore[arg-type]

    assert source.token() is None
    assert source.token() == "token"
    assert command_calls == 2
    assert client.auth == [("cid", "old-secret"), ("cid", "new-secret")]


def test_insert_auth_rejection_refreshes_credentials_and_retries_same_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)
    config = dl.config_from_env()
    assert config is not None
    secrets = iter(("old-secret", "new-secret"))
    command_calls = 0

    def run(*_: object, **__: object) -> object:
        nonlocal command_calls
        command_calls += 1
        return type("Completed", (), {"returncode": 0, "stdout": next(secrets)})()

    class Client:
        def __init__(self) -> None:
            self.insert_payloads: list[object] = []

        def post(self, url: str, **kwargs: object) -> httpx.Response:
            if url.endswith("/oidc/v1/token"):
                secret = kwargs["auth"][1]  # type: ignore[index]
                return httpx.Response(
                    200, json={"access_token": f"token-for-{secret}", "expires_in": 3600}
                )
            self.insert_payloads.append(kwargs["content"])
            if len(self.insert_payloads) == 1:
                assert kwargs["headers"] == {  # type: ignore[comparison-overlap]
                    "Authorization": "Bearer token-for-old-secret",
                    "Content-Type": "application/json",
                }
                return httpx.Response(401, text="expired token")
            assert kwargs["headers"] == {  # type: ignore[comparison-overlap]
                "Authorization": "Bearer token-for-new-secret",
                "Content-Type": "application/json",
            }
            return httpx.Response(200)

    monkeypatch.setattr(dl.subprocess, "run", run)
    client = Client()
    sink = object.__new__(dl.ZerobusLogHandler)
    sink._config = config
    sink._client = client  # type: ignore[assignment]
    sink._tokens = dl._TokenSource(config, client)  # type: ignore[arg-type]
    sink._delivered_any = False
    batch: list[dl.DebugLogRow] = [{"message": "same batch"}]

    sink._post(batch)

    assert command_calls == 2
    assert client.insert_payloads == [json.dumps(batch), json.dumps(batch)]


def test_client_secret_command_runs_on_uploader_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_COMMAND_ENV_VAR, "credential-helper")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.cloud.databricks.com")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _INSERT_URL)
    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    command_started = threading.Event()
    release_command = threading.Event()
    delivered = threading.Event()
    command_thread: list[threading.Thread] = []

    def run(_command: tuple[str, ...], **_: object) -> object:
        command_thread.append(threading.current_thread())
        command_started.set()
        assert release_command.wait(timeout=1.0)
        return type("Completed", (), {"returncode": 0, "stdout": "secret\n"})()

    class Client:
        def post(self, url: str, **_: object) -> httpx.Response:
            if url.endswith("/oidc/v1/token"):
                return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
            delivered.set()
            return httpx.Response(200)

        def close(self) -> None:
            pass

    monkeypatch.setattr(dl.subprocess, "run", run)
    monkeypatch.setattr(dl.httpx, "Client", lambda **_: Client())
    config = dl.config_from_env()
    assert config is not None
    sink = dl.ZerobusLogHandler(config, "server")
    try:
        record = logging.LogRecord("omnigent.test", logging.INFO, __file__, 1, "ready", (), None)
        sink.emit(record)

        assert command_started.wait(timeout=1.0)
        assert command_thread == [sink._thread]
        assert not delivered.is_set()
        release_command.set()
        assert delivered.wait(timeout=1.0)
    finally:
        release_command.set()
        sink.close()


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
    assert row["attributes"]["exception_type"] == "ValueError"
    assert "exception_cause_type" not in row["attributes"]


def test_record_to_row_auto_attributes_logged_exception() -> None:
    """A record with exc_info gets error_category/error_impact derived from the
    exception, so every exc_info=… log site is covered without per-site edits.

    An arbitrary exception is UNKNOWN on both axes (no guessed owner); the sink
    does not need the callsite to have classified it.
    """
    import sys

    try:
        try:
            raise TimeoutError("private upstream URL")
        except TimeoutError as cause:
            raise RuntimeError("private launch configuration") from cause
    except RuntimeError:
        record = logging.LogRecord(
            "omnigent", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    attrs = dl.record_to_row(record, source="server")["attributes"]
    assert attrs == {
        "error_category": "unknown",
        "error_impact": "unknown",
        "exception_type": "RuntimeError",
        "exception_cause_type": "TimeoutError",
    }


def test_record_to_row_auto_attributes_omnigent_error_from_its_axes() -> None:
    """An OmnigentError logged via exc_info carries its own code-derived axes."""
    import sys

    from omnigent.errors import ErrorCode, OmnigentError

    try:
        raise OmnigentError("gone", code=ErrorCode.RUNNER_UNAVAILABLE)
    except OmnigentError:
        record = logging.LogRecord(
            "omnigent", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    attrs = dl.record_to_row(record, source="server")["attributes"]
    # runner_unavailable is config-owned and self-healing.
    assert attrs["error_category"] == "config"
    assert attrs["error_impact"] == "transient"


def test_record_to_row_explicit_attributes_win_over_derived() -> None:
    """An explicit error_category/error_impact on the record is never overwritten
    by the exception-derived fallback."""
    import sys

    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "omnigent", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    record.attributes = {
        "error_category": "server",
        "error_impact": "blocking",
        "exception_type": "ExplicitFailure",
        "exception_cause_type": "ExplicitCause",
    }
    attrs = dl.record_to_row(record, source="server")["attributes"]
    assert attrs == record.attributes


def test_phase_scope_stamps_error_phase_on_logged_exception() -> None:
    """An error logged inside a phase_scope inherits where it failed, even when
    the exception carries no error code."""
    import sys

    from omnigent.errors import ErrorPhase

    with dl.phase_scope(ErrorPhase.HARNESS_STARTUP):
        try:
            raise ValueError("spawn blew up")
        except ValueError:
            record = logging.LogRecord(
                "omnigent", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        attrs = dl.record_to_row(record, source="runner")["attributes"]
    assert attrs["error_phase"] == "harness_startup"
    # And the arbitrary exception still auto-attributes category/impact.
    assert attrs["error_category"] == "unknown"


def test_coded_error_phase_wins_over_ambient_scope() -> None:
    """A coded OmnigentError's own (concrete) phase beats the ambient scope, so
    a harness_not_configured raised during a runner-launch scope still reads
    harness_setup (the useful, semantic location)."""
    import sys

    from omnigent.errors import ErrorCode, ErrorPhase, OmnigentError

    with dl.phase_scope(ErrorPhase.RUNNER_LAUNCH):
        try:
            raise OmnigentError("no harness", code=ErrorCode.HARNESS_NOT_CONFIGURED)
        except OmnigentError:
            record = logging.LogRecord(
                "omnigent", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        attrs = dl.record_to_row(record, source="server")["attributes"]
    assert attrs["error_phase"] == "harness_setup"


def test_no_phase_when_no_code_and_no_scope() -> None:
    """Outside any scope, an uncoded exception gets no error_phase (we don't
    guess a location)."""
    import sys

    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "omnigent", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    attrs = dl.record_to_row(record, source="server")["attributes"]
    assert "error_phase" not in attrs


def test_benign_row_in_phase_scope_is_not_stamped() -> None:
    """A non-error log line inside a phase_scope must NOT inherit error_phase.

    phase_scope wraps the whole turn loop, so benign INFO/DEBUG telemetry (and
    high-volume SSE-event rows) flow through here. Stamping them would pollute
    the error_phase column, so only rows that are actually errors (an exception,
    or an explicit category/impact) get located.
    """
    from omnigent.errors import ErrorPhase

    with dl.phase_scope(ErrorPhase.TURN):
        info = logging.LogRecord("omnigent.runtime.x", logging.INFO, __file__, 1, "hi", (), None)
        info_attrs = dl.record_to_row(info, source="runner")["attributes"]
        # A bare WARNING with no exception and no explicit error attrs is not an
        # error row either; it stays clean.
        warn = logging.LogRecord(
            "omnigent.runtime.x", logging.WARNING, __file__, 1, "hm", (), None
        )
        warn_attrs = dl.record_to_row(warn, source="runner")["attributes"]
    assert "error_phase" not in info_attrs
    assert "error_category" not in info_attrs
    assert "error_phase" not in warn_attrs


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


# ── SSE-event file sink (OMNIGENT_SSE_LOG_TO_FILE) ───────────────────────────


@pytest.fixture
def _reset_sse_file_sink() -> Iterator[None]:
    """Detach the SSE file sink and reset its process-wide state around a test."""
    yield
    sse_logger = logging.getLogger(dl.SSE_LOGGER_NAME)
    for handler in list(sse_logger.handlers):
        if isinstance(handler, dl.SseFileHandler):
            sse_logger.removeHandler(handler)
            handler.close()
    dl._sse_file_handler = None


def _emit_sse(event: str, *, session_id: str, level: int = logging.INFO, **attrs: object) -> None:
    """Emit one record the way session_stream._log_sse_event does."""
    extra = dl.debug_event(event, session_id=session_id)
    extra["attributes"] = dict(attrs)
    dl.sse_event_logger().log(level, "sse %s", event, extra=extra)


def _flush_sse_sink() -> None:
    """Block until the async writer has persisted everything queued so far.

    Writes happen on a daemon thread, so tests flush before reading the files or
    inspecting the fd cache.
    """
    handler = dl._sse_file_handler
    if handler is not None:
        handler.flush()


def _sse_dir(data_dir: Path, source: str = "server") -> Path:
    """The per-source SSE log directory under a test data dir (``logs/<source>``)."""
    return data_dir / "logs" / source


def test_sse_file_sink_noop_without_env(_reset_sse_file_sink: None) -> None:
    dl.attach_sse_file_sink(source="server", level=logging.INFO)
    assert not dl.sse_file_sink_enabled()


@pytest.mark.parametrize("value", ["0", "false", "off", "no", ""])
def test_sse_file_sink_noop_when_falsy(
    value: str, monkeypatch: pytest.MonkeyPatch, _reset_sse_file_sink: None
) -> None:
    monkeypatch.setenv(dl.SSE_LOG_TO_FILE_ENV_VAR, value)
    dl.attach_sse_file_sink(source="server", level=logging.INFO)
    assert not dl.sse_file_sink_enabled()


def test_sse_file_sink_writes_per_session_safe_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _reset_sse_file_sink: None
) -> None:
    monkeypatch.setenv(dl.SSE_LOG_TO_FILE_ENV_VAR, "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    dl.attach_sse_file_sink(source="server", level=logging.INFO)
    assert dl.sse_file_sink_enabled()

    _emit_sse("response.completed", session_id="conv_1", response_id="resp_1", sequence_number=7)
    _emit_sse("response.failed", session_id="conv_1", level=logging.WARNING, error_code="timeout")
    _emit_sse("response.created", session_id="conv_2", response_id="resp_2")
    _flush_sse_sink()

    sse_dir = _sse_dir(tmp_path)
    # One file per session, named by session id, under logs/server/.
    conv1 = (sse_dir / "conv_1-sse.jsonl").read_text().splitlines()
    conv2 = (sse_dir / "conv_2-sse.jsonl").read_text().splitlines()
    assert len(conv1) == 2  # both conv_1 events; conv_2 stays in its own file
    assert len(conv2) == 1

    first = json.loads(conv1[0])
    assert first["source"] == "server"
    assert first["level"] == "INFO"
    assert first["conversation_id"] == "conv_1"
    assert first["event"] == "response.completed"
    # Safe subset preserved with native types; no content field ever written.
    assert first["attrs"] == {"response_id": "resp_1", "sequence_number": 7}
    assert "delta" not in first and "message" not in first
    assert "ts" in first
    second = json.loads(conv1[1])
    assert second["level"] == "WARNING"
    assert second["attrs"] == {"error_code": "timeout"}
    assert json.loads(conv2[0])["conversation_id"] == "conv_2"


def test_sse_file_sink_sanitizes_session_id_in_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _reset_sse_file_sink: None
) -> None:
    # A session id with path separators must not escape the log directory.
    monkeypatch.setenv(dl.SSE_LOG_TO_FILE_ENV_VAR, "yes")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    dl.attach_sse_file_sink(source="server", level=logging.INFO)
    _emit_sse("response.completed", session_id="a/../b")
    _flush_sse_sink()
    # Every non-alnum/[-_] char (slash and dot) collapses to "_".
    assert (_sse_dir(tmp_path) / "a____b-sse.jsonl").exists()


def test_sse_file_sink_bounds_open_descriptors_and_reopens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _reset_sse_file_sink: None
) -> None:
    # Over the LRU cap the least-recently-used fd is closed, but its file remains
    # and a later event for that session reopens and appends (no lines lost).
    monkeypatch.setenv(dl.SSE_LOG_TO_FILE_ENV_VAR, "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(dl.SseFileHandler, "_MAX_OPEN_FILES", 2)
    dl.attach_sse_file_sink(source="server", level=logging.INFO)
    handler = dl._sse_file_handler
    assert handler is not None

    _emit_sse("response.created", session_id="conv_a")  # opens conv_a
    _emit_sse("response.created", session_id="conv_b")  # opens conv_b
    _emit_sse("response.created", session_id="conv_c")  # evicts conv_a (LRU)
    _flush_sse_sink()
    assert len(handler._fds) == 2
    assert "conv_a" not in handler._fds

    _emit_sse("response.completed", session_id="conv_a")  # reopens conv_a, appends
    _flush_sse_sink()
    conv_a = (_sse_dir(tmp_path) / "conv_a-sse.jsonl").read_text().splitlines()
    assert [json.loads(line)["event"] for line in conv_a] == [
        "response.created",
        "response.completed",
    ]


def test_sse_file_sink_preserves_per_session_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _reset_sse_file_sink: None
) -> None:
    # The async writer persists a session's events in emit order (however the
    # drain loop happens to batch them) and loses none under normal load.
    monkeypatch.setenv(dl.SSE_LOG_TO_FILE_ENV_VAR, "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    dl.attach_sse_file_sink(source="server", level=logging.INFO)

    for i in range(500):
        _emit_sse("response.output_text.delta", session_id="conv_x", sequence_number=i)
    _flush_sse_sink()

    lines = (_sse_dir(tmp_path) / "conv_x-sse.jsonl").read_text().splitlines()
    assert [json.loads(line)["attrs"]["sequence_number"] for line in lines] == list(range(500))


def test_sse_file_sink_is_independent_of_table_and_stops_propagation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _reset_sse_file_sink: None
) -> None:
    # File sink on, ZeroBus table sink off: SSE logging is still enabled, and the
    # SSE logger must not propagate (so events never reach the on-disk/stderr logs).
    monkeypatch.setattr(dl, "_active_sink", None)
    monkeypatch.setenv(dl.SSE_LOG_TO_FILE_ENV_VAR, "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    dl.attach_sse_file_sink(source="server", level=logging.INFO)

    assert not dl.debug_sink_enabled()
    assert dl.sse_logging_enabled()
    assert logging.getLogger(dl.SSE_LOGGER_NAME).propagate is False


def test_sse_file_sink_attach_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _reset_sse_file_sink: None
) -> None:
    monkeypatch.setenv(dl.SSE_LOG_TO_FILE_ENV_VAR, "1")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    dl.attach_sse_file_sink(source="server", level=logging.INFO)
    dl.attach_sse_file_sink(source="server", level=logging.INFO)
    sse_logger = logging.getLogger(dl.SSE_LOGGER_NAME)
    handlers = [h for h in sse_logger.handlers if isinstance(h, dl.SseFileHandler)]
    assert len(handlers) == 1


@pytest.mark.asyncio
async def test_runner_log_scope_isolates_tasks_and_threads_and_restores_context() -> None:
    import asyncio

    def row() -> dict[str, object]:
        record = logging.LogRecord("omnigent.test", logging.INFO, __file__, 1, "test", (), None)
        return dl.record_to_row(record, "server")

    async def launch(session_id: str, runner_id: str) -> dict[str, object]:
        with dl.runner_log_scope(session_id, runner_id):
            await asyncio.sleep(0)
            return await asyncio.to_thread(row)

    with dl.runner_log_scope(None, None):
        first, second = await asyncio.gather(launch("s1", "r1"), launch("s2", "r2"))
        assert first["session_id"] == "s1"
        assert first["attributes"]["runner_id"] == "r1"
        assert second["session_id"] == "s2"
        assert second["attributes"]["runner_id"] == "r2"
        assert row()["session_id"] is None
        assert "runner_id" not in row()["attributes"]


def test_runner_defaults_and_explicit_child_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dl.RUNNER_ID_ENV_VAR, "runner_env")
    monkeypatch.setenv(dl.PRIMARY_SESSION_ID_ENV_VAR, "parent")
    record = logging.LogRecord("omnigent.test", logging.INFO, __file__, 1, "test", (), None)
    with dl.runner_log_scope(None, None):
        assert dl.record_to_row(record, "runner")["attributes"]["runner_id"] == "runner_env"
        assert "runner_id" not in dl.record_to_row(record, "host")["attributes"]
        assert dl.record_to_row(record, "host")["session_id"] is None
        assert dl.record_to_row(record, "server")["session_id"] is None
        assert "runner_id" not in dl.record_to_row(record, "server")["attributes"]
        with dl.current_session_id_scope("child"):
            assert dl.record_to_row(record, "runner")["session_id"] == "child"
        record.session_id = "explicit_child"
        record.attributes = {"runner_id": "explicit_runner", "request_id": "explicit_request"}
        assert dl.record_to_row(record, "runner")["session_id"] == "explicit_child"
        assert dl.record_to_row(record, "runner")["attributes"]["runner_id"] == "explicit_runner"
