"""Unit tests for ``omnigent.telemetry.client``.

``test_telemetry.py`` covers the opt-out checks and record promotion. This
module covers remote-config parsing, the environment tag, record building
edge cases, the emitter's queue/batching behavior, and the module-level
singleton. Threads, ``atexit`` and the network are all faked, so nothing runs
in the background and nothing is sent.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.telemetry.client as client_mod
from omnigent.telemetry.client import (
    TelemetryClient,
    TelemetryConfig,
    _build_record,
    _config_telemetry_disabled,
    _config_url,
    _detect_environment,
    _fetch_remote_config,
    emit,
    get_client,
    init_client,
    is_disabled,
)

_INGEST = "https://ingest.example.test/records"


@pytest.fixture(autouse=True)
def _isolate_client_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the opt-out cache and singleton so tests never leak state."""
    monkeypatch.setattr(client_mod, "_IS_DISABLED_CACHE", [None])
    monkeypatch.setattr(client_mod, "_CLIENT", None)


@dataclass
class _Evt:
    installation_id: object = None
    session_id: object = None
    anon_user_id: object = None
    host_installation_id: object = None


@dataclass
class _Detail:
    path: Path
    count: int = 1


class _Response:
    def __init__(self, body: bytes = b"") -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _serve_config(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _Response(body))


def _valid_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "omnigent_version": client_mod.VERSION,
        "ingestion_url": _INGEST,
    }
    config.update(overrides)
    return config


def _record(name: str = "Evt") -> dict[str, object]:
    return {"data": {"event_name": name}, "partition-key": "k"}


# ── remote config URL ────────────────────────────────────────


def test_config_url_honors_override_and_trims_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_TELEMETRY_CONFIG_URL", " http://localhost:9/cfg/ ")
    monkeypatch.setattr(client_mod, "VERSION", "1.2.3")

    assert _config_url() == "http://localhost:9/cfg/1.2.3.json"


@pytest.mark.parametrize(
    ("version", "base"),
    [
        ("1.2.3", client_mod._CONFIG_URL_PROD),
        ("1.2.3.dev4", client_mod._CONFIG_URL_STAGING),
        ("1.2.3rc1", client_mod._CONFIG_URL_STAGING),
        # An unparseable version falls back to production.
        ("not a version", client_mod._CONFIG_URL_PROD),
    ],
)
def test_config_url_selects_staging_for_prereleases(
    monkeypatch: pytest.MonkeyPatch, version: str, base: str
) -> None:
    monkeypatch.delenv("OMNIGENT_TELEMETRY_CONFIG_URL", raising=False)
    monkeypatch.setattr(client_mod, "VERSION", version)

    assert _config_url() == f"{base}/{version}.json"


# ── remote config fetch ──────────────────────────────────────


def test_fetch_config_returns_ingestion_url_and_disabled_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_config(monkeypatch, _valid_config(disable_events=["A", 7, "B"]))

    config = _fetch_remote_config()

    assert config == TelemetryConfig(ingestion_url=_INGEST, disable_events={"A", "B"})


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "an", "object"],
        _valid_config(omnigent_version="0.0.0-other"),
        _valid_config(disable_telemetry=True),
        _valid_config(ingestion_url=""),
        _valid_config(ingestion_url=42),
        {"omnigent_version": client_mod.VERSION},
        _valid_config(rollout_percentage=0),
    ],
)
def test_fetch_config_disables_telemetry_for_unusable_config(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    _serve_config(monkeypatch, payload)

    assert _fetch_remote_config() is None


def test_fetch_config_disables_telemetry_for_excluded_os(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_mod.platform, "system", lambda: "Windows")
    _serve_config(monkeypatch, _valid_config(disable_os=["Windows", 3]))
    assert _fetch_remote_config() is None

    monkeypatch.setattr(client_mod.platform, "system", lambda: "Linux")
    assert _fetch_remote_config() is not None


def test_fetch_config_ignores_malformed_optional_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-list ``disable_os`` / ``disable_events`` and a non-numeric rollout
    # (bool counts as non-numeric) fall back to their permissive defaults.
    _serve_config(
        monkeypatch,
        _valid_config(disable_os="Linux", disable_events="A", rollout_percentage=True),
    )

    assert _fetch_remote_config() == TelemetryConfig(ingestion_url=_INGEST)


def test_fetch_config_swallows_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(req: object, timeout: float) -> None:
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", failing)

    assert _fetch_remote_config() is None


# ── config.yaml opt-out ──────────────────────────────────────


def test_config_opt_out_reads_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("theme: dark\n  Telemetry : FALSE  \n", encoding="utf-8")

    assert _config_telemetry_disabled() is True


def test_config_opt_out_defaults_to_home_dot_omnigent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "config.yaml").write_text("telemetry: false\n", encoding="utf-8")

    assert _config_telemetry_disabled() is True


