"""Schema-only ``browser_*`` builtin tool classes.

These classes are the **tool surface only** — ``name()``,
``description()`` and ``get_schema()``. They exist so the five
embedded-browser tools are *advertised* to the LLM; they deliberately
do NOT implement ``invoke()``.

Execution lives in the runner dispatch layer
(``omnigent/runner/tool_dispatch.py`` — the ``_BROWSER_TOOLS`` branch),
because the browser protocol needs the runner's ``server_client`` to
POST a blocking action request to the server, and ``ToolContext`` carries
no ``server_client``. Any call that reaches ``Tool.invoke`` here means
the tool was misrouted to the server-side path — the base class raises
``NotImplementedError`` loudly in that case.

Descriptions for the five browser tools that drive the Omnigent
desktop app's embedded browser; they fail cleanly when no desktop
renderer is subscribed (the action times out with a clear error).
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# The five browser tools. Kept as a module constant so the registration
# factory in ``builtins/__init__.py`` and any test can reference the
# canonical name set without re-listing it.
BROWSER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    }
)


class BrowserNavigateTool(Tool):
    """Open or navigate the embedded browser pane to a URL (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_navigate"``."""
        return "browser_navigate"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Open or navigate the Omnigent desktop app's embedded "
            "browser pane to a URL. Auto-opens the pane if it isn't "
            "open yet. Requires the Omnigent desktop window to be "
            "running — fails cleanly otherwise. After a load settles, "
            "call browser_snapshot to inspect what's on the page."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": BrowserNavigateTool.name(),
                "description": BrowserNavigateTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to open or navigate to.",
                        },
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        }


class BrowserSnapshotTool(Tool):
    """Capture an accessibility-tree snapshot of the page (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_snapshot"``."""
        return "browser_snapshot"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Capture an accessibility-tree snapshot of the embedded "
            "browser. Returns a snapshot_id plus the current URL, "
            "document.title, and a YAML-shaped tree of every "
            "interactive / text-bearing / landmark element on the "
            "page, each tagged with [ref=N]. Hand the refs (along with "
            "the snapshot_id) to browser_click / browser_type so the "
            "renderer can detect when the snapshot has been superseded "
            "by a newer one or invalidated by navigation. Refs are "
            "dramatically more stable than CSS selectors against "
            "generated class names and Shadow DOM."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": BrowserSnapshotTool.name(),
                "description": BrowserSnapshotTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }


class BrowserClickTool(Tool):
    """Click an element by ref or CSS selector (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_click"``."""
        return "browser_click"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Click an element in the embedded browser. Prefer the "
            "`ref` form (integer id from a recent browser_snapshot "
            "result) — refs are stable against generated class names "
            "and Shadow DOM. Pass `snapshot_id` alongside `ref` so the "
            "renderer can reject stale-snapshot refs with a precise "
            "error instead of a generic stale-ref message. CSS "
            "`selector` is accepted as a fallback when you already know "
            "a stable selector. Exactly one of `ref` or `selector` "
            "must be provided."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": BrowserClickTool.name(),
                "description": BrowserClickTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "integer",
                            "description": (
                                "Non-negative integer id of the element "
                                "from a recent browser_snapshot. Preferred "
                                "over selector."
                            ),
                        },
                        "snapshot_id": {
                            "type": "string",
                            "description": (
                                "The snapshot_id the ref came from, so a "
                                "stale ref is rejected with a precise error."
                            ),
                        },
                        "selector": {
                            "type": "string",
                            "description": (
                                "CSS selector fallback when you already "
                                "know a stable selector. Provide either "
                                "ref or selector, not both."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }


class BrowserTypeTool(Tool):
    """Type text into an input by ref or CSS selector (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_type"``."""
        return "browser_type"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Focus an input element and type text into it. Identify the "
            "input with `ref` (preferred — integer id from "
            "browser_snapshot, pair with `snapshot_id` for precise "
            "stale-ref errors) or `selector` (CSS, fallback). "
            "Dispatches `input` + `change` events using the native "
            "value setter so React/Vue/etc. controlled inputs see the "
            "value."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": BrowserTypeTool.name(),
                "description": BrowserTypeTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "integer",
                            "description": (
                                "Non-negative integer id of the input "
                                "element from a recent browser_snapshot. "
                                "Preferred over selector."
                            ),
                        },
                        "snapshot_id": {
                            "type": "string",
                            "description": (
                                "The snapshot_id the ref came from, so a "
                                "stale ref is rejected with a precise error."
                            ),
                        },
                        "selector": {
                            "type": "string",
                            "description": (
                                "CSS selector fallback when you already "
                                "know a stable selector. Provide either "
                                "ref or selector, not both."
                            ),
                        },
                        "text": {
                            "type": "string",
                            "description": "The text to type into the input.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        }


class BrowserScreenshotTool(Tool):
    """Capture a PNG screenshot of the browser pane (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_screenshot"``."""
        return "browser_screenshot"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Capture a PNG screenshot of the embedded browser pane. "
            "Returns image content the agent surface renders inline. "
            "INTENDED FOR VISUAL INSPECTION ONLY — prefer "
            "browser_snapshot for picking elements to act on, since "
            "screenshots can't carry ref ids and you can't click a "
            "pixel location. Use this when you need to verify what "
            "something looks like, not to plan an interaction."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": BrowserScreenshotTool.name(),
                "description": BrowserScreenshotTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }


# Ordered tuple of the schema-only browser tool classes, in the same
# order as ``BROWSER_TOOL_NAMES`` reads. The registration factory in
# ``builtins/__init__.py`` iterates this to construct instances.
BROWSER_TOOL_CLASSES: tuple[type[Tool], ...] = (
    BrowserNavigateTool,
    BrowserSnapshotTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserScreenshotTool,
)
