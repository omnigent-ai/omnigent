"""
Tests for the unified builtin tool registry (POLICIES.md §15.8).

Phase 2 unification: `BUILTIN_NAMES` and the instantiable
subset both derive from a single `_BUILTIN_REGISTRY` dict,
with `None` factories marking framework-owned names
(``web_fetch``).

These tests lock the name-space invariants so any future
registry change has to either deliberately touch them or
break loudly.
"""

from __future__ import annotations

import importlib
import pkgutil

import omnigent.tools.builtins as _builtins_pkg
from omnigent.tools.base import Tool
from omnigent.tools.builtins import (
    BUILTIN_NAMES,
    INSTANTIABLE_BUILTINS,
    get_builtin_tool,
)


def test_builtin_names_excludes_request_approval() -> None:
    """``request_approval`` is no longer reserved.

    The synthetic function_call by that name was deleted when
    policy ASKs moved to MCP-shape elicitations
    (``response.elicitation_request`` SSE event +
    session ``approval`` event). User specs are
    now free to declare a tool called ``request_approval`` —
    no carve-out, no collision.

    A regression where ``request_approval`` reappears in
    BUILTIN_NAMES would silently re-reserve a name we no longer
    own; this test guards that.
    """
    assert "request_approval" not in BUILTIN_NAMES


def test_builtin_names_includes_framework_owned_tools() -> None:
    """web_fetch, list_comments, and update_comment
    are framework-owned (need runtime context, not instantiated
    via the registry). They must still occupy the name-space so
    user specs can't declare tools with these names."""
    assert "web_fetch" in BUILTIN_NAMES
    assert "list_comments" in BUILTIN_NAMES
    assert "update_comment" in BUILTIN_NAMES
    assert "sys_list_models" in BUILTIN_NAMES


def test_instantiable_subset_excludes_framework_owned() -> None:
    """Framework-owned names are NOT in INSTANTIABLE_BUILTINS
    because they have no factory. The onboarding assistant
    uses this set to tell the agent author what they can
    declare — listing framework-owned names there would be
    confusing and wrong."""
    assert "web_fetch" not in INSTANTIABLE_BUILTINS
    assert "list_comments" not in INSTANTIABLE_BUILTINS
    assert "update_comment" not in INSTANTIABLE_BUILTINS


def test_instantiable_is_subset_of_builtin_names() -> None:
    """Every instantiable name is also a reserved name. The
    two sets can't get out of sync because they derive from
    the same dict — this test guards against a refactor that
    introduces drift."""
    # subset check expressed via issubset — clearer than
    # "for-in" iteration.
    assert INSTANTIABLE_BUILTINS.issubset(BUILTIN_NAMES)


def test_get_builtin_tool_returns_none_for_framework_owned() -> None:
    """Calling get_builtin_tool on a framework-owned name
    returns None — the caller must fall back to the special
    constructor path. This is the same behavior as an
    unknown name, which is fine because BUILTIN_NAMES is
    the authoritative "is this reserved?" set."""
    assert get_builtin_tool("web_fetch") is None
    assert get_builtin_tool("list_comments") is None
    assert get_builtin_tool("update_comment") is None


def test_get_builtin_tool_returns_none_for_unknown_name() -> None:
    """Unknown names also return None. Callers that want to
    distinguish "unknown" from "framework-owned" must check
    `name in BUILTIN_NAMES` first."""
    assert get_builtin_tool("definitely_not_a_tool") is None


def test_get_builtin_tool_instantiates_known_tools() -> None:
    """Instantiable tools produce a real Tool instance. Smoke
    test — if this breaks, every agent with `web_search`
    declared starts failing at load time."""
    tool = get_builtin_tool("web_search")
    # Not None + correct name — proves both the factory ran
    # and produced an instance with the expected identity.
    assert tool is not None
    assert tool.name() == "web_search"


def test_builtin_names_size_matches_registry() -> None:
    """A sanity check that the derivation is lossless. If
    someone adds a new registry entry but BUILTIN_NAMES
    doesn't reflect it (impossible under current derivation,
    but a refactor could miss it), this test turns red."""
    # Lock the expected set so adding / removing a name is an
    # explicit test edit.
    assert (
        frozenset(
            {
                # Instantiable
                "web_search",
                "upload_file",
                "list_files",
                "download_file",
                "search_conversations",
                "export_agent",
                "create_work_item",
                "list_tasks",
                "update_work_item",
                "create_loop",
                "create_monitor",
                "list_schedules",
                "delete_schedule",
                # Framework-owned (need runtime context, not
                # user-instantiable). Policy ASKs surface as
                # MCP-shape elicitations on the SSE stream and
                # do NOT reserve a name in this registry.
                # The ``sys_terminal_*`` family also lives outside
                # this registry — registered by ToolManager when
                # the spec declares ``terminals:``.
                "web_fetch",
                # Comment tools: auto-registered by ToolManager so
                # agents can list/update review comments without
                # spec opt-in. Session-scoped at invoke time via
                # ToolContext.conversation_id.
                "list_comments",
                "update_comment",
                # sys_list_models: auto-registered by ToolManager
                # alongside the sub-agent dispatch grant and
                # intercepted in the runner's tool dispatch. Reserved
                # so user specs can't shadow it.
                "sys_list_models",
            }
        )
        == BUILTIN_NAMES
    )


def _all_builtin_tool_subclasses() -> list[type[Tool]]:
    """Concrete ``Tool`` subclasses defined under ``omnigent.tools.builtins``."""
    for mod_info in pkgutil.iter_modules(_builtins_pkg.__path__):
        importlib.import_module(f"{_builtins_pkg.__name__}.{mod_info.name}")

    seen: set[type[Tool]] = set()
    for cls in Tool.__subclasses__():
        stack: list[type[Tool]] = [cls]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(node.__subclasses__())

    return [
        c
        for c in seen
        if c.__module__.startswith(_builtins_pkg.__name__) and not c.__name__.startswith("_")
    ]


def test_async_builtins_override_dispatch_async_or_are_runner_dispatched() -> None:
    """``is_async()==True`` tools either override ``dispatch_async`` OR are runner-dispatched.

    The base ``Tool.dispatch_async`` raises ``NotImplementedError``,
    so any tool that flips ``is_async`` true without an override
    would crash the in-process Omnigent loop. After the DBOS removal,
    a class of async-namespace tools (``sys_call_async``,
    ``sys_read_inbox``, ``sys_cancel_async``) are dispatched by
    the runner via ``omnigent/runner/tool_dispatch.py`` —
    ``dispatch_async`` is never reached on those, so leaving them
    on the base implementation is correct. Pin the contract: an
    async tool is permitted iff it either overrides
    ``dispatch_async`` or is listed in the runner's
    ``_ALL_LOCAL_TOOLS`` set.
    """
    from omnigent.runner.tool_dispatch import should_dispatch_locally

    base_dispatch_async = Tool.dispatch_async

    offenders = []
    for cls in _all_builtin_tool_subclasses():
        try:
            instance = cls()
        except Exception:
            # Tools that require constructor args are exempt — they
            # can't be instantiated bare here; their own tests cover
            # the async contract.
            continue
        if not instance.is_async():
            continue
        if cls.dispatch_async is not base_dispatch_async:
            continue
        if should_dispatch_locally(cls.name()):
            continue
        offenders.append(f"{cls.__module__}.{cls.__name__}")

    assert not offenders, (
        f"is_async()==True without dispatch_async override AND not "
        f"runner-dispatched (would raise NotImplementedError in the "
        f"in-process loop): {offenders}"
    )
