"""Per-session error boundary for imports: classified failures, and a stream that always ends."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import imports as imports_module
from omnigent.session_import.errors import (
    MISSING_SQLITE_FIX_COMMANDS,
    MISSING_SQLITE_MESSAGE,
    ImportErrorCode,
    LocalImportError,
)
from omnigent.stores.conversation_store import ConversationStore
from tests.server.import_tunnel_harness import (
    FakeConversationStore,
    TunnelPair,
    cli_import_body,
    client,
    fail_append_for,
    host_record,
    imports_app,
    local_import_body,
    local_session,
    ndjson,
    stream_through_tunnel,
)


class _ResourceExhausted(Exception):
    """Stands in for a backend error the store knows how to classify."""


class _ClassifyingStore(FakeConversationStore):
    """A store whose hook recognizes oversized items and save timeouts."""

    def __init__(self) -> None:
        super().__init__()
        self.classified: list[BaseException] = []

    def classify_import_error(self, exc: BaseException) -> LocalImportError | None:
        self.classified.append(exc)
        if isinstance(exc, _ResourceExhausted):
            return LocalImportError(
                "This session is too large to import: one message is 5 MB (limit 4 MB).",
                import_code=ImportErrorCode.SESSION_TOO_LARGE,
                code=ErrorCode.INVALID_INPUT,
                http_status=413,
            )
        if isinstance(exc, TimeoutError):
            return LocalImportError(
                "Saving this session timed out. Try importing it again.",
                import_code=ImportErrorCode.SESSION_SAVE_TIMEOUT,
                code=ErrorCode.INTERNAL_ERROR,
                http_status=503,
            )
        return None


_INTERNAL_MESSAGE = (
    "Import stopped because of an internal error. Try again; if it keeps happening, "
    "contact an administrator."
)


def _failed(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] == "failed"]


async def _post_with_fake_stream(
    monkeypatch: pytest.MonkeyPatch, fake_stream: Any, path: str
) -> Any:
    """POST a local import whose host stream is replaced by ``fake_stream``."""
    monkeypatch.setattr(imports_module, "_stream_local_sessions_from_host", fake_stream)
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    async with client(app) as http:
        return await http.post(path, json=local_import_body())


async def _post_cli(store: FakeConversationStore) -> Any:
    app = imports_app(store, host_registry=HostRegistry(), host=host_record())
    async with client(app, raise_app_exceptions=False) as http:
        return await http.post("/v1/imports", json=cli_import_body())


async def test_unclassified_storage_error_fails_one_session(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-Omnigent storage error fails only its session, rolls it back, and hides its text."""
    store = FakeConversationStore()
    fail_append_for(store, "s1", RuntimeError("grpc: socket closed /secret/path"))
    sessions = {f"s{i}": local_session(f"s{i}") for i in range(3)}
    with caplog.at_level(logging.ERROR, logger=imports_module.__name__):
        events = await stream_through_tunnel(monkeypatch, store, sessions)

    assert imports_module._import_conversation_id("claude", "s1") not in store.conversations
    done = events[-1]
    assert done["event"] == "done"
    assert (done["imported"], done["failed"], done["total"], done["complete"]) == (2, 1, 3, True)
    (failed,) = _failed(events)
    assert failed["external_session_id"] == "s1"
    assert failed["code"] == ImportErrorCode.INTERNAL
    assert failed["retryable"] is True
    assert failed["reason"] == _INTERNAL_MESSAGE
    # The id is its own field, not part of the reason.
    error_id = failed["error_id"]
    assert error_id.startswith("err_")
    assert "/secret/path" not in str(events)
    # The raw text and the error id land in the server log only.
    assert error_id in caplog.text
    assert "/secret/path" in caplog.text
    assert not [e for e in events if e["event"] == "error"]


async def test_store_hook_classifies_a_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store's classify_import_error turns a backend error into an actionable code."""
    store = _ClassifyingStore()
    fail_append_for(store, "s0", _ResourceExhausted("message too large 5242880 > 4194304"))
    sessions = {"s0": local_session("s0"), "s1": local_session("s1")}
    events = await stream_through_tunnel(monkeypatch, store, sessions)
    (failed,) = _failed(events)
    assert failed["code"] == ImportErrorCode.SESSION_TOO_LARGE
    assert failed["retryable"] is False
    assert failed["reason"].startswith("This session is too large to import")
    assert isinstance(store.classified[0], _ResourceExhausted)
    assert events[-1]["imported"] == 1


