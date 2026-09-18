"""Guard the hot request paths against blocking the server's event loop.

The stores are synchronous, so a store call made directly from an
``async def`` handler runs *on* the event loop: for its whole duration —
a database round-trip, a ``pool_pre_ping``, and up to ``pool_timeout``
when the pool is saturated — no other request, SSE stream or WebSocket
heartbeat on that process makes progress. A single-worker deployment
shares one loop across every connected user, so one such call stalls all
of them, and the stall gets worse exactly when the server is busiest.

The fix is always the same shape: ``await asyncio.to_thread(store.method,
…)``. This test pins it for the paths whose call rate scales with usage —
per tool call, per message, per session create, per page load, per file
operation — so a new one can't be added back unnoticed.

Modules that only serve login and admin flows are deliberately *not*
guarded yet: their call rate scales with logins rather than with agent
activity, and converting them is a separate change. See
:data:`_UNGUARDED_KNOWN_DEBT`.
"""

from __future__ import annotations

import ast
import pathlib

# Request-path modules whose store reads must never run on the event loop.
_GUARDED_MODULES = (
    "omnigent/server/app.py",
    "omnigent/server/routes/builtin_agents.py",
    "omnigent/server/routes/_sessions/helpers.py",
    "omnigent/server/routes/_sessions/orchestration.py",
    "omnigent/server/routes/sessions/routes_agent.py",
    "omnigent/server/routes/sessions/routes_core.py",
    "omnigent/server/routes/sessions/routes_events.py",
    "omnigent/server/routes/sessions/routes_hooks.py",
    "omnigent/server/routes/sessions/routes_resources.py",
)

# Known remaining debt, left un-guarded on purpose (login / device-grant /
# account-admin flows). Listed so the gap is explicit rather than forgotten.
_UNGUARDED_KNOWN_DEBT = (
    "omnigent/server/routes/accounts_auth.py",
    "omnigent/server/routes/auth.py",
    "omnigent/server/routes/device_auth.py",
)

# Injected store dependencies: synchronous by contract, except for the few
# conversation-store methods that are themselves ``async def`` (those are
# awaited, and awaited calls are never reported).
_STORE_NAMES = frozenset(
    {
        "account_store",
        "accounts_store",
        "agent_store",
        "artifact_store",
        "comment_store",
        "conversation_store",
        "credential_store",
        "device_grant_store",
        "file_store",
        "host_store",
        "permission_store",
        "policy_store",
        "project_store",
        "scheduled_task_store",
    }
)


class _BlockingStoreCallFinder(ast.NodeVisitor):
    """Collect sync store calls made directly inside an ``async def`` body.

    A nested ``def`` or ``lambda`` is a callable handed to a worker thread,
    so calls inside one do not run on the loop and are not reported.
    """

    def __init__(self) -> None:
        self._scopes: list[bool] = []
        self._awaited: set[int] = set()
        self.findings: list[tuple[int, str]] = []

    def visit_Await(self, node: ast.Await) -> None:
        """Mark the awaited call so an async store method isn't reported."""
        if isinstance(node.value, ast.Call):
            self._awaited.add(id(node.value))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Enter a coroutine body — calls here run on the event loop."""
        self._scopes.append(True)
        self.generic_visit(node)
        self._scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Enter a sync body — reached through a thread, so not on the loop."""
        self._scopes.append(False)
        self.generic_visit(node)
        self._scopes.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Treat a lambda like a sync function body."""
        self._scopes.append(False)
        self.generic_visit(node)
        self._scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        """Report ``<store>.<method>(…)`` invoked straight from a coroutine."""
        func = node.func
        on_loop = bool(self._scopes) and self._scopes[-1]
        if (
            on_loop
            and id(node) not in self._awaited
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _STORE_NAMES
        ):
            self.findings.append((node.lineno, f"{func.value.id}.{func.attr}"))
        self.generic_visit(node)


def _blocking_calls(module: str) -> list[str]:
    """Return ``path:line store.method`` for each on-loop store call in *module*."""
    path = pathlib.Path(__file__).resolve().parents[2] / module
    finder = _BlockingStoreCallFinder()
    finder.visit(ast.parse(path.read_text()))
    return [f"{module}:{line} {call}()" for line, call in finder.findings]


def test_hot_request_paths_do_not_block_the_event_loop() -> None:
    """No guarded request path calls a sync store from a coroutine.

    Wrap the offending call in ``await asyncio.to_thread(...)``; a stall
    here is shared by every user connected to the process.
    """
    offenders = [call for module in _GUARDED_MODULES for call in _blocking_calls(module)]

    assert offenders == [], (
        "these store calls run on the event loop and stall every connected "
        "client for the duration of the read:\n  " + "\n  ".join(offenders)
    )


def test_known_debt_modules_still_exist() -> None:
    """The un-guarded list names real modules, so it can't rot silently."""
    root = pathlib.Path(__file__).resolve().parents[2]
    assert [module for module in _UNGUARDED_KNOWN_DEBT if not (root / module).is_file()] == []