def test_config_opt_out_is_false_when_missing_or_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    assert _config_telemetry_disabled() is False  # no config.yaml

    # A directory where the file should be: exists() is true but reading fails.
    (tmp_path / "config.yaml").mkdir()
    assert _config_telemetry_disabled() is False


def test_is_disabled_fails_closed_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def exploding() -> bool:
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(client_mod, "_compute_is_disabled", exploding)

    assert is_disabled() is True
    assert is_disabled() is True
    # The result is computed once and reused.
    assert calls == [1]


# ── environment tag ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("env_var", "tag"),
    [
        ("KAGGLE_KERNEL_RUN_TYPE", "kaggle"),
        ("COLAB_BACKEND_VERSION", "colab"),
        ("AZUREML_ARM_WORKSPACE_NAME", "azure_ml"),
        ("SM_CURRENT_HOST", "sagemaker_studio"),
    ],
)
def test_detect_environment_tags_hosted_notebooks(
    monkeypatch: pytest.MonkeyPatch, env_var: str, tag: str
) -> None:
    for name in ("KAGGLE_KERNEL_RUN_TYPE", "COLAB_BACKEND_VERSION", "AZUREML_ARM_WORKSPACE_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SM_CURRENT_HOST", raising=False)
    monkeypatch.setenv(env_var, "1")

    assert _detect_environment() == tag


def test_detect_environment_docker_and_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KAGGLE_KERNEL_RUN_TYPE",
        "COLAB_BACKEND_VERSION",
        "AZUREML_ARM_WORKSPACE_NAME",
        "SM_CURRENT_HOST",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(client_mod.os.path, "exists", lambda p: p == "/.dockerenv")
    assert _detect_environment() == "docker"

    monkeypatch.setattr(client_mod.os.path, "exists", lambda p: False)
    assert _detect_environment() is None

    def failing(path: str) -> bool:
        raise OSError("denied")

    monkeypatch.setattr(client_mod.os.path, "exists", failing)
    assert _detect_environment() is None


# ── record building ──────────────────────────────────────────


def test_build_record_without_ids_or_fields_has_empty_envelope() -> None:
    record = _build_record(_Evt())

    data = record["data"]
    assert data["event_name"] == "_Evt"
    assert data["session_id"] == ""
    assert data["installation_id"] is None and data["anon_user_id"] is None
    assert data["host_installation_id"] is None
    # Every id field was promoted out, leaving nothing for ``params``.
    assert data["params"] is None
    assert data["status"] == "success" and data["duration_ms"] == 0
    assert record["partition-key"] != _build_record(_Evt())["partition-key"]


def test_build_record_ignores_non_string_ids() -> None:
    record = _build_record(_Evt(installation_id=7, session_id=object(), anon_user_id=["x"]))

    data = record["data"]
    assert data["installation_id"] is None
    assert data["session_id"] == ""
    assert data["anon_user_id"] is None


def test_build_record_json_encodes_event_fields_with_str_fallback(tmp_path: Path) -> None:
    record = _build_record(_Detail(path=tmp_path, count=3))

    assert json.loads(record["data"]["params"] or "") == {"path": str(tmp_path), "count": 3}


# ── emitter: emit ────────────────────────────────────────────


@pytest.fixture()
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "is_disabled", lambda: False)


@pytest.fixture()
def quiet_client(monkeypatch: pytest.MonkeyPatch, enabled: None) -> TelemetryClient:
    """A client whose background threads never start."""
    client = TelemetryClient()
    started: list[bool] = []
    monkeypatch.setattr(client, "_ensure_started", lambda: started.append(True))
    client.started_calls = started  # type: ignore[attr-defined]
    return client


