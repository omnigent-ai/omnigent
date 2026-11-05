"""Server-side evaluation of runtime flags for hosts and runners.

A deployment plugs in a :class:`RuntimeFlagProvider` to answer
``POST /v1/runtime-flags``. Without one, the server serves no flags and every
client keeps its declared defaults.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from omnigent.runtime_flags import RuntimeFlagType, RuntimeFlagValue


@dataclass(frozen=True)
class RuntimeFlagQuery:
    """One flag a client asked for.

    :param name: Flag name, e.g. ``"example.newPath"``.
    :param value_type: Declared type, e.g. ``"bool"``.
    :param default_value: The client's code default, e.g. ``False``.
    """

    name: str
    value_type: RuntimeFlagType
    default_value: RuntimeFlagValue


@dataclass(frozen=True)
class RuntimeFlagCaller:
    """The authenticated caller a flag is evaluated for.

    :param user_id: Authenticated user, e.g. ``"alice@example.com"``;
        ``None`` when the server runs without authentication.
    """

    user_id: str | None


class RuntimeFlagProvider(Protocol):
    """Evaluates runtime flags for a caller."""

    async def evaluate(
        self, queries: Sequence[RuntimeFlagQuery], caller: RuntimeFlagCaller
    ) -> Mapping[str, RuntimeFlagValue]:
        """Return values for the queried flags this provider serves.

        Leave a flag out to have the client use its default. Raise to report
        that evaluation is unavailable; clients then keep their last values.

        :param queries: Flags requested by one client, with unique names.
        :param caller: Identity resolved from the request's credentials.
        :returns: Values by flag name; values of the wrong type are dropped.
        """
        ...


class StaticRuntimeFlagProvider:
    """Serves a fixed set of values, e.g. from deployment configuration."""

    def __init__(self, values: Mapping[str, RuntimeFlagValue]) -> None:
        """:param values: Values by flag name, e.g. ``{"example.newPath": True}``."""
        self._values = dict(values)

    async def evaluate(
        self, queries: Sequence[RuntimeFlagQuery], caller: RuntimeFlagCaller
    ) -> Mapping[str, RuntimeFlagValue]:
        """Return the configured values for the queried names."""
        del caller
        return {
            query.name: self._values[query.name] for query in queries if query.name in self._values
        }
