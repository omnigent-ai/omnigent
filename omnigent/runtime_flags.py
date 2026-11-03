"""Runtime flags that hosts and runners read from the Omnigent server while running.

A process declares each flag once at module level, with its name as a string
literal and a typed default, for example ``BoolFeatureFlag(<name>, False)``,
then calls ``get_current_value()`` on every use.

A :class:`RuntimeFlagRefresher` asks the server (``POST /v1/runtime-flags``) for
every declared flag and keeps the latest answer in memory, so reads never block.
A flag the server does not serve, or a value of the wrong type, reads as the
declared default. A failed refresh keeps the previous answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar, Generic, Literal, TypeVar

import httpx

_logger = logging.getLogger(__name__)

RUNTIME_FLAGS_PATH = "/v1/runtime-flags"

RuntimeFlagType = Literal["bool", "int", "long", "float", "string", "string_list"]
RuntimeFlagValue = bool | int | float | str | list[str]

RUNTIME_FLAG_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$"
MAX_RUNTIME_FLAGS_PER_REQUEST = 500

DEFAULT_REFRESH_INTERVAL_S = 60.0
_MIN_REFRESH_INTERVAL_S = 15.0
_MAX_REFRESH_INTERVAL_S = 3600.0
# An older server has no runtime-flag route; check again rarely.
_UNSUPPORTED_SERVER_RETRY_S = 3600.0
_REQUEST_TIMEOUT_S = 30.0
_REFRESH_JITTER = 0.1

_NAME_RE = re.compile(RUNTIME_FLAG_NAME_PATTERN)
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

T = TypeVar("T")


def runtime_flag_value_matches(value_type: RuntimeFlagType, value: object) -> bool:
    """Return whether *value* is a valid value for a flag of *value_type*.

    :param value_type: Declared flag type, e.g. ``"bool"``.
    :param value: Candidate value, e.g. ``True`` or ``["a", "b"]``.
    :returns: ``True`` when the value has that type; booleans are not integers.
    """
    if value_type == "bool":
        return isinstance(value, bool)
    if value_type in ("int", "long"):
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        low, high = (_INT32_MIN, _INT32_MAX) if value_type == "int" else (_INT64_MIN, _INT64_MAX)
        return low <= value <= high
    if value_type == "float":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "string_list":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return False


_registry: dict[str, _FeatureFlag[Any]] = {}
_registry_lock = threading.Lock()
# Replaced as a whole on each refresh, so readers never see a partial update.
_values: Mapping[str, RuntimeFlagValue] = MappingProxyType({})


class _FeatureFlag(Generic[T]):
    """A named runtime flag with a typed code default."""

    value_type: ClassVar[RuntimeFlagType]

    def __init__(self, name: str, default_value: T) -> None:
        """Declare a flag and register it with this process.

        :param name: Flag name, e.g. ``"example.newPath"``.
        :param default_value: Value used until the server serves this flag.
        :raises ValueError: If the name or default is invalid, or the name is
            already declared with a different type or default.
        """
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"invalid runtime flag name: {name!r}")
        if not runtime_flag_value_matches(self.value_type, default_value):
            raise ValueError(f"default for {self.value_type} flag {name!r} has the wrong type")
        self.name = name
        self.default_value = default_value
        with _registry_lock:
            existing = _registry.get(name)
            if existing is None:
                _registry[name] = self
            elif existing.value_type != self.value_type or existing.default_value != default_value:
                raise ValueError(f"runtime flag {name!r} is already declared differently")

    def get_current_value(self) -> T:
        """Return the latest value the server served, else the default.

        :returns: The served value, or :attr:`default_value`.
        """
        value = _values.get(self.name)
        if value is None or not runtime_flag_value_matches(self.value_type, value):
            value = self.default_value
        if isinstance(value, list):
            return list(value)  # type: ignore[return-value]
        if self.value_type == "float":
            return float(value)  # type: ignore[return-value,arg-type]
        return value  # type: ignore[return-value]


class BoolFeatureFlag(_FeatureFlag[bool]):
    """A boolean runtime flag."""

    value_type = "bool"


class IntFeatureFlag(_FeatureFlag[int]):
    """A 32-bit integer runtime flag."""

    value_type = "int"


class LongFeatureFlag(_FeatureFlag[int]):
    """A 64-bit integer runtime flag."""

    value_type = "long"


class FloatFeatureFlag(_FeatureFlag[float]):
    """A floating-point runtime flag."""

    value_type = "float"


class StringFeatureFlag(_FeatureFlag[str]):
    """A string runtime flag."""

    value_type = "string"


class StringListFeatureFlag(_FeatureFlag[list[str]]):
    """A string-list runtime flag."""

    value_type = "string_list"


def declared_runtime_flags() -> tuple[_FeatureFlag[Any], ...]:
    """Return every flag declared in this process, sorted by name."""
    with _registry_lock:
        return tuple(_registry[name] for name in sorted(_registry))


def _apply_served_values(flags: Sequence[_FeatureFlag[Any]], served: object) -> None:
    """Replace the in-memory values with the server's answer for *flags*."""
    global _values
    served_values = served if isinstance(served, dict) else {}
    accepted: dict[str, RuntimeFlagValue] = {}
    for flag in flags:
        value = served_values.get(flag.name)
        if value is not None and runtime_flag_value_matches(flag.value_type, value):
            accepted[flag.name] = value
    _values = MappingProxyType(accepted)


