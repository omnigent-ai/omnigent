"""Unity Catalog function execution for the runner.

Executes UC SQL functions declared via ``catalog_path:`` in agent
YAML tool definitions. Functions are called through the Databricks
SQL Statement Execution API (``WorkspaceClient
.statement_execution.execute_statement()``), which supports
parameterized queries and avoids SQL injection.

The ``WorkspaceClient`` is constructed once per profile and cached
for the lifetime of the process — UC function calls are frequent
during agent turns and workspace auth resolution is expensive (SDK
reads ``~/.databrickscfg``, fetches OAuth tokens, etc.).

Design decisions:

- **Statement Execution over direct invoke**: UC functions are SQL
  objects; the canonical invocation path is ``SELECT func(args)``.
  The SDK's ``FunctionsAPI`` has no ``execute`` method — it only
  supports CRUD on function metadata. Statement Execution is the
  only SDK path that runs a function and returns results.
- **Warehouse ID resolution**: Statement Execution needs a SQL
  warehouse. The tool YAML can declare ``warehouse_id:`` per tool;
  when absent, the ``DATABRICKS_WAREHOUSE_ID`` environment variable
  is read at runtime as a convenience fallback.
- **Parameter schema**: The LLM must know what arguments a UC
  function accepts. The YAML's ``parameters:`` block is the source
  of truth (populated by the author or fetched from UC metadata at
  agent-build time — the latter is a future enhancement).
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import TYPE_CHECKING

from omnigent.debug_logging import runner_primary_session_id

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

_logger = logging.getLogger(__name__)

# Validates catalog_path: bare identifier (e.g. "ai_query") or
# dotted three-level name (e.g. "my_catalog.my_schema.func").
# Rejects backticks, semicolons, parens, and other SQL metacharacters.
_CATALOG_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*$")

# Validates parameter names from the LLM's tool-call arguments.
# Rejects SQL metacharacters (parens, colons, semicolons, etc.).
_PARAM_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


@lru_cache(maxsize=32)
def _get_workspace_client(
    profile: str | None,
) -> WorkspaceClient:
    """
    Construct a cached :class:`WorkspaceClient` for the given
    Databricks profile.

    Caches by ``profile`` so repeated tool calls within a session
    reuse the same authenticated client. The ``maxsize=32`` cap
    prevents unbounded growth in multi-profile deployments.

    :param profile: Databricks config profile name from
        ``~/.databrickscfg``, e.g. ``"oss"``. ``None`` uses the
        SDK's default resolution (``DEFAULT`` section, env vars).
    :returns: An authenticated ``WorkspaceClient`` instance.
    :raises ImportError: If ``databricks-sdk`` is not installed.
    """
    from databricks.sdk import WorkspaceClient as _WSC

    if profile is not None:
        return _WSC(profile=profile)
    return _WSC()


def _build_select_statement(
    catalog_path: str,
    args: dict[str, object],
) -> tuple[str, list[dict[str, str]]]:
    """
    Build a parameterized ``SELECT`` statement for a UC function
    call.

    Uses named parameter markers (``:param_name``) instead of
    string interpolation to prevent SQL injection. The Databricks
    Statement Execution API resolves the markers server-side.

    :param catalog_path: UC function name — either a three-level
        qualified name (``"my_catalog.my_schema.classify"``) or a
        bare built-in function name (``"ai_query"``). Three-level
        names are backtick-quoted to handle special characters;
        bare names are emitted unquoted so built-in SQL functions
        like ``ai_query`` resolve correctly.
    :param args: Argument dict from the LLM, e.g.
        ``{"text": "I love Databricks", "lang": "en"}``.
    :returns: A ``(sql, parameters)`` tuple where ``sql`` is the
        parameterized query string and ``parameters`` is a list
        of ``StatementParameterListItem``-compatible dicts with
        ``name`` and ``value`` keys.
    """
    # Validate catalog_path to prevent SQL injection via crafted
    # tool specs. Only alphanumeric, underscore, and dot are allowed.
    if not _CATALOG_PATH_RE.match(catalog_path):
        raise ValueError(
            f"Invalid catalog_path {catalog_path!r}: must contain only "
            f"alphanumeric characters, underscores, and dots."
        )

    # Validate parameter names from the LLM's tool-call arguments.
    # These are interpolated as ``:name`` markers in the SQL string;
    # SQL metacharacters in keys would alter the query structure.
    for name in args:
        if not _PARAM_NAME_RE.match(name):
            raise ValueError(
                f"Invalid parameter name {name!r} for UC function "
                f"{catalog_path!r}: must be alphanumeric/underscore."
            )

    # Bare names (no dots) are built-in SQL functions like ai_query
    # — emit unquoted. Three-level catalog paths are backtick-quoted
    # so dots inside catalog/schema/function names are not misread
    # as identifier separators.
    func_ref = catalog_path if "." not in catalog_path else f"`{catalog_path}`"

    if not args:
        sql = f"SELECT {func_ref}()"
        return sql, []

    param_names = list(args.keys())
    placeholders = ", ".join(f":{name}" for name in param_names)
    sql = f"SELECT {func_ref}({placeholders})"

    parameters = [
        {"name": name, "value": json.dumps(value) if not isinstance(value, str) else value}
        for name, value in args.items()
    ]
    return sql, parameters


async def execute_uc_function(
    catalog_path: str,
    args: dict[str, object],
    *,
    profile: str | None = None,
    warehouse_id: str | None = None,
) -> str:
    """
    Execute a Unity Catalog function and return the result as a
    string.

    Constructs a parameterized ``SELECT catalog.schema.func(:args)``
    query and executes it via the Databricks SQL Statement Execution
    API. The result is extracted from the response's ``data_array``
    and returned as a JSON string.

    :param catalog_path: Three-level UC function name, e.g.
        ``"my_catalog.my_schema.classify_sentiment"``.
    :param args: Argument dict from the LLM, e.g.
        ``{"text": "I love it"}``.
    :param profile: Databricks config profile name, e.g.
        ``"oss"``. ``None`` uses the SDK's default resolution.
    :param warehouse_id: SQL warehouse ID to execute against, e.g.
        ``"abc123def456"``. When ``None``, falls back to the
        ``DATABRICKS_WAREHOUSE_ID`` environment variable.
    :returns: The function's return value as a JSON string. Scalar
        results are returned directly; multi-row results are
        returned as a JSON array.
    :raises ValueError: If no warehouse ID is available from
        either the parameter or the environment variable.
    :raises RuntimeError: If the statement execution fails or
        returns an unexpected status.
    """
    import asyncio
    import os

    if not warehouse_id:
        warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not warehouse_id:
        raise ValueError(
            f"UC function {catalog_path!r} requires a warehouse_id. "
            f"Set 'warehouse_id' in the tool's YAML definition or "
            f"the DATABRICKS_WAREHOUSE_ID environment variable."
        )

    client = _get_workspace_client(profile)
    sql, parameters = _build_select_statement(catalog_path, args)

    _logger.info(
        "Executing UC function %s on warehouse %s",
        catalog_path,
        warehouse_id,
        extra={"session_id": runner_primary_session_id()},
    )

    # Statement execution is a blocking HTTP call — run in a thread
    # to avoid blocking the event loop.
    from databricks.sdk.service.sql import StatementParameterListItem

    sdk_params = [StatementParameterListItem(name=p["name"], value=p["value"]) for p in parameters]

    response = await asyncio.to_thread(
        client.statement_execution.execute_statement,
        statement=sql,
        warehouse_id=warehouse_id,
        parameters=sdk_params,
    )

    # Check execution status.
    status = response.status
    if status is None:
        raise RuntimeError(
            f"UC function {catalog_path!r}: statement execution returned no status."
        )
    state = status.state
    if state is None:
        raise RuntimeError(f"UC function {catalog_path!r}: statement status has no state.")

    from databricks.sdk.service.sql import StatementState

    if state == StatementState.FAILED:
        error = status.error
        msg = error.message if error else "unknown error"
        raise RuntimeError(f"UC function {catalog_path!r} execution failed: {msg}")
    if state not in (StatementState.SUCCEEDED,):
        raise RuntimeError(
            f"UC function {catalog_path!r}: unexpected state {state.value!r}. Expected SUCCEEDED."
        )

    # Extract result data.
    result = response.result
    if result is None or result.data_array is None:
        _logger.debug(
            "UC function %s returned no result data",
            catalog_path,
            extra={"session_id": runner_primary_session_id()},
        )
        return json.dumps(None)

    data = result.data_array
    _logger.debug(
        "UC function %s result: %s",
        catalog_path,
        data,
        extra={"session_id": runner_primary_session_id()},
    )
    # Single-row, single-column result (common for scalar functions).
    if len(data) == 1 and len(data[0]) == 1:
        return data[0][0] if data[0][0] is not None else json.dumps(None)

    # Multi-row or multi-column: return as JSON array of arrays.
    return json.dumps(data)
