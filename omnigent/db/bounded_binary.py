"""A binary column type that enforces its byte limit on every dialect."""

from __future__ import annotations

from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.dialects.mysql import VARBINARY as MySQLVarbinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class BoundedBinary(TypeDecorator[bytes]):
    """Bytes stored in a binary column, rejected on write when over a byte limit.

    PostgreSQL ``BYTEA`` and SQLite ``BLOB`` have no length, so the limit is
    checked here on every write. MySQL gets a ``VARBINARY`` of exactly that
    length instead of a ``BLOB``, so the column itself is bounded there too.

    :param max_bytes: Most bytes a value may take. A larger value raises
        ``ValueError`` on write.
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self.max_bytes = max_bytes

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "mysql":
            return dialect.type_descriptor(MySQLVarbinary(self.max_bytes))
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: object, dialect: Dialect) -> bytes | None:
        del dialect
        if value is None:
            return None
        # A memoryview's length counts elements, not bytes; measure the bytes.
        if isinstance(value, memoryview):
            value = value.tobytes()
        if not isinstance(value, bytes | bytearray):
            raise TypeError(f"expected bytes, got {type(value).__name__}")
        if len(value) > self.max_bytes:
            raise ValueError(f"stored value exceeds {self.max_bytes} bytes")
        return bytes(value)

    def process_result_value(
        self, value: bytes | memoryview | None, dialect: Dialect
    ) -> bytes | None:
        del dialect
        # Some drivers return a memoryview.
        return None if value is None else bytes(value)