RuntimeFlagPost = Callable[..., Awaitable[httpx.Response]]


class RuntimeFlagRefresher:
    """Periodically fetches this process's declared flags from the server."""

    def __init__(
        self,
        post: RuntimeFlagPost,
        *,
        default_interval_s: float = DEFAULT_REFRESH_INTERVAL_S,
    ) -> None:
        """Create a refresher.

        :param post: Sends ``post(RUNTIME_FLAGS_PATH, json=payload)`` to the
            server with this process's credentials, e.g. a server client's
            ``post`` method.
        :param default_interval_s: Delay between refreshes when the server
            does not suggest one.
        """
        self._post = post
        self._default_interval_s = default_interval_s
        self._task: asyncio.Task[None] | None = None
        self._unsupported_logged = False

    async def refresh_once(self) -> float:
        """Fetch the declared flags once and store the server's answer.

        :returns: Seconds to wait before the next refresh.
        """
        flags = declared_runtime_flags()
        if not flags:
            return self._default_interval_s
        payload = {
            "flags": [
                {"name": flag.name, "type": flag.value_type, "default": flag.default_value}
                for flag in flags[:MAX_RUNTIME_FLAGS_PER_REQUEST]
            ]
        }
        try:
            response = await asyncio.wait_for(
                self._post(RUNTIME_FLAGS_PATH, json=payload), timeout=_REQUEST_TIMEOUT_S
            )
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            _logger.debug("runtime flag refresh failed: %s", type(exc).__name__)
            return self._default_interval_s
        if response.status_code in (404, 405):
            if not self._unsupported_logged:
                _logger.info("server does not serve runtime flags; using defaults")
                self._unsupported_logged = True
            return _UNSUPPORTED_SERVER_RETRY_S
        if response.status_code != 200:
            _logger.debug("runtime flag refresh returned HTTP %d", response.status_code)
            return self._default_interval_s
        try:
            body = response.json()
        except ValueError:
            _logger.debug("runtime flag refresh returned invalid JSON")
            return self._default_interval_s
        if not isinstance(body, dict):
            return self._default_interval_s
        _apply_served_values(flags[:MAX_RUNTIME_FLAGS_PER_REQUEST], body.get("values"))
        suggested = body.get("refresh_after_s")
        if isinstance(suggested, int | float) and not isinstance(suggested, bool):
            return min(max(float(suggested), _MIN_REFRESH_INTERVAL_S), _MAX_REFRESH_INTERVAL_S)
        return self._default_interval_s

    async def _run(self) -> None:
        while True:
            try:
                delay = await self.refresh_once()
            except Exception:
                _logger.exception("runtime flag refresh crashed")
                delay = self._default_interval_s
            jitter = 1 + random.uniform(-_REFRESH_JITTER, _REFRESH_JITTER)
            await asyncio.sleep(delay * jitter)

    def start(self) -> None:
        """Start refreshing in the background on the running event loop."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="runtime-flag-refresh")

    async def aclose(self) -> None:
        """Stop the background refresh."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