def test_emit_queues_a_record(quiet_client: TelemetryClient) -> None:
    quiet_client.emit(_Evt(session_id="s1"))

    assert quiet_client._queue.qsize() == 1
    assert quiet_client._queue.get_nowait()["data"]["session_id"] == "s1"  # type: ignore[index]
    assert quiet_client.started_calls == [True]  # type: ignore[attr-defined]


def test_emit_is_a_noop_when_stopped_or_disabled(
    monkeypatch: pytest.MonkeyPatch, quiet_client: TelemetryClient
) -> None:
    quiet_client._stopped = True
    quiet_client.emit(_Evt())
    quiet_client._stopped = False
    monkeypatch.setattr(client_mod, "is_disabled", lambda: True)
    quiet_client.emit(_Evt())

    assert quiet_client._queue.empty()


def test_emit_skips_events_killed_by_remote_config(quiet_client: TelemetryClient) -> None:
    quiet_client._config = TelemetryConfig(ingestion_url=_INGEST, disable_events={"_Evt"})
    quiet_client._config_ready.set()

    quiet_client.emit(_Evt())
    quiet_client.emit(_Detail(path=Path(".")))

    assert quiet_client._queue.qsize() == 1


def test_emit_drops_events_when_queue_is_full(
    monkeypatch: pytest.MonkeyPatch, quiet_client: TelemetryClient
) -> None:
    monkeypatch.setattr(quiet_client, "_queue", queue.Queue(maxsize=1))

    quiet_client.emit(_Evt(session_id="first"))
    quiet_client.emit(_Evt(session_id="dropped"))  # must not raise or block

    assert quiet_client._queue.qsize() == 1
    assert quiet_client._queue.get_nowait()["data"]["session_id"] == "first"  # type: ignore[index]


def test_emit_swallows_serialisation_errors(quiet_client: TelemetryClient) -> None:
    quiet_client.emit(object())  # not a dataclass

    assert quiet_client._queue.empty()


# ── emitter: flush / shutdown ────────────────────────────────


def test_flush_returns_once_queue_is_drained_and_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TelemetryClient()
    client.flush()  # nothing queued: join() returns immediately

    def failing_join() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client._queue, "join", failing_join)
    client.flush()


def test_shutdown_sends_poison_pill_joins_thread_and_is_idempotent() -> None:
    client = TelemetryClient()
    joined: list[float] = []
    client._thread = SimpleNamespace(join=lambda timeout: joined.append(timeout))  # type: ignore[assignment]

    client.shutdown()
    client.shutdown()

    assert client._stopped is True
    assert client._queue.qsize() == 1 and client._queue.get_nowait() is None
    assert joined == [2.0]


def test_shutdown_tolerates_a_full_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TelemetryClient()
    monkeypatch.setattr(client, "_queue", queue.Queue(maxsize=1))
    client._queue.put_nowait(_record())  # type: ignore[arg-type]

    client.shutdown()

    assert client._stopped is True


# ── emitter: startup ─────────────────────────────────────────


class _FakeThread:
    started: list[_FakeThread] = []

    def __init__(self, *, target: object, name: str, daemon: bool) -> None:
        self.target, self.name, self.daemon = target, name, daemon

    def start(self) -> None:
        _FakeThread.started.append(self)


def test_ensure_started_launches_config_and_consumer_threads_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeThread.started = []
    registered: list[object] = []
    monkeypatch.setattr(
        client_mod,
        "threading",
        SimpleNamespace(Thread=_FakeThread, Event=threading.Event, Lock=threading.Lock),
    )
    monkeypatch.setattr(client_mod, "atexit", SimpleNamespace(register=registered.append))
    client = TelemetryClient()

    client._ensure_started()
    client._ensure_started()

    assert [t.name for t in _FakeThread.started] == [
        "OmnigentTelemetryConfig",
        "OmnigentTelemetryConsumer",
    ]
    assert all(t.daemon for t in _FakeThread.started)
    assert registered == [client._atexit_callback]


# ── emitter: config loading ──────────────────────────────────


def test_load_config_stores_resolved_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = TelemetryConfig(ingestion_url=_INGEST)
    monkeypatch.setattr(client_mod, "_fetch_remote_config", lambda: config)
    client = TelemetryClient()

    client._load_config()

    assert client._config is config
    assert client._config_ready.is_set() and client._stopped is False


