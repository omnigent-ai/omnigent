"""Schema-only ``browser_*`` builtin tool classes.

These classes are the **tool surface only** — ``name()``,
``description()`` and ``get_schema()``. They exist so the
embedded-browser tools are *advertised* to the LLM; they deliberately
do NOT implement ``invoke()``.

Execution lives in the runner dispatch layer
(``omnigent/runner/tool_dispatch.py`` — the ``_BROWSER_TOOLS`` branch),
because the browser protocol needs the runner's ``server_client`` to
POST a blocking action request to the server, and ``ToolContext`` carries
no ``server_client``. Any call that reaches ``Tool.invoke`` here means
the tool was misrouted to the server-side path — the base class raises
``NotImplementedError`` loudly in that case.

Descriptions for the browser tools that drive the Omnigent desktop
app's embedded browser. Request-driven harnesses omit them when
the turn starts without a stream subscriber; native harnesses have a
session-scoped relay surface, but unclaimed calls still fail promptly.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool

# The browser tools. Kept as a module constant so the registration
# factory in ``builtins/__init__.py`` and any test can reference the
# canonical name set without re-listing it.
BROWSER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "browser_navigate",
        "browser_go_back",
        "browser_go_forward",
        "browser_reload",
        "browser_tabs",
        "browser_snapshot",
        "browser_click",
        "browser_hover",
        "browser_drag",
        "browser_scroll",
        "browser_type",
        "browser_press_key",
        "browser_select_option",
        "browser_check",
        "browser_screenshot",
    }
)


def _target_properties(noun: str = "element", prefix: str = "") -> dict[str, Any]:
    """
    Schema properties naming one element by snapshot ref or CSS selector.

    :param noun: What the target is, e.g. ``"element"`` or ``"input element"``.
    :param prefix: Property-name prefix for tools with two targets, e.g. ``"start_"``.
    :returns: The ``ref`` / ``snapshot_id`` / ``selector`` properties.
    """
    return {
        f"{prefix}ref": {
            "type": "integer",
            "description": (
                f"Non-negative integer id of the {noun} from a recent "
                "browser_snapshot. Preferred over selector."
            ),
        },
        f"{prefix}snapshot_id" if prefix else "snapshot_id": {
            "type": "string",
            "description": (
                "The snapshot_id the ref came from, so a stale ref is "
                "rejected with a precise error."
            ),
        },
        f"{prefix}selector": {
            "type": "string",
            "description": (
                "CSS selector fallback when you already know a stable selector. "
                "Provide either ref or selector, not both."
            ),
        },
    }


def _function_schema(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """
    Build an OpenAI-format function schema with no extra properties.

    :param name: The tool name, e.g. ``"browser_hover"``.
    :param description: The tool description.
    :param properties: JSON-schema properties of the arguments object.
    :param required: Required property names, if any.
    :returns: Dict with ``"type": "function"`` and a ``"function"`` sub-dict.
    """
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


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
            "must be provided. A default left click runs the element's "
            'click handler directly; `button` "right" or "middle" and '
            "`double_click` send real mouse input at the element's center, "
            "so the element must not be covered."
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
                        "button": {
                            "type": "string",
                            "enum": ["left", "right", "middle"],
                            "description": (
                                "Mouse button. Defaults to left; use right to open a context menu."
                            ),
                        },
                        "double_click": {
                            "type": "boolean",
                            "description": "Double-click instead of a single click.",
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


class _HistoryTool(Tool):
    """Shared shape of the argument-less history tools (schema only)."""

    _DESCRIPTION = ""

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return cls._DESCRIPTION

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(self.name(), self.description(), {})


class BrowserGoBackTool(_HistoryTool):
    """Go back one page in the current tab's history (schema only)."""

    _DESCRIPTION = (
        "Go back one page in the embedded browser's current tab, like the "
        "browser's back button. Waits for the page to load and returns its "
        "url and title. Fails when there is no previous page."
    )

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_go_back"``."""
        return "browser_go_back"


class BrowserGoForwardTool(_HistoryTool):
    """Go forward one page in the current tab's history (schema only)."""

    _DESCRIPTION = (
        "Go forward one page in the embedded browser's current tab, like "
        "the browser's forward button. Waits for the page to load and "
        "returns its url and title. Fails when there is no next page."
    )

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_go_forward"``."""
        return "browser_go_forward"


class BrowserReloadTool(_HistoryTool):
    """Reload the current tab (schema only)."""

    _DESCRIPTION = (
        "Reload the page in the embedded browser's current tab. Waits for "
        "the page to load and returns its url and title. Refs from earlier "
        "snapshots are invalid afterwards."
    )

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_reload"``."""
        return "browser_reload"