async def test_failing_hook_degrades_to_internal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A classify hook that raises leaves the session failed as internal, not the batch."""

    class _BrokenHookStore(FakeConversationStore):
        def classify_import_error(self, exc: BaseException) -> LocalImportError | None:
            raise ValueError("hook bug")

    store = _BrokenHookStore()
    fail_append_for(store, "s0", RuntimeError("boom"))
    events = await stream_through_tunnel(monkeypatch, store, {"s0": local_session("s0")})
    (failed,) = _failed(events)
    assert failed["code"] == ImportErrorCode.INTERNAL
    assert events[-1]["complete"] is True


async def test_dedupe_lookup_failure_is_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store error in the already-imported lookup fails only that session."""

    class _FlakyLookupStore(FakeConversationStore):
        def find_conversation_by_external_session_id(self, external_session_id: str) -> Any:
            if external_session_id == "s0":
                raise ConnectionResetError("db went away")
            return super().find_conversation_by_external_session_id(external_session_id)

    sessions = {"s0": local_session("s0"), "s1": local_session("s1")}
    events = await stream_through_tunnel(monkeypatch, _FlakyLookupStore(), sessions)
    assert [e["external_session_id"] for e in _failed(events)] == ["s0"]
    assert events[-1]["imported"] == 1


def test_store_default_hook_classifies_nothing() -> None:
    """The base ConversationStore hook leaves every error unclassified."""
    assert ConversationStore.classify_import_error(cast(Any, object()), RuntimeError("x")) is None


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        ({"reason": "No visible messages to import."}, ImportErrorCode.SESSION_UNREADABLE),
        (
            {"reason": "too big", "code": ImportErrorCode.SESSION_TOO_LARGE},
            ImportErrorCode.SESSION_TOO_LARGE,
        ),
        ({"reason": "future", "code": "some_future_code"}, "some_future_code"),
        (
            {"reason": "ModuleNotFoundError: No module named '_sqlite3'"},
            ImportErrorCode.HOST_PYTHON_MISSING_SQLITE,
        ),
    ],
)
def test_host_failure_code_passes_through_or_defaults(entry: dict[str, Any], code: str) -> None:
    """A host-reported failure keeps its code; a code-less one is classified from its reason."""
    assert imports_module._host_failure_code(entry) == code


async def test_host_reported_failures_keep_code_and_retryability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host failure entries become failed events with their code and retryable flag."""

    async def _fake_stream(**kwargs: Any) -> Any:
        yield {
            "external_session_id": "ok",
            "items": cli_import_body()["items"],
            "source": "claude",
            "total": 3,
        }
        kwargs["stats"]["host_failures"] = [
            {
                "external_session_id": "big",
                "source": "claude",
                "reason": "This session is too large for the connected server.",
                "code": ImportErrorCode.SESSION_TOO_LARGE,
            },
            {
                "external_session_id": "old",
                "source": "codex",
                "reason": "ModuleNotFoundError: No module named '_sqlite3'",
            },
        ]

    response = await _post_with_fake_stream(monkeypatch, _fake_stream, "/v1/imports/local/stream")
    events = ndjson(response)
    by_id = {e["external_session_id"]: e for e in _failed(events)}
    assert (by_id["big"]["code"], by_id["big"]["retryable"]) == (
        ImportErrorCode.SESSION_TOO_LARGE,
        False,
    )
    # An older host's raw ImportError text is replaced with the actionable message.
    assert by_id["old"]["code"] == ImportErrorCode.HOST_PYTHON_MISSING_SQLITE
    assert by_id["old"]["reason"] == MISSING_SQLITE_MESSAGE
    assert by_id["old"]["retryable"] is False
    assert (events[-1]["imported"], events[-1]["failed"]) == (1, 2)


async def test_oversized_item_count_is_session_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session over the item cap fails as session_too_large, not unreadable."""
    monkeypatch.setattr(imports_module, "_MAX_IMPORT_ITEMS", 2)
    events = await stream_through_tunnel(
        monkeypatch, FakeConversationStore(), {"s0": local_session("s0", items=3)}
    )
    (failed,) = _failed(events)
    assert (failed["code"], failed["retryable"]) == (ImportErrorCode.SESSION_TOO_LARGE, False)


async def _exploding_stream(**_kwargs: Any) -> Any:
    # A malformed session (fails alone), then an unexpected crash.
    yield {"external_session_id": "s0", "items": "not-a-list", "source": "claude", "total": 4}
    raise KeyError("unexpected")


async def test_unexpected_exception_buffered_is_classified_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The buffered route answers an unexpected crash with a classified 500 body."""
    response = await _post_with_fake_stream(monkeypatch, _exploding_stream, "/v1/imports/local")
    assert response.status_code == 500
    error = response.json()["error"]
    assert (error["code"], error["import_code"], error["retryable"]) == (
        ErrorCode.INTERNAL_ERROR,
        ImportErrorCode.INTERNAL,
        True,
    )
    assert error["error_id"].startswith("err_")


async def test_unexpected_exception_stream_still_ends_with_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream reports an unexpected crash inline and still ends with an incomplete done."""
    response = await _post_with_fake_stream(
        monkeypatch, _exploding_stream, "/v1/imports/local/stream"
    )
    events = ndjson(response)
    assert [e["event"] for e in events][-3:] == ["failed", "error", "done"]
    error = events[-2]
    assert (error["code"], error["retryable"]) == (ImportErrorCode.INTERNAL, True)
    assert error["error_id"] in error["message"]
    assert (events[-1]["total"], events[-1]["complete"], events[-1]["failed"]) == (4, False, 1)


