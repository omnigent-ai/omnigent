"""A binary column type that enforces its byte limit on every dialect."""

from __future__ import annotations

from sqlalchemy import LargeBinary
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class BoundedBinary(TypeDecorator[bytes]):
    """Bytes stored in a binary column, rejected on write when over a byte limit.

    PostgreSQL ``BYTEA`` has no length limit and MySQL picks a ``BLOB`` size
    from the length, so the limit is checked here rather than by the column.

    :param max_bytes: Most bytes a value may take. A larger value raises
        ``ValueError`` on write.
    """

    impl = LargeBinary
    cache_ok = True

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self.max_bytes = max_bytes

    def process_bind_param(self, value: bytes | None, dialect: Dialect) -> bytes | None:
        del dialect
        if value is not None and len(value) > self.max_bytes:
            raise ValueError(f"stored value exceeds {self.max_bytes} bytes")
        return value

    def process_result_value(
        self, value: bytes | memoryview | None, dialect: Dialect
    ) -> bytes | None:
        del dialect
        # Some drivers return a memoryview.
        return None if value is None else bytes(value)