def test_load_config_stops_client_when_telemetry_is_disabled_remotely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_mod, "_fetch_remote_config", lambda: None)
    client = TelemetryClient()

    client._load_config()

    assert client._stopped is True and client._config_ready.is_set()
    # The consumer is woken with a poison pill.
    assert client._queue.get_nowait() is None


def test_load_config_tolerates_full_queue_and_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_mod, "_fetch_remote_config", lambda: None)
    full = TelemetryClient()
    monkeypatch.setattr(full, "_queue", queue.Queue(maxsize=1))
    full._queue.put_nowait(_record())  # type: ignore[arg-type]
    full._load_config()
    assert full._stopped is True and full._config_ready.is_set()

    def exploding() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client_mod, "_fetch_remote_config", exploding)
    broken = TelemetryClient()
    broken._load_config()
    assert broken._stopped is True and broken._config_ready.is_set()


def test_atexit_callback_shuts_down_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    client = TelemetryClient()
    client._atexit_callback()
    assert client._stopped is True

    def failing() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(TelemetryClient, "shutdown", lambda self: failing())
    TelemetryClient()._atexit_callback()


# ── emitter: consumer loop ───────────────────────────────────


def _consume(
    client: TelemetryClient, sent: list[list[str]], *, config: TelemetryConfig | None
) -> None:
    """Run the consumer synchronously, recording each batch's event names."""
    client._config = config
    client._config_ready.set()
    client._send = lambda records, url: sent.append(  # type: ignore[method-assign]
        [r["data"]["event_name"] for r in records]
    )
    client._consumer()


def test_consumer_discards_backlog_without_config() -> None:
    client = TelemetryClient()
    client._queue.put_nowait(_record())  # type: ignore[arg-type]
    sent: list[list[str]] = []

    _consume(client, sent, config=None)

    assert sent == [] and client._queue.empty()
    client._queue.join()  # every discarded record was acknowledged


def test_consumer_batches_and_filters_killed_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "_BATCH_SIZE", 2)
    client = TelemetryClient()
    for name in ("A", "Killed", "B", "C", "D"):
        client._queue.put_nowait(_record(name))  # type: ignore[arg-type]
    client._queue.put_nowait(None)
    sent: list[list[str]] = []

    _consume(
        client, sent, config=TelemetryConfig(ingestion_url=_INGEST, disable_events={"Killed"})
    )

    # Killed is dropped at send time; batches fill to the configured size.
    assert sent == [["A", "B"], ["C", "D"]]
    client._queue.join()


@pytest.mark.xfail(
    strict=True,
    reason="the poison-pill flush does not clear `pending`, so the final batch is sent twice",
)
def test_consumer_sends_a_partial_batch_once_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "_BATCH_SIZE", 10)
    client = TelemetryClient()
    client._queue.put_nowait(_record("A"))  # type: ignore[arg-type]
    client._queue.put_nowait(None)
    sent: list[list[str]] = []

    _consume(client, sent, config=TelemetryConfig(ingestion_url=_INGEST))

    assert sent == [["A"]]


def test_consumer_flushes_pending_records_on_idle_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_mod, "_BATCH_SIZE", 100)
    monkeypatch.setattr(client_mod, "_BATCH_INTERVAL_S", 10.0)
    # monotonic() reads: loop start, after receiving A (too young to flush),
    # the idle tick (batch is now old enough), and the flush timestamp.
    clock = iter([0.0, 1.0, 20.0, 20.0])
    monkeypatch.setattr(client_mod, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    script = iter([_record("A"), queue.Empty, None])

    class _Scripted:
        def get(self, timeout: float) -> object:
            item = next(script)
            if item is queue.Empty:
                raise queue.Empty
            return item

        def task_done(self) -> None:
            pass

        def empty(self) -> bool:
            return True

    client = TelemetryClient()
    monkeypatch.setattr(client, "_queue", _Scripted())
    sent: list[list[str]] = []

    _consume(client, sent, config=TelemetryConfig(ingestion_url=_INGEST))

    assert sent == [["A"]]


def test_consumer_drains_remaining_records_after_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_mod, "_BATCH_SIZE", 1)
    client = TelemetryClient()
    for name in ("A", "B", "Killed"):
        client._queue.put_nowait(_record(name))  # type: ignore[arg-type]
    client._queue.put_nowait(None)
    sent: list[list[str]] = []

    def send_then_stop(records: list[dict[str, object]], url: str) -> None:
        sent.append([r["data"]["event_name"] for r in records])  # type: ignore[index]
        client._stopped = True

    client._config = TelemetryConfig(ingestion_url=_INGEST, disable_events={"Killed"})
    client._config_ready.set()
    monkeypatch.setattr(client, "_send", send_then_stop)

    client._consumer()

    # A goes out normally; stopping mid-run still ships what was already queued.
    assert sent == [["A"], ["B"]]
    assert client._queue.empty()