async def test_done_total_is_null_when_the_host_never_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that sent nothing yields a lone done with a null total."""

    async def _empty_stream(**_kwargs: Any) -> Any:
        for item in ():
            yield item

    response = await _post_with_fake_stream(monkeypatch, _empty_stream, "/v1/imports/local/stream")
    assert ndjson(response) == [
        {
            "event": "done",
            "imported": 0,
            "already_imported": 0,
            "failed": 0,
            "failures": [],
            "total": None,
            "complete": True,
        }
    ]


@pytest.mark.parametrize("route", ["stream", "buffered"])
async def test_host_missing_sqlite_whole_import_is_actionable(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """A host whose listing fails on a missing SQLite module gets the fix, not a generic error."""

    def _across(*, limit: int) -> list[tuple[str, str]]:
        raise ModuleNotFoundError("No module named '_sqlite3'")

    monkeypatch.setattr(
        "omnigent.session_import.local.list_recent_sessions_across_harnesses", _across
    )
    pair = TunnelPair()
    app = imports_app(FakeConversationStore(), host_registry=pair.registry, host=host_record())
    path = "/v1/imports/local/stream" if route == "stream" else "/v1/imports/local"
    async with pair, client(app) as http:
        response = await http.post(path, json=local_import_body())
    if route == "buffered":
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["import_code"] == ImportErrorCode.HOST_PYTHON_MISSING_SQLITE
    else:
        events = ndjson(response)
        (error,) = [e for e in events if e["event"] == "error"]
        assert error["code"] == ImportErrorCode.HOST_PYTHON_MISSING_SQLITE
        assert events[-1]["complete"] is False
    assert error["retryable"] is False
    assert error["message"] == MISSING_SQLITE_MESSAGE
    assert error["fix_commands"] == [dict(fix) for fix in MISSING_SQLITE_FIX_COMMANDS]


@pytest.mark.parametrize(
    ("exc", "status", "import_code"),
    [
        (_ResourceExhausted("Frame size 5242880 exceeds maximum"), 413, "session_too_large"),
        (TimeoutError("deadline"), 503, "session_save_timeout"),
    ],
)
async def test_cli_import_returns_classified_status(
    exc: BaseException, status: int, import_code: str
) -> None:
    """``/v1/imports`` answers a classified storage error with its status and import code."""
    store = _ClassifyingStore()

    def on_append(_conversation_id: str, _items: list[Any]) -> None:
        raise exc

    store.on_append = on_append
    response = await _post_cli(store)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["import_code"] == import_code
    assert error["retryable"] is (import_code == ImportErrorCode.SESSION_SAVE_TIMEOUT)
    assert error["message"] != "An internal error occurred."
    # Nothing half-written is left behind.
    assert store.conversations == {}


async def test_cli_import_unclassified_error_is_internal_with_an_error_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error the store doesn't recognize is a 500 internal whose id is in the server log."""
    store = _ClassifyingStore()

    def on_append(_conversation_id: str, _items: list[Any]) -> None:
        raise RuntimeError("db exploded at /secret/path")

    store.on_append = on_append
    with caplog.at_level(logging.ERROR, logger=imports_module.__name__):
        response = await _post_cli(store)
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == ErrorCode.INTERNAL_ERROR
    assert (error["import_code"], error["retryable"]) == (ImportErrorCode.INTERNAL, True)
    assert error["message"] == _INTERNAL_MESSAGE
    assert error["error_id"].startswith("err_")
    assert error["error_id"] in caplog.text
    assert "/secret/path" not in response.text
    # Rolled back like any failed persist.
    assert store.conversations == {}


def test_classified_error_keeps_the_global_code() -> None:
    """A LocalImportError is an OmnigentError whose details carry its import code."""
    error = LocalImportError(
        "x",
        import_code=ImportErrorCode.TIME_LIMIT_REACHED,
        code=ErrorCode.INTERNAL_ERROR,
        http_status=503,
    )
    assert isinstance(error, OmnigentError)
    assert error.code == ErrorCode.INTERNAL_ERROR
    assert error.http_status == 503
    assert error.details == {"import_code": "time_limit_reached", "retryable": True}
    report = imports_module._record_local_import_failure(error)
    assert (report.import_code, report.message, report.retryable) == (
        "time_limit_reached",
        "x",
        True,
    )
