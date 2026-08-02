"""Shared contracts for open JSON objects and serializable JSON values."""

from typing import TypeAlias

JsonObject: TypeAlias = dict[str, object]
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