# ── emitter: send ────────────────────────────────────────────


def test_send_posts_records_as_json(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[object] = []

    def fake_urlopen(req: object, timeout: float) -> _Response:
        requests.append((req, timeout))
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    TelemetryClient()._send([_record("A")], _INGEST)  # type: ignore[list-item]

    ((req, timeout),) = requests  # type: ignore[misc]
    assert req.full_url == _INGEST and req.get_method() == "POST"  # type: ignore[attr-defined]
    assert req.get_header("Content-type") == "application/json"  # type: ignore[attr-defined]
    assert json.loads(req.data) == {"records": [_record("A")]}  # type: ignore[attr-defined]
    assert timeout == 3


def test_send_skips_empty_batches_and_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def failing(req: object, timeout: float) -> None:
        calls.append(req)
        raise OSError("offline")

    monkeypatch.setattr("urllib.request.urlopen", failing)
    client = TelemetryClient()

    client._send([], _INGEST)
    assert calls == []

    client._send([_record()], _INGEST)  # type: ignore[list-item]
    assert len(calls) == 1


# ── module-level singleton ───────────────────────────────────


class _FakeClient:
    instances: list[_FakeClient] = []

    def __init__(self) -> None:
        self.started = 0
        self.emitted: list[object] = []
        _FakeClient.instances.append(self)

    def _ensure_started(self) -> None:
        self.started += 1

    def emit(self, event: object) -> None:
        self.emitted.append(event)


@pytest.fixture()
def fake_client_class(monkeypatch: pytest.MonkeyPatch, enabled: None) -> type[_FakeClient]:
    _FakeClient.instances = []
    monkeypatch.setattr(client_mod, "TelemetryClient", _FakeClient)
    monkeypatch.setattr(
        "omnigent.telemetry.installation_id.get_installation_id", lambda: "install-1"
    )
    return _FakeClient


def test_init_client_creates_and_starts_one_client(fake_client_class: type[_FakeClient]) -> None:
    init_client()
    init_client(config={"telemetry": True})

    (instance,) = fake_client_class.instances
    assert get_client() is instance
    assert instance.started == 1


def test_init_client_is_skipped_when_disabled(
    monkeypatch: pytest.MonkeyPatch, fake_client_class: type[_FakeClient]
) -> None:
    monkeypatch.setattr(client_mod, "is_disabled", lambda: True)

    init_client()

    assert get_client() is None and fake_client_class.instances == []


def test_init_client_survives_installation_id_and_constructor_failures(
    monkeypatch: pytest.MonkeyPatch, fake_client_class: type[_FakeClient]
) -> None:
    def failing_id() -> str:
        raise RuntimeError("no disk")

    monkeypatch.setattr("omnigent.telemetry.installation_id.get_installation_id", failing_id)
    init_client()
    assert get_client() is fake_client_class.instances[0]

    def failing_constructor() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(client_mod, "_CLIENT", None)
    monkeypatch.setattr(client_mod, "TelemetryClient", failing_constructor)
    init_client()
    assert get_client() is None


def test_module_emit_delegates_to_the_singleton(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    client = _FakeClient()
    monkeypatch.setattr(client_mod, "_CLIENT", client)
    event = _Evt()

    emit(event)

    assert client.emitted == [event]


def test_module_emit_is_a_noop_without_client_or_when_disabled(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    emit(_Evt())  # no client initialised: nothing to assert but no error

    client = _FakeClient()
    monkeypatch.setattr(client_mod, "_CLIENT", client)
    monkeypatch.setattr(client_mod, "is_disabled", lambda: True)
    emit(_Evt())

    assert client.emitted == []


def test_module_emit_swallows_client_errors(
    monkeypatch: pytest.MonkeyPatch, enabled: None
) -> None:
    class _Exploding:
        def emit(self, event: object) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(client_mod, "_CLIENT", _Exploding())

    emit(_Evt())
