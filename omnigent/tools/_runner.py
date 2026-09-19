"""Subprocess entry point for local Python tool execution.

Invoked by ``LocalPythonTool.invoke()`` as a child process.
Reads a JSON request from stdin, dynamically imports the tool
module, discovers or invokes ``@tool``-decorated functions, and
writes a JSON response to file descriptor 3.

The fd 3 protocol keeps stdout/stderr free for tool debugging
(``print()`` statements in tool code). In Docker mode (where fd 3
is not available), the ``_AP_RESPONSE_MODE=stdout`` env var
switches to a stdout-based protocol with a ``__AP_RESPONSE__:``
prefix.

Request format (stdin)::

    {
        "module_path": "/abs/path/to/tool.py",
        "tool_name": "word_count",
        "arguments": {"text": "..."}
    }

Discovery requests omit ``tool_name`` and ``arguments`` and set
``"mode": "discover"``.

Response format (fd 3 or stdout)::

    {"result": "tool output as JSON-serialized string"}
    {"error": "TypeError: missing required argument"}
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import traceback
from types import ModuleType
from typing import Any

_RESPONSE_FD = 3
_STDOUT_PREFIX = "__AP_RESPONSE__:"

# The marker attribute the framework's @tool decorator attaches.
# We import the constant lazily inside main() to avoid importing
# the full omnigent.tools package in the subprocess (it pulls
# in heavy deps); the constant is a string literal anyway.
_TOOL_MARKER_ATTR = "_omnigent_tool_metadata"

# Reserved parameter name — kept in sync with
# ``omnigent_client.tools._schema.STATE_PARAM_NAME``. We hardcode
# it here instead of importing to keep the subprocess runner's
# import surface minimal on the hot path.
_STATE_PARAM_NAME = "tool_state"


def main() -> None:
    """
    Entry point for the tool runner subprocess.

    Reads a JSON request from stdin, imports the tool module,
    dispatches to the named ``@tool`` function, serializes the
    return value, and writes the result to fd 3.
    """
    raw = sys.stdin.buffer.read()
    try:
        request = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        _write_error(f"Invalid request JSON: {exc}")
        return

    module_path: str = request.get("module_path", "")
    tool_name: str = request.get("tool_name", "")
    arguments: dict[str, Any] = request.get("arguments", {})
    # Per-agent tool-state directory (see designs/TOOL_STATE.md).
    # ``None`` when no workspace is available (e.g. ad-hoc tests);
    # _maybe_inject_tool_state handles that by raising if the tool
    # actually asked for tool_state in that case.
    state_root: str | None = request.get("state_root")

    if request.get("mode") == "discover":
        module = _load_module(module_path)
        if module is None:
            return
        _write_response({"tools": _discover_tools(module)})
        return

    if not tool_name:
        _write_error("Request missing 'tool_name' field — runner cannot dispatch.")
        return

    module = _load_module(module_path)
    if module is None:
        return

    target = _resolve_tool_function(module, tool_name)
    if target is None:
        return

    try:
        _maybe_inject_tool_state(target, arguments, state_root)
        result = _invoke_tool(target, arguments)
    except Exception as exc:
        traceback.print_exc()
        _write_error(f"{type(exc).__name__}: {exc}")
        return

    serialized = _serialize_result(target, result)
    _write_response({"result": serialized})


def _load_module(path: str) -> ModuleType | None:
    """
    Import a Python file as a standalone module.

    :param path: Absolute path to the tool Python file.
    :returns: The loaded module, or ``None`` on failure (error
        already written to fd 3).
    """
    if not path:
        _write_error("Empty module_path in request")
        return None
    spec = importlib.util.spec_from_file_location("_tool_module", path)
    if spec is None or spec.loader is None:
        _write_error(f"Cannot create module spec from {path}")
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        traceback.print_exc()
        _write_error(f"Import error: {type(exc).__name__}: {exc}")
        return None
    return module


def _discover_tools(module: ModuleType) -> list[dict[str, Any]]:
    """Return metadata for ``@tool`` functions defined by *module*."""
    tools: list[dict[str, Any]] = []
    for value in module.__dict__.values():
        if not callable(value):
            continue
        if getattr(value, "__module__", None) != module.__name__:
            continue
        if not hasattr(value, _TOOL_MARKER_ATTR):
            continue
        metadata = getattr(value, _TOOL_MARKER_ATTR)
        tools.append(
            {
                "name": metadata.name,
                "description": metadata.description,
                "json_schema": metadata.json_schema,
                "strict": metadata.strict,
                "uses_tool_state": metadata.uses_tool_state,
            }
        )
    return tools


def _resolve_tool_function(module: ModuleType, tool_name: str) -> Any:
    """
    Find the ``@tool``-decorated function named ``tool_name`` in ``module``.

    Looks up by attribute name first; if found, verifies the
    object carries the ``@tool`` marker attribute (defense in
    depth — prevents accidentally invoking a non-tool function
    with the same name).

    :param module: The loaded tool module.
    :param tool_name: The function name to dispatch to.
    :returns: The callable if found and properly decorated, else
        ``None`` (error already written to fd 3).
    """
    target = getattr(module, tool_name, None)
    if target is None:
        _write_error(f"Tool function '{tool_name}' not found in module.")
        return None
    if not callable(target):
        _write_error(f"Object '{tool_name}' in module is not callable.")
        return None
    if not hasattr(target, _TOOL_MARKER_ATTR):
        _write_error(f"Function '{tool_name}' is not decorated with @tool.")
        return None
    return target


def _maybe_inject_tool_state(
    target: Any,
    arguments: dict[str, Any],
    state_root: str | None,
) -> None:
    """Inject a ``ToolState`` kwarg into ``arguments`` if the tool asks for it.

    Inspects the tool function's signature; if it declares a
    parameter named :data:`_STATE_PARAM_NAME`, constructs a live
    ``ToolState`` rooted at ``state_root`` and adds it to
    ``arguments`` in place.

    :param target: The ``@tool`` callable to be invoked.
    :param arguments: Kwargs dict splatted into the call. Mutated
        in place when state is injected.
    :param state_root: Parent-provided directory for this agent's
        state, or ``None`` when no workspace is available.
    :raises RuntimeError: If the tool declares ``tool_state`` but
        the parent didn't provide a ``state_root``.
    """
    import inspect as _inspect

    try:
        sig = _inspect.signature(target)
    except (TypeError, ValueError):
        # Non-introspectable (builtin, C-extension). Not an @tool,
        # but defense-in-depth.
        return
    if _STATE_PARAM_NAME not in sig.parameters:
        return
    arguments[_STATE_PARAM_NAME] = _construct_tool_state(state_root)


def _construct_tool_state(state_root: str | None) -> Any:
    """Build a :class:`ToolState` for the given root.

    Split out of :func:`_maybe_inject_tool_state` so the injector
    stays under the 40-line limit and so the lazy imports happen
    on the one path that needs them.

    :param state_root: Directory provided by the parent.
    :returns: A new ``ToolState`` instance.
    :raises RuntimeError: If ``state_root`` is ``None``. Silently
        falling back to a temp dir would crash the tool deep
        inside its body with a less helpful error.
    """
    if state_root is None:
        raise RuntimeError(
            f"Tool declares a '{_STATE_PARAM_NAME}' parameter but no "
            f"state_root was provided by the parent. This usually means "
            f"the invocation has no workspace (e.g. an ad-hoc test). "
            f"ToolState is only available inside a conversation."
        )
    # Lazy imports so stateless tools don't pay the cost. ToolState's
    # own deps are stdlib-only (fcntl, json, pathlib).
    from pathlib import Path as _Path

    from omnigent_client.tools import ToolState as _ToolState

    return _ToolState(_Path(state_root))


def _invoke_tool(target: Any, arguments: dict[str, Any]) -> Any:
    """
    Call the tool function with deserialized arguments.

    For ``async def`` bodies, schedules the coroutine on a fresh
    event loop. For plain ``def`` bodies, calls directly (we're
    already in a subprocess; blocking is fine here — the parent
    framework wraps the subprocess invocation in
    ``asyncio.to_thread`` for event-loop friendliness).

    :param target: The ``@tool``-decorated callable.
    :param arguments: Deserialized argument dict (already validated
        on the parent side via Pydantic during decoration).
    :returns: The function's return value.
    """
    result = target(**arguments)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


def _serialize_result(target: Any, result: Any) -> str:
    """
    Serialize the tool's return value to a JSON string.

    Tries ``pydantic.TypeAdapter`` keyed on the function's
    declared return annotation (handles BaseModel, dataclasses,
    primitives, datetime, UUID natively). Falls back to
    ``json.dumps(value, default=str)`` for un-annotated returns or
    when TypeAdapter rejects the value (e.g. open file handles).

    :param target: The decorated function — its
        ``ToolMetadata.return_annotation`` drives serialization.
    :param result: The function's return value.
    :returns: A JSON string suitable for the LLM-facing tool result.
    """
    metadata = getattr(target, _TOOL_MARKER_ATTR, None)
    return_annotation = (
        getattr(metadata, "return_annotation", None) if metadata is not None else None
    )

    # If the return is already a string, pass it through unchanged
    # (avoids wrapping JSON-string returns in extra quoting). This
    # mirrors how authors of "stringly-typed" tools expect their
    # output to appear in the LLM context.
    if isinstance(result, str):
        return result

    if return_annotation is not None:
        try:
            from pydantic import TypeAdapter  # local import — heavy module

            adapter = TypeAdapter(return_annotation)
            return adapter.dump_json(result).decode("utf-8")
        except Exception:
            # Fall through to json.dumps fallback.
            pass

    try:
        return json.dumps(result, default=str)
    except (TypeError, ValueError) as exc:
        return f"<unserializable return value: {type(result).__name__}: {exc}>"


def _write_response(data: dict[str, Any]) -> None:
    """
    Write a JSON response to the output channel.

    :param data: The response dict (must contain ``"result"``
        or ``"error"``).
    """
    encoded = json.dumps(data).encode()
    fd = _get_output_fd()
    if fd == sys.stdout.fileno():
        # Docker mode: prefix so parent can find the response
        # in stdout mixed with tool debug output.
        sys.stdout.buffer.write(
            f"{_STDOUT_PREFIX}".encode() + encoded + b"\n",
        )
        sys.stdout.buffer.flush()
    else:
        os.write(fd, encoded)
        os.close(fd)


def _write_error(message: str) -> None:
    """
    Write an error response to the output channel.

    :param message: Human-readable error description.
    """
    _write_response({"error": message})


def _get_output_fd() -> int:
    """
    Return the file descriptor for writing the response.

    Reads from ``_AP_RESPONSE_FD`` env var (set by the parent
    to the actual fd number passed via ``pass_fds``). Falls back
    to fd 3 if not set. When ``_AP_RESPONSE_MODE=stdout``, returns
    stdout's fd instead (Docker mode).

    :returns: The file descriptor number.
    """
    if os.environ.get("_AP_RESPONSE_MODE") == "stdout":
        return sys.stdout.fileno()
    return int(os.environ.get("_AP_RESPONSE_FD", str(_RESPONSE_FD)))


if __name__ == "__main__":
    main()
