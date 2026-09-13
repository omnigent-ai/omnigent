"""Shared JSON type contracts.

``JsonObject`` is an open object bag whose values are narrowed at use sites.
``JsonValue`` is recursively constrained to JSON-serializable values.
"""

from typing import TypeAlias

JsonObject: TypeAlias = dict[str, object]
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
