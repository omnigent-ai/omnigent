"""Rate-limiting policy for the local ``search_web`` tool: 3 free, then ASK.

This is a stateful FunctionPolicy callable.  It uses module-level state
(a dict keyed by session id would be needed for multi-session, but this
simple version uses a plain counter suitable for a single session).

The callable follows the Service Policies V0 contract:
``fn(event) -> {"result": ..., "reason": ...}``.

Gates on the agent-facing tool name ``search_web`` rather than
``web_search`` because the latter is a reserved Omnigent built-in (see
``omnigent/tools/builtins/__init__.py:BUILTIN_NAMES``); the example
YAML names its local search tool ``search_web`` to avoid the
collision.
"""

from omnigent.policies.schema import PolicyEvent, PolicyResponse

# Agent-facing tool name the YAML declares (``tools.search_web``).
# Kept as a module constant so a future YAML rename only touches
# one site and doesn't silently disable the rate-limit policy.
_RATE_LIMITED_TOOL = "search_web"

_search_count = 0
_FREE_LIMIT = 3


def rate_limit_search(event: PolicyEvent) -> PolicyResponse:
    """Return ASK after the free search budget is exhausted.

    :param event: V0 event dict with ``type``, ``target``,
        ``data``, ``context`` keys.
    :returns: V0 decision dict.
    """
    global _search_count
    if event.get("target") != _RATE_LIMITED_TOOL:
        return {"result": "ALLOW"}

    _search_count += 1
    if _search_count > _FREE_LIMIT:
        return {
            "result": "ASK",
            "reason": (
                f"You have used {_search_count - 1} of your {_FREE_LIMIT} free "
                f"web searches.  Do you approve an additional search?"
            ),
        }
    return {"result": "ALLOW"}


POLICY_REGISTRY = [
    {
        "handler": "tests.resources.examples._shared.search_rate_limit_policy.rate_limit_search",
        "kind": "callable",
    },
]
