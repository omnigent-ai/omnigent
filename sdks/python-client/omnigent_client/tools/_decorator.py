"""
The ``@tool`` decorator and its metadata.

A ``@tool``-decorated module-level function is the authoring
contract for custom Python tools in omnigent. The decorator:

1. Validates that the target is a module-level ``def`` or
   ``async def`` (rejects class methods, lambdas, and nested
   functions; see :func:`_validate_decorator_target`).
2. Derives the function-calling JSON schema from the signature
   and Google-style docstring (see :mod:`._schema`).
3. Attaches metadata to the function via the
   :data:`TOOL_MARKER_ATTR` attribute so the framework can
   discover decorated functions by scanning a module's namespace.

The decorator is intentionally pure metadata: it returns the
original function unwrapped. The framework's executor handles
sync vs async execution (wrapping plain ``def`` bodies in
``asyncio.to_thread`` so they don't block the event loop).

**Decorator stacking**: ``@tool`` should be the outermost
decorator. Inner decorators (``@retry``, ``@cache``, etc.) must
use ``functools.wraps`` so the signature and docstring survive
to the schema-derivation pass; otherwise the schema will reflect
the wrapper, not the underlying function.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar, overload

from ._schema import build_function_schema

# Marker attribute name. The framework's loader scans
# ``module.__dict__`` for objects carrying this attribute to
# enumerate the tools a Python file exports.
TOOL_MARKER_ATTR = "_omnigent_tool_metadata"


@dataclass(frozen=True)
class ToolMetadata:
    """
    Metadata attached to a ``@tool``-decorated function.

    Read by the framework loader and by the subprocess runner.
    Read-only by convention — re-decoration or hand-editing is
    not supported.

    :param name: The tool name as the LLM sees it. Derived from
        the function's ``__name__``, e.g. ``"word_count"``.
    :param description: Human-readable description, derived from
        the function's docstring's leading paragraph.
    :param json_schema: The function-calling JSON schema for the
        tool's parameters, in the OpenAI function-calling shape
        (an ``object`` schema with ``properties`` / ``required``).
        Already strict-mode-normalized if ``strict=True`` was
        passed to the decorator.
    :param strict: Whether the schema was normalized to strict
        mode. Stored so the executor side can stay consistent with
        the LLM's expectations.
    :param return_annotation: The function's declared return type,
        used by the executor to deserialize the return value via
        ``pydantic.TypeAdapter``. ``None`` if the function has no
        return annotation (executor falls back to
        ``json.dumps(value, default=str)``).
    :param uses_tool_state: Whether the function declared a
        ``tool_state`` parameter. Recorded at decoration time so
        the framework can gate per-conversation state-directory
        creation without re-inspecting the function signature.
    """

    name: str
    description: str
    json_schema: dict[str, Any]
    strict: bool
    return_annotation: type[Any] | None
    uses_tool_state: bool = False


P = ParamSpec("P")
R = TypeVar("R")


@overload
def tool(fn: Callable[P, R]) -> Callable[P, R]: ...


@overload
def tool(
    *,
    strict: bool = ...,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def tool(
    fn: Callable[P, R] | None = None,
    *,
    strict: bool = True,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Mark a module-level function as an omnigent tool.

    The decorator infers the LLM-facing schema from the function's
    type hints and Google-style docstring, then attaches the
    derived metadata via the :data:`TOOL_MARKER_ATTR` attribute.
    The framework's loader scans modules for this marker to
    register tools at agent-image load time.

    Every ``@tool``-decorated function is sync from the framework's
    perspective. Authors who want a tool dispatched as background
    work do not annotate the tool — the LLM picks per call site
    via ``sys_call_async(tool=..., args=...)`` (see
    ``omnigent/tools/builtins/async_inbox.py``). The author-time
    ``@tool(synchronous=False)`` decoration was removed once
    ``sys_call_async`` shipped — keeping both surfaces would double
    the dispatch paths without adding capability.

    Usage::

        @tool
        async def word_count(text: str) -> dict[str, int]:
            \"\"\"Count words, characters, and lines.\"\"\"
            ...

    Restrictions:
    - Target must be a module-level ``def`` or ``async def``.
      Class methods, lambdas, and nested functions are rejected
      with ``TypeError``.
    - ``@tool`` should be the outermost decorator; inner
      decorators must use ``functools.wraps`` for schema derivation
      to see the underlying signature and docstring.

    :param fn: When used as bare ``@tool`` (no parens), the
        decorated function. ``None`` when used as
        ``@tool(strict=False)``.
    :param strict: If ``True`` (default), the derived schema is
        normalized to strict mode (``additionalProperties: false``
        on objects, all properties required). Authors who hit a
        schema strict mode breaks can opt out with
        ``@tool(strict=False)``.
    :returns: The original function with the tool metadata
        attached (when called with ``fn``), or a decorator
        function (when called with keyword args).
    """

    def wrap(target: Callable[P, R]) -> Callable[P, R]:
        _validate_decorator_target(target)
        schema_result = build_function_schema(target, strict=strict)
        metadata = ToolMetadata(
            name=target.__name__,
            description=schema_result.description,
            json_schema=schema_result.parameters_json_schema,
            strict=strict,
            return_annotation=schema_result.return_annotation,
            uses_tool_state=schema_result.uses_tool_state,
        )
        # Attach metadata via setattr (the dynamic attribute name
        # is intentional — it's the framework's discovery contract,
        # see TOOL_MARKER_ATTR).
        setattr(target, TOOL_MARKER_ATTR, metadata)
        return target

    if fn is None:
        return wrap
    return wrap(fn)