class BrowserTabsTool(Tool):
    """List, open, select, or close embedded-browser tabs (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_tabs"``."""
        return "browser_tabs"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Manage this session's embedded-browser tabs, including tabs the "
            "user opened. The other browser_* tools act on your current tab. "
            "`list` returns every tab's tab_id, url, title and which one is "
            "current. `new` opens a tab (optionally at `url`) and makes it "
            "current. `select` makes `tab_id` current and shows it to the "
            "user. `close` closes `tab_id`, or the current tab when omitted."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "action": {
                    "type": "string",
                    "enum": ["list", "new", "select", "close"],
                    "description": "The tab operation.",
                },
                "tab_id": {
                    "type": "string",
                    "description": "Tab from `list`, for `select` and `close`.",
                },
                "url": {
                    "type": "string",
                    "description": "URL to open in the new tab, for `new`.",
                },
            },
            ["action"],
        )


class BrowserHoverTool(Tool):
    """Move the mouse over an element (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_hover"``."""
        return "browser_hover"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Move the mouse over an element in the embedded browser to "
            "trigger hover menus, tooltips and hover styles. Identify the "
            "element with `ref` (from browser_snapshot, with `snapshot_id`) "
            "or `selector`. Call browser_snapshot afterwards to see what "
            "appeared."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(self.name(), self.description(), _target_properties())


class BrowserDragTool(Tool):
    """Drag one element onto another (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_drag"``."""
        return "browser_drag"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Drag an element and drop it on another element in the embedded "
            "browser, using real mouse input (works for HTML drag-and-drop "
            "and pointer-based sortable lists, sliders and canvases). Name "
            "the source with `start_ref` or `start_selector` and the drop "
            "target with `end_ref` or `end_selector`; pass `snapshot_id` "
            "when using refs."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        properties = {
            **_target_properties("element to drag", "start_"),
            **_target_properties("element to drop on", "end_"),
        }
        properties.pop("start_snapshot_id")
        properties.pop("end_snapshot_id")
        properties["snapshot_id"] = _target_properties()["snapshot_id"]
        return _function_schema(self.name(), self.description(), properties)


class BrowserScrollTool(Tool):
    """Scroll the page or a scrollable element (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_scroll"``."""
        return "browser_scroll"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Scroll the embedded browser's page, or the scrollable container "
            "around an element when `ref` or `selector` is given. Returns the "
            "new scroll position and whether the end was reached. Use it to "
            "load more of a long or virtualized list before browser_snapshot."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "left", "right"],
                    "description": "Which way to scroll.",
                },
                "amount": {
                    "type": "integer",
                    "description": (
                        "Pixels to scroll. Defaults to most of the visible height or width."
                    ),
                },
                **_target_properties("element inside the container to scroll"),
            },
            ["direction"],
        )


class BrowserPressKeyTool(Tool):
    """Press a key or key combination (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_press_key"``."""
        return "browser_press_key"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Press a key or key combination in the embedded browser with "
            "real keyboard input, for example `Enter` to submit, `Escape` to "
            "close a dialog, `Tab`, `ArrowDown`, `Backspace`, `Shift+Tab` or "
            "`Control+a`. Focuses the element first when `ref` or `selector` "
            "is given; otherwise the key goes to the focused element. Use "
            "browser_type to enter text."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                "key": {
                    "type": "string",
                    "description": (
                        "Key name or a single character, with optional "
                        "`Shift+`, `Control+`, `Alt+` or `Meta+` modifiers."
                    ),
                },
                **_target_properties("element to focus first"),
            },
            ["key"],
        )


class BrowserSelectOptionTool(Tool):
    """Pick options in a native ``<select>`` (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_select_option"``."""
        return "browser_select_option"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Choose options in a native <select> dropdown in the embedded "
            "browser. Each entry in `values` matches an option's value or "
            "its visible label; pass several for a multi-select. Fires "
            "`input` and `change` like a user would. For custom (non-native) "
            "dropdowns, click to open them and click an option instead."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                **_target_properties("select element"),
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Option values or labels to select.",
                },
            },
            ["values"],
        )


class BrowserCheckTool(Tool):
    """Check or uncheck a checkbox, radio, or switch (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"browser_check"``."""
        return "browser_check"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Set a checkbox, radio button or switch in the embedded browser "
            "to checked (default) or unchecked. Clicks it only when its state "
            "differs, then returns the resulting state. Works on native "
            "inputs, their labels, and ARIA checkbox / radio / switch "
            "elements. A radio button can't be unchecked; check another one."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return _function_schema(
            self.name(),
            self.description(),
            {
                **_target_properties("checkbox, radio or switch"),
                "checked": {
                    "type": "boolean",
                    "description": "Desired state. Defaults to true.",
                },
            },
        )


# Ordered tuple of the schema-only browser tool classes, in the same
# order as ``BROWSER_TOOL_NAMES`` reads. The registration factory in
# ``builtins/__init__.py`` iterates this to construct instances.
BROWSER_TOOL_CLASSES: tuple[type[Tool], ...] = (
    BrowserNavigateTool,
    BrowserGoBackTool,
    BrowserGoForwardTool,
    BrowserReloadTool,
    BrowserTabsTool,
    BrowserSnapshotTool,
    BrowserClickTool,
    BrowserHoverTool,
    BrowserDragTool,
    BrowserScrollTool,
    BrowserTypeTool,
    BrowserPressKeyTool,
    BrowserSelectOptionTool,
    BrowserCheckTool,
    BrowserScreenshotTool,
)
