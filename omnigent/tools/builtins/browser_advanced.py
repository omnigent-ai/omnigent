"""Schema-only classes for the advanced embedded-browser tools.

Like the standard ``browser_*`` classes in
``omnigent/tools/builtins/browser.py`` they only advertise the tools
(``name()``, ``description()``, ``get_schema()``); the runner's
``_BROWSER_TOOLS`` dispatch branch forwards calls to the renderer.

``register_browser_agent_tools`` turns the standard five classes into the
full surface: ``browser_click`` and ``browser_type`` swap in subclasses
describing the desktop app's real (trusted) input, and the advanced
tools follow. The session switch (``omnigent/tools/browser_agent_tools.py``)
hides the advanced parts per session.
"""

from __future__ import annotations

import copy
from typing import Any

from omnigent.tools.base import Tool
from omnigent.tools.builtins.browser import BrowserClickTool, BrowserTypeTool


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


class TrustedBrowserClickTool(BrowserClickTool):
    """``browser_click`` as the desktop app performs it: real mouse input (schema only)."""

    @classmethod
    def description(cls) -> str:
        """:returns: The standard description plus how real clicks behave."""
        return (
            BrowserClickTool.description() + " Clicks with real mouse input at the "
            "element's center, so it fails if something covers the element; "
            "close the dialog or menu over it first. Native dropdowns, and "
            "date, time or color inputs, can't be clicked: set them with "
            "browser_type. "
            '`button` "right" or "middle" and `double_click` send those '
            "clicks instead."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The standard schema plus the ``button`` / ``double_click`` options."""
        schema = copy.deepcopy(super().get_schema())
        schema["function"]["description"] = self.description()
        schema["function"]["parameters"]["properties"].update(
            {
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
            }
        )
        return schema


class TrustedBrowserTypeTool(BrowserTypeTool):
    """``browser_type`` as the desktop app performs it: real text input (schema only)."""

    @classmethod
    def description(cls) -> str:
        """:returns: How the desktop app's real text input behaves."""
        return (
            "Focus a text field, textarea or editable area (including "
            "rich-text editors) and replace what it holds with `text`, "
            "entered as real text input. Identify it with `ref` "
            "(preferred — integer id from browser_snapshot, pair with "
            "`snapshot_id` for precise stale-ref errors) or `selector` "
            "(CSS, fallback). Date, time, color and range inputs get the "
            'value set directly, e.g. "2026-03-04" for a date, and a native '
            "dropdown (<select>) gets the option whose value or label is "
            "`text`. Doesn't press Enter; use browser_press_key for that."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The standard schema with this description."""
        schema = copy.deepcopy(super().get_schema())
        schema["function"]["description"] = self.description()
        return schema


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
                    "items": {"type": "string", "maxLength": 4096},
                    "minItems": 1,
                    "maxItems": 100,
                    "description": "Option values or labels to select (1-100 entries).",
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


# The advanced tools, in the order they're advertised.
ADVANCED_BROWSER_TOOL_CLASSES: tuple[type[Tool], ...] = (
    BrowserGoBackTool,
    BrowserGoForwardTool,
    BrowserReloadTool,
    BrowserTabsTool,
    BrowserHoverTool,
    BrowserDragTool,
    BrowserScrollTool,
    BrowserPressKeyTool,
    BrowserSelectOptionTool,
    BrowserCheckTool,
)
_TRUSTED_INPUT_CLASSES: tuple[type[Tool], ...] = (TrustedBrowserClickTool, TrustedBrowserTypeTool)


def register_browser_agent_tools(tools: dict[str, Tool]) -> None:
    """
    Complete the browser tool surface the tool manager registered.

    Swaps ``browser_click`` and ``browser_type`` for their trusted-input
    versions (keeping their place) and appends the advanced tools.

    :param tools: The manager's name-to-tool registry, already holding
        the standard five browser tools, e.g. ``{"browser_click": ...}``.
    """
    for cls in _TRUSTED_INPUT_CLASSES:
        if cls.name() in tools:
            tools[cls.name()] = cls()
    for cls in ADVANCED_BROWSER_TOOL_CLASSES:
        tools[cls.name()] = cls()