def _validate_decorator_target(target: Any) -> None:
    """
    Reject decorator application to anything other than a
    module-level ``def`` or ``async def``.

    Class methods would include ``self`` / ``cls`` in the schema,
    which the LLM has no way to fill. Lambdas have no name and
    no docstring. Nested functions can close over enclosing scope
    that doesn't survive subprocess invocation.

    :param target: The object the decorator was applied to.
    :raises TypeError: If ``target`` is not a module-level
        function. The message names the offending construct so
        agent authors get an actionable error.
    """
    if not callable(target):
        raise TypeError(f"@tool can only be applied to functions, got {type(target).__name__}.")

    if isinstance(target, (staticmethod, classmethod)):
        raise TypeError(
            "@tool cannot be applied to staticmethod or classmethod. "
            "Define the tool as a module-level function instead — "
            "the framework has no way to bind 'self' or 'cls' from "
            "an LLM-supplied argument set."
        )

    if not inspect.isfunction(target):
        # Covers callables, methods of class instances, etc.
        raise TypeError(
            f"@tool requires a plain Python function, got "
            f"{type(target).__name__}. Define the tool as a "
            f"module-level def or async def."
        )

    if target.__name__ == "<lambda>":
        raise TypeError(
            "@tool cannot be applied to a lambda. Lambdas have no "
            "name or docstring; the LLM has nothing to call. Define "
            "the tool with `def` or `async def` instead."
        )

    # Nested-function detection: __qualname__ contains a dot path
    # (e.g. "outer.<locals>.inner") for any function defined inside
    # another function or class body.
    qualname = target.__qualname__
    if qualname != target.__name__:
        # Allow class-level methods if someone re-binds them at module
        # scope (rare); the staticmethod/classmethod check above is
        # the primary defense. Otherwise reject as nested.
        raise TypeError(
            f"@tool cannot be applied to nested functions or methods "
            f"({qualname!r}). Define the tool at module scope so the "
            f"framework's subprocess runner can re-import it cleanly. "
            f"State that needs to persist across invocations belongs "
            f"in module-level globals, not closure variables."
        )


def get_tool_metadata(obj: Any) -> ToolMetadata | None:
    """
    Return the :class:`ToolMetadata` for an object if it is a
    ``@tool``-decorated function, else ``None``.

    Used by the framework loader to filter a module's namespace
    down to the decorated functions it should register.

    :param obj: Any value pulled from a module's namespace.
    :returns: The attached metadata, or ``None`` if the object
        was not produced by ``@tool``.
    """
    metadata = getattr(obj, TOOL_MARKER_ATTR, None)
    if isinstance(metadata, ToolMetadata):
        return metadata
    return None
