"""Async queue-based telemetry emitter.

Errors are silently swallowed — telemetry must never disrupt the
application.  All opt-out signals are checked in :func:`is_disabled`.

Wire format (matches the API Gateway / Kinesis ingestion schema):

    POST <ingestion_url>
    {
        "records": [
            {
                "data": {
                    "event_name": "SessionCreatedEvent",
                    "session_id": "<telemetry-session-uuid>",
                    "omnigent_version": "0.4.2",
                    "schema_version": 1,
                    "python_version": "3.12.3",
                    "operating_system": "Linux",
                    "timestamp_ns": 1720000000000000000,
                    "status": "success",
                    "duration_ms": 0,
                    "installation_id": "<uuid>",
                    "environment": null,
                    "params": "{\"agent_id\": \"...\", ...}"
                },
                "partition-key": "<random-uuid>"
            }
        ]
    }

``session_id`` is a per-process UUID that groups all events from one
server run — it is NOT the Omnigent conversation id (which goes in
``params``).  ``params`` is a JSON-encoded string of event-specific
fields.  ``additionalProperties: false`` on the gateway means any field
not in the schema above will cause a 400, so event-specific data must
live in ``params``.

Remote config
~~~~~~~~~~~~~
On startup a daemon thread fetches a JSON config from a versioned URL::

    https://config.omnigent-telemetry.io/{version}.json          (prod)
    https://config-staging.omnigent-telemetry.io/{version}.json  (dev/pre-release)

The config shape::

    {
        "omnigent_version": "0.5.0",
        "ingestion_url": "https://...",   # required; disables telemetry if absent
        "disable_telemetry": false,       # kill-switch
        "disable_events": [],             # per-event kill-switch list
        "disable_os": [],                 # e.g. ["Windows"]
        "rollout_percentage": 100         # 0-100; probabilistic rollout
    }

If the config fetch fails or the config disables telemetry, the client
stops itself and drops all pending events.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import queue
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.version import VERSION

_logger = logging.getLogger(__name__)

# CI / test environment variable names that indicate telemetry should be off.
_CI_ENV_VARS = frozenset(
    {
        "CI",
        "GITHUB_ACTIONS",
        "PYTEST_CURRENT_TEST",
        "CIRCLECI",
        "JENKINS_URL",
        "TRAVIS",
        "GITLAB_CI",
        "TF_BUILD",
        "BITBUCKET_BUILD_NUMBER",
        "CODEBUILD_BUILD_ARN",
        "BUILDKITE",
        "TEAMCITY_VERSION",
    }
)

_BATCH_SIZE = 50
_BATCH_INTERVAL_S = 10.0
_MAX_QUEUE_SIZE = 512
_SCHEMA_VERSION = 1
_CONFIG_FETCH_TIMEOUT_S = 2.0

# Remote config base URLs. Dev/pre-release versions use staging.
_CONFIG_URL_PROD = "https://config.omnigent-telemetry.io"
_CONFIG_URL_STAGING = "https://config-staging.omnigent-telemetry.io"


# Cached result of is_disabled() — computed once on first call, then reused.
# Using a list so it's mutable from within the function (avoids global keyword).
_IS_DISABLED_CACHE: list[bool | None] = [None]


@dataclass
class TelemetryConfig:
    """Resolved remote configuration for the telemetry client."""

    ingestion_url: str
    disable_events: set[str] = field(default_factory=set)


def _config_url() -> str:
    """Return the remote config URL for the running version.

    ``OMNIGENT_TELEMETRY_CONFIG_URL`` overrides for local testing.
    Dev/pre-release versions use the staging URL.
    """
    override = os.environ.get("OMNIGENT_TELEMETRY_CONFIG_URL", "").strip()
    if override:
        return f"{override.rstrip('/')}/{VERSION}.json"
    try:
        from packaging.version import Version

        v = Version(VERSION)
        if v.is_devrelease or v.is_prerelease:
            return f"{_CONFIG_URL_STAGING}/{VERSION}.json"
    except Exception:
        _logger.debug("Version parse failed; using production config URL", exc_info=True)
    return f"{_CONFIG_URL_PROD}/{VERSION}.json"


def _fetch_remote_config() -> TelemetryConfig | None:
    """Fetch and validate the remote telemetry config.

    :returns: :class:`TelemetryConfig` on success, ``None`` when
        telemetry should be disabled (fetch failure, kill-switch, OS
        exclusion, or rollout exclusion).
    """
    try:
        import urllib.request

        url = _config_url()
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=_CONFIG_FETCH_TIMEOUT_S) as resp:
            cfg: dict[str, Any] = json.loads(resp.read().decode("utf-8"))

        if cfg.get("omnigent_version") != VERSION:
            _logger.debug("Telemetry config version mismatch; disabling telemetry")
            return None
        if cfg.get("disable_telemetry") is True:
            _logger.debug("Telemetry disabled by remote config kill-switch")
            return None
        ingestion_url: str | None = cfg.get("ingestion_url")
        if not ingestion_url:
            _logger.debug("Telemetry config missing ingestion_url; disabling telemetry")
            return None
        if platform.system() in cfg.get("disable_os", []):
            _logger.debug("Telemetry disabled for OS %s by remote config", platform.system())
            return None
        rollout = cfg.get("rollout_percentage", 100)
        if random.random() * 100 >= rollout:
            _logger.debug("Telemetry excluded by rollout_percentage=%s", rollout)
            return None

        return TelemetryConfig(
            ingestion_url=ingestion_url,
            disable_events=set(cfg.get("disable_events", [])),
        )
    except Exception:
        _logger.debug("Telemetry config fetch failed; disabling telemetry", exc_info=True)
        return None


def _config_telemetry_disabled() -> bool:
    """Return ``True`` when ``telemetry: false`` is set in config.yaml.

    Reads ``~/.omnigent/config.yaml`` (honouring ``OMNIGENT_CONFIG_HOME``).
    Returns ``False`` on any error so a missing/malformed config never
    silently suppresses telemetry.
    """
    try:
        import re as _re

        config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
        if config_home:
            config_path = Path(config_home) / "config.yaml"
        else:
            config_path = Path.home() / ".omnigent" / "config.yaml"
        if not config_path.exists():
            return False
        # Read raw text and match `telemetry: false` directly to avoid
        # PyYAML SafeLoader bool-resolver corruption: spec/parser.py's
        # _ConfigYamlLoader subclass modifies the shared
        # SafeLoader.yaml_implicit_resolvers class dict at import time,
        # causing `false` to parse as a string rather than a boolean.
        text = config_path.read_text(encoding="utf-8")
        return bool(
            _re.search(r"^\s*telemetry\s*:\s*false\s*$", text, _re.IGNORECASE | _re.MULTILINE)
        )
    except Exception:
        return False


def is_disabled() -> bool:
    """Return ``True`` when telemetry should be completely suppressed.

    Result is cached after the first call — env vars and config.yaml are
    checked once at startup and not re-read on every emit, so there is no
    per-request I/O overhead.

    Checks (in order):
    1. ``OMNIGENT_ANALYTICS=0``
    2. ``DISABLE_TELEMETRY=true`` or ``OMNIGENT_DISABLE_TELEMETRY=true``
    3. ``DO_NOT_TRACK=1``
    4. Any CI environment variable from :data:`_CI_ENV_VARS`
    5. ``telemetry: false`` in ``~/.omnigent/config.yaml``

    Always returns a ``bool``; never raises.
    """
    if _IS_DISABLED_CACHE[0] is not None:
        return _IS_DISABLED_CACHE[0]
    try:
        result = _compute_is_disabled()
    except Exception:
        result = True
    _IS_DISABLED_CACHE[0] = result
    return result


def _compute_is_disabled() -> bool:
    """Compute whether telemetry is disabled (uncached)."""
    if os.environ.get("OMNIGENT_ANALYTICS", "").strip() == "0":
        return True
    for var in ("DISABLE_TELEMETRY", "OMNIGENT_DISABLE_TELEMETRY"):
        if os.environ.get(var, "").strip().lower() in ("1", "true", "yes"):
            return True
    if os.environ.get("DO_NOT_TRACK", "").strip() == "1":
        return True
    if any(var in os.environ for var in _CI_ENV_VARS):
        return True
    return _config_telemetry_disabled()


def _detect_environment() -> str | None:
    """Return a short environment tag or ``None`` for plain installs."""
    try:
        checks: list[tuple[str, str]] = [
            ("KAGGLE_KERNEL_RUN_TYPE", "kaggle"),
            ("COLAB_BACKEND_VERSION", "colab"),
            ("AZUREML_ARM_WORKSPACE_NAME", "azure_ml"),
            ("SM_CURRENT_HOST", "sagemaker_studio"),
        ]
        for env_var, tag in checks:
            if os.environ.get(env_var):
                return tag
        # Docker: /.dockerenv exists in containers
        if os.path.exists("/.dockerenv"):
            return "docker"
        return None
    except Exception:
        return None


def _build_record(event: object) -> dict[str, Any]:
    """Serialise *event* into the gateway ``data`` envelope.

    Event-specific fields (everything except ``installation_id``) are
    JSON-encoded into the ``params`` string so the gateway schema's
    ``additionalProperties: false`` constraint is satisfied.
    """
    from dataclasses import asdict

    fields: dict[str, Any] = asdict(event)  # type: ignore[arg-type]
    installation_id: str | None = fields.pop("installation_id", None)
    session_id: str | None = fields.pop("session_id", None)
    anon_user_id: str | None = fields.pop("anon_user_id", None)

    # All remaining event-specific fields go into params as a JSON string.
    params_str: str | None = None
    if fields:
        params_str = json.dumps(fields, default=str)

    data: dict[str, Any] = {
        "event_name": type(event).__name__,
        "session_id": session_id or "",
        "omnigent_version": VERSION,
        "schema_version": _SCHEMA_VERSION,
        "python_version": sys.version.split()[0],
        "operating_system": platform.system(),
        "timestamp_ns": time.time_ns(),
        "status": "success",
        "duration_ms": 0,
        "installation_id": installation_id,
        "anon_user_id": anon_user_id,
        "environment": _detect_environment(),
        "params": params_str,
    }
    return {
        "data": data,
        "partition-key": str(uuid.uuid4()),
    }


class TelemetryClient:
    """Fire-and-forget telemetry emitter backed by a background thread.

    On startup a daemon thread fetches the remote config (ingestion URL,
    kill-switches, rollout percentage).  The consumer thread buffers events
    until the config is resolved, then sends or discards them.  If the
    config fetch fails or signals ``disable_telemetry``, the client stops
    itself.

    All errors are suppressed — telemetry must never disrupt the application.
    """

    def __init__(self) -> None:
        self._config: TelemetryConfig | None = None
        self._config_ready = threading.Event()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False
        self._atexit_registered = False
        self._thread: threading.Thread | None = None
        self._config_thread: threading.Thread | None = None

    # ── Public interface ─────────────────────────────────

    def emit(self, event: object) -> None:
        """Queue an event for async delivery.

        Accepts any dataclass; converts it to the gateway wire format.
        Skips events listed in ``config.disable_events``.
        Silently no-ops when disabled or stopped.

        :param event: A dataclass instance, e.g. :class:`SessionCreatedEvent`.
        """
        if self._stopped:
            return
        # Defense-in-depth: re-check opt-out inside the client so
        # a late env-var change is respected even if the call site
        # skipped the module-level is_disabled() guard.
        if is_disabled():
            return
        try:
            event_name = type(event).__name__
            # If config is already resolved, check per-event kill-switch.
            if self._config_ready.is_set() and self._config is not None:
                if event_name in self._config.disable_events:
                    return
            record = _build_record(event)
            self._ensure_started()
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                pass  # queue full — drop event; telemetry must never block
        except Exception:
            _logger.debug("Telemetry emit failed; dropping event", exc_info=True)

    def flush(self) -> None:
        """Block until the queue is empty (used in tests and at shutdown)."""
        try:
            self._queue.join()
        except Exception:
            pass  # best-effort flush; never raise from telemetry

    def shutdown(self) -> None:
        """Signal the background thread to stop and wait briefly."""
        if self._stopped:
            return
        self._stopped = True
        try:
            self._queue.put_nowait(None)  # poison pill
        except Exception:
            pass  # queue may be full/closed; best-effort shutdown
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # ── Internal helpers ─────────────────────────────────

    def _ensure_started(self) -> None:
        if self._started:
            return
        with self._lock:
            if self._started:
                return
            # Config fetch runs in its own daemon thread so it never blocks
            # the first emit call.
            self._config_thread = threading.Thread(
                target=self._load_config,
                name="OmnigentTelemetryConfig",
                daemon=True,
            )
            self._config_thread.start()

            self._thread = threading.Thread(
                target=self._consumer,
                name="OmnigentTelemetryConsumer",
                daemon=True,
            )
            self._thread.start()
            self._started = True
            if not self._atexit_registered:
                atexit.register(self._atexit_callback)
                self._atexit_registered = True

    def _load_config(self) -> None:
        """Daemon thread: fetch remote config then signal the consumer."""
        try:
            cfg = _fetch_remote_config()
            if cfg is None:
                # Kill-switch or fetch failure — stop the client.
                self._stopped = True
                try:
                    self._queue.put_nowait(None)  # unblock consumer
                except queue.Full:
                    pass
            else:
                self._config = cfg
        except Exception:
            _logger.debug("Telemetry config load failed; stopping client", exc_info=True)
            self._stopped = True
        finally:
            self._config_ready.set()

    def _atexit_callback(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass  # best-effort shutdown at process exit; telemetry must never disrupt termination

    def _consumer(self) -> None:
        """Background thread: wait for config, then drain the queue in batches."""
        # Wait for config to be resolved before sending anything.
        self._config_ready.wait()

        if self._stopped or self._config is None:
            # Config fetch failed or kill-switched — drain and discard.
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
            return

        ingestion_url = self._config.ingestion_url
        disable_events = self._config.disable_events
        pending: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        while not self._stopped:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                if pending and time.monotonic() - last_flush >= _BATCH_INTERVAL_S:
                    self._send(pending, ingestion_url)
                    pending = []
                    last_flush = time.monotonic()
                continue

            if item is None:
                # Poison pill — flush what we have, then exit.
                if pending:
                    self._send(pending, ingestion_url)
                self._queue.task_done()
                break

            # Apply per-event kill-switch at send time too (config may have
            # arrived after the event was queued).
            event_name = (item.get("data") or {}).get("event_name", "")
            if event_name not in disable_events:
                pending.append(item)
            self._queue.task_done()

            if len(pending) >= _BATCH_SIZE or time.monotonic() - last_flush >= _BATCH_INTERVAL_S:
                self._send(pending, ingestion_url)
                pending = []
                last_flush = time.monotonic()

        # Drain remaining on stop.
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    event_name = (item.get("data") or {}).get("event_name", "")
                    if event_name not in disable_events:
                        pending.append(item)
                self._queue.task_done()
            except queue.Empty:
                break
        if pending:
            self._send(pending, ingestion_url)

    def _send(self, records: list[dict[str, Any]], ingestion_url: str) -> None:
        """POST a batch to the ingestion endpoint."""
        if not records:
            return
        try:
            import urllib.request

            body = json.dumps({"records": records}).encode("utf-8")
            req = urllib.request.Request(
                ingestion_url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                resp.read()
        except Exception:
            _logger.debug("Telemetry send failed; dropping batch", exc_info=True)


# ── Module-level singleton ───────────────────────────────

_CLIENT: TelemetryClient | None = None
_CLIENT_LOCK = threading.Lock()


def get_client() -> TelemetryClient | None:
    """Return the active singleton client, or ``None`` when disabled."""
    return _CLIENT


def init_client(*, config: dict[str, Any] | None = None) -> None:
    """Initialise the module-level client if telemetry is enabled.

    Safe to call multiple times; idempotent after the first call.

    :param config: Optional parsed server config dict (e.g. from ``-c
        config.yaml``).  When ``config.get("telemetry") is False``
        telemetry is disabled regardless of env vars.
    """
    global _CLIENT
    if is_disabled():
        return
    if config is not None and config.get("telemetry") is False:
        return
    # Prime the installation-id cache on startup so later request
    # handlers do not perform synchronous file I/O on the event loop.
    try:
        from omnigent.telemetry.installation_id import get_installation_id

        get_installation_id()
    except Exception:
        _logger.debug("Telemetry installation-id prime failed", exc_info=True)
    with _CLIENT_LOCK:
        if _CLIENT is None:
            try:
                _CLIENT = TelemetryClient()
                # Start threads eagerly at init time so the config fetch
                # runs in the background before the first event arrives.
                _CLIENT._ensure_started()
            except Exception:
                _logger.debug("TelemetryClient init failed", exc_info=True)


def emit(event: object) -> None:
    """Emit an event through the module-level client.

    No-op when telemetry is disabled or the client is not initialised.
    Never raises.

    :param event: A dataclass instance.
    """
    try:
        if is_disabled():
            return
        client = _CLIENT
        if client is not None:
            client.emit(event)
    except Exception:
        _logger.debug(
            "Telemetry emit failed; swallowing to avoid disrupting application", exc_info=True
        )
