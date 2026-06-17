"""
In-process callable tools for the omnigent-style spec adapter.

The omnigent YAML translator stores user-declared function
tools as :class:`omnigent.spec.types.LocalToolInfo` entries
with ``language == "omnigent-python-callable"`` and ``path``
holding a dotted-import string (e.g.
``"examples._shared.tool_functions.calculate"``). These are
distinct from native Omnigent local tools, which live as
``tools/python/*.py`` files in the agent bundle and execute via
:class:`omnigent.tools.local.LocalPythonTool` in a subprocess.

The harness contract migration (``designs/SERVER_HARNESS_CONTRACT.md``,
step 5g) routes spec-declared tools through AP's ToolManager so the
executor can dispatch them. This module supplies the AP-side wrapper:

- :class:`LocalCallableTool` — one :class:`Tool` per registered
  callable. Imports the dotted path lazily on first invoke;
  derives the OpenAI function schema from the explicit
  ``parameters`` block in :class:`LocalToolInfo` (preferred) or
  from runtime introspection of the resolved callable
  (fallback). Invocation runs in-process — these tools come
  from the agent author's own code and are trusted to the same
  level as the agent runtime.
- :func:`load_local_callable_tools` — helper that
  :class:`omnigent.tools.manager.ToolManager` calls alongside
  :func:`omnigent.tools.local.load_local_python_tools` to
  pick up the omnigent-translated subset.

Why a separate module: ``local.py`` is dedicated to subprocess-
isolated file-based tools (PEP 723 deps, srt sandbox, docker,
etc.). Mixing in-process callable dispatch there would muddle
its responsibilities. Keeping it separate makes deletion clean
when the omnigent compat path retires (the legacy executor
already reads from ``agent_def`` directly; this module exists
only to bridge the new harness path).
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
from typing import Any

from omnigent.spec.types import LocalToolInfo
from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)

# Match the language constant used by the omnigent YAML
# translator (omnigent/spec/omnigent.py:OMNIGENT_TOOL_LANGUAGE).
# Duplicated here because importing from spec/omnigent.py would
# pull in the heavy translator module just to read one string.
_OMNIGENT_CALLABLE_LANGUAGE = "omnigent-python-callable"


class LocalCallableTool(Tool):
    """
    AP-side :class:`Tool` wrapping a dotted-path Python callable.

    The dotted path comes from
    :attr:`omnigent.spec.types.LocalToolInfo.path` and is
    resolved via :func:`importlib.import_module` on first
    invocation. The resolved callable runs in-process — these
    tools come from the same source tree as the agent spec
    (the user's own Python module), so subprocess isolation
    wouldn't add a meaningful trust boundary.

    :param info: The :class:`LocalToolInfo` produced by the
        omnigent YAML translator.
    """

    def __init__(self, info: LocalToolInfo) -> None:
        self._info = info
        self._name = info.name
        self._callable: Any = None
        self._description: str = ""
        self._parameters: dict[str, Any] = info.parameters or {
            "type": "object",
            "properties": {},
        }

    def name(self) -> str:  # type: ignore[override]
        """
        :returns: The tool's name as advertised to the LLM, e.g.
            ``"calculate"``.
        """
        return self._name

    def dotted_path(self) -> str:
        """
        Return the dotted import path the YAML registered.

        Used by :func:`omnigent.runtime.workflow._dispatch_local_callable_tool_async`
        to thread the path into a background workflow without
        reaching into ``self._info`` (which would couple the runtime
        to :class:`LocalToolInfo`'s internal shape).

        :returns: Dotted import path, e.g.
            ``"examples._shared.tool_functions.calculate"``.
        """
        return self._info.path

    @classmethod
    def description(cls) -> str:
        """
        :returns: Generic class-level description. Per-instance
            descriptions are derived lazily from the resolved
            callable's docstring and exposed via
            :meth:`get_schema`.
        """
        return "User-declared function tool from an omnigent-style YAML."

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI Chat-Completions function schema.

        Resolves the callable lazily so the description (taken
        from the function's docstring) is available even before
        the first invocation. Parameters come from the explicit
        ``parameters`` block on :class:`LocalToolInfo` when the
        YAML supplied one; otherwise the empty
        ``{"type": "object", "properties": {}}`` placeholder is
        used (matching what the omnigent harness advertises
        when introspection fails).

        :returns: OpenAI Chat-Completions tool schema, e.g.
            ``{"type": "function", "function": {"name":
            "calculate", "description": "...", "parameters":
            {...}}}``.
        """
        self._ensure_resolved()
        return {
            "type": "function",
            "function": {
                "name": self._name,
                "description": self._description,
                "parameters": self._parameters,
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Run the wrapped callable with the LLM's arguments.

        Parses the JSON-encoded ``arguments`` string into kwargs
        (the LLM produces JSON per the OpenAI tool-call shape).
        Runs the callable in-process; converts its return value
        to a string for the workflow to record. Exceptions are
        re-raised to the caller — :meth:`ExecutorContext.call_tool`
        wraps them into a ``status="error"`` :class:`ToolResult`
        upstream.

        :param arguments: JSON-encoded arguments string from
            the LLM, e.g. ``'{"expression": "2+2"}'``.
        :param ctx: Server-side execution context (unused — the
            wrapped callable doesn't see workflow identity).
        :returns: The callable's return value coerced to a
            string. ``None`` returns become the empty string.
        """
        del ctx  # The wrapped callable is plain Python; no ctx plumbing.
        self._ensure_resolved()
        try:
            kwargs = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"local-callable tool {self._name!r} got malformed JSON arguments: {exc}"
            ) from exc
        if not isinstance(kwargs, dict):
            raise ValueError(
                f"local-callable tool {self._name!r} expects a JSON object for "
                f"arguments; got {type(kwargs).__name__}"
            )
        result = self._callable(**kwargs)
        return _stringify(result)

    def _ensure_resolved(self) -> None:
        """
        Import the callable on first use; cache for subsequent calls.

        Done lazily (not at construction time) so spec validation
        and tool listing don't trigger import-time side effects of
        the user's module. Failures here surface a clear error
        naming the missing path; AP's tool dispatch then converts
        it to an error :class:`ToolResult` via the standard
        wrapper.

        :raises ImportError: If the dotted path's module is not
            importable.
        :raises AttributeError: If the module exists but does not
            expose the named attribute.
        """
        if self._callable is not None:
            return
        path = self._info.path
        module_name, _, attr_name = path.rpartition(".")
        if not module_name or not attr_name:
            raise ImportError(
                f"local-callable tool {self._name!r} has invalid path {path!r}; "
                f"expected dotted form like 'pkg.module.func'"
            )
        module = importlib.import_module(module_name)
        if not hasattr(module, attr_name):
            raise AttributeError(
                f"local-callable tool {self._name!r}: module {module_name!r} has "
                f"no attribute {attr_name!r}"
            )
        callable_obj = getattr(module, attr_name)
        if not callable(callable_obj):
            raise TypeError(
                f"local-callable tool {self._name!r}: {path!r} resolved to a "
                f"non-callable {type(callable_obj).__name__}"
            )
        self._callable = callable_obj
        # Description: first paragraph of the docstring, or a
        # fallback derived from the tool name. The LLM only sees
        # one description per tool, so collapsing multi-line
        # docstrings to the lead paragraph keeps token usage in
        # check while preserving the most useful information.
        doc = inspect.getdoc(callable_obj) or ""
        first_paragraph = doc.split("\n\n", 1)[0].strip()
        self._description = first_paragraph or f"User function tool {self._name!r}."


def load_local_callable_tools(
    local_tools: list[LocalToolInfo],
) -> list[LocalCallableTool]:
    """
    Build :class:`LocalCallableTool` instances for omnigent tools.

    Filters *local_tools* to the ones the omnigent YAML
    translator produced — entries whose ``language`` is
    ``"omnigent-python-callable"``. Other entries (native AP
    file-based tools with ``language == "python"``) are
    handled by :func:`omnigent.tools.local.load_local_python_tools`.

    :param local_tools: All :class:`LocalToolInfo` entries from
        the agent spec.
    :returns: One :class:`LocalCallableTool` per omnigent-style
        entry. Empty list when none are present (which is the
        case for native Omnigent specs).
    """
    from omnigent.spec.types import ToolRuntime

    result: list[LocalCallableTool] = []
    for info in local_tools:
        if info.language != _OMNIGENT_CALLABLE_LANGUAGE:
            continue
        # UC function tools have path=None and are dispatched by
        # the runner via the SQL Statement Execution API — they
        # are not in-process callables and must not be wrapped.
        if info.runtime == ToolRuntime.UC_FUNCTION:
            continue
        result.append(LocalCallableTool(info))
    return result


def _stringify(value: Any) -> str:
    """
    Coerce a tool's return value into a string for the workflow.

    Strings pass through; ``None`` becomes the empty string;
    everything else routes through :func:`json.dumps` with a
    fallback to :func:`repr` for objects JSON cannot encode.
    The legacy omnigent path stringifies tool results the same
    way, so behavior round-trips cleanly between paths.

    :param value: The wrapped callable's return value.
    :returns: A string suitable for the
        ``function_call_output.output`` field.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
