"""Database clock expressions for shared liveness decisions."""

from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement


class database_epoch_seconds(FunctionElement[int]):
    """Epoch seconds from the database, evaluated after acquiring a write lock."""

    type = BigInteger()
    inherit_cache = True


@compiles(database_epoch_seconds, "sqlite")
def _sqlite_epoch(_element: Any, _compiler: Any, **_kwargs: Any) -> str:
    return "CAST(strftime('%s', 'now') AS BIGINT)"


@compiles(database_epoch_seconds, "mysql")
def _mysql_epoch(_element: Any, _compiler: Any, **_kwargs: Any) -> str:
    return "CAST(UNIX_TIMESTAMP() AS SIGNED)"


@compiles(database_epoch_seconds, "postgresql")
@compiles(database_epoch_seconds, "cockroachdb")
def _postgres_epoch(_element: Any, _compiler: Any, **_kwargs: Any) -> str:
    return "CAST(FLOOR(EXTRACT(EPOCH FROM clock_timestamp())) AS BIGINT)"
