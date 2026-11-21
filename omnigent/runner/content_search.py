"""Bounded ripgrep searches executed inside the session's OS environment."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from omnigent.runner.environment_filesystem import _python_script_command
from omnigent.workspace_content_search import (
    COULD_NOT_RUN,
    MAX_SCRIPT_OUTPUT,
    SCRIPT_TIMEOUT_S,
    ContentSearchError,
    collect_records,
    content_search_script,
)

if TYPE_CHECKING:
    from omnigent.inner.os_env import OSEnvironment


async def search_content(
    environment: OSEnvironment,
    query: str,
    *,
    case_sensitive: bool = False,
    regex: bool = False,
    include: str = "",
    exclude: str = "",
    limit: int = 500,
) -> dict[str, Any]:
    """Search on-disk workspace text without bypassing the environment sandbox.

    :param environment: The session environment whose cwd is searched.
    :param query: A single-line literal or ripgrep regular expression.
    :param case_sensitive: Whether letter case must match.
    :param regex: Interpret the query as a regular expression.
    :param include: Comma-separated ripgrep globs; explicit includes override ignore rules.
    :param exclude: Comma-separated ripgrep globs to exclude, taking precedence over includes.
    :param limit: Maximum matching lines returned, from 1 to 500.
    :returns: Matches and partial-result metadata, or a user-facing error.
    """
    try:
        script = content_search_script(
            query,
            case_sensitive=case_sensitive,
            regex=regex,
            include=include,
            exclude=exclude,
            limit=limit,
        )
    except ContentSearchError as exc:
        return {"error": exc.message}
    result = await environment.shell(
        _python_script_command(script),
        timeout=SCRIPT_TIMEOUT_S,
        max_output=MAX_SCRIPT_OUTPUT,
    )
    if result.get("error") or result.get("exit_code"):
        return {"error": COULD_NOT_RUN}
    # Records end in "\n" only; splitlines() would also split a match at U+2028.
    return collect_records(line for line in result.get("stdout", "").split("\n") if line)
