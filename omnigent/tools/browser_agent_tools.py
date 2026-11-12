"""
Opt-in for the advanced embedded-browser agent tools.

The desktop app's Settings > General switch "Advanced browser tools for
agents" (off by default) decides whether agents in new sessions see the
browser tools beyond the original five (navigate, snapshot, click, type,
screenshot): history, tabs, hover, drag, scroll, keys, native selects and
checkboxes, plus ``browser_click``'s ``button`` / ``double_click`` options.

The switch is local to the app. The web app sends it as a header when it
creates a session, the server records it as a session label, and the
runner hides the advanced tools from every tool surface (request-driven
turns, the native relay and pi-native) unless the label is on. The
renderer also refuses the advanced actions while the switch is off.

The header is only sent from a desktop app that has the tools: "on" with
the switch on, "trusted" with it off. Sessions created anywhere else get
the standard click and type schemas, so an agent is never promised input
the app it runs in can't perform.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import httpx

# Header the web app sends on session creation: "on" (switch on) or "trusted"
# (switch off), and only from a desktop app that sends trusted input.
BROWSER_AGENT_TOOLS_HEADER = "x-omnigent-browser-agent-tools"
# Session label recording it at creation, e.g. ``{"omnigent.browser_agent_tools": "on"}``.
BROWSER_AGENT_TOOLS_LABEL = "omnigent.browser_agent_tools"
# Per-turn runner message keys the server derives from the label.
BROWSER_AGENT_TOOLS_TURN_KEY = "browser_agent_tools"
BROWSER_TRUSTED_INPUT_TURN_KEY = "browser_trusted_input"

# What a session's agents get:
# - "on": every browser tool, with click and type as the desktop app's real
#   (trusted) input performs them;
# - "trusted": the standard five tools with those click / type semantics;
# - "standard": the standard five tools exactly, for desktop apps without
#   trusted input (and sessions not created from a desktop app).
BrowserAgentToolsMode = Literal["on", "trusted", "standard"]
_LABEL_MODES: dict[str, BrowserAgentToolsMode] = {"on": "on", "trusted": "trusted"}

# Browser tools that only appear while the switch is on. The original five
# (navigate, snapshot, click, type, screenshot) are always available.
ADVANCED_BROWSER_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "browser_go_back",
        "browser_go_forward",
        "browser_reload",
        "browser_tabs",
        "browser_hover",
        "browser_drag",
        "browser_scroll",
        "browser_press_key",
        "browser_select_option",
        "browser_check",
    }
)
# Always-available tools with options or advice that only apply while the
# switch is on: the arguments to drop, and where the description's
# advanced-only tail starts (``browser_click``'s trusted mouse options, and
# ``browser_type``'s pointer to ``browser_press_key``).
_ADVANCED_TAILS: dict[str, tuple[tuple[str, ...], str]] = {
    "browser_click": (("button", "double_click"), " `button` "),
    "browser_type": ((), " Doesn't press Enter"),
}


# Labels a forked session takes from its own request, never from its source: the
# forking app decides which tools it can run, like a new session's.
BROWSER_AGENT_TOOLS_CREATION_LABEL_KEYS = frozenset({BROWSER_AGENT_TOOLS_LABEL})


def browser_agent_tools_creation_labels(headers: Mapping[str, str]) -> dict[str, str]:
    """
    Labels to add to a session being created, from the web app's header.

    :param headers: The create request's headers.
    :returns: ``{"omnigent.browser_agent_tools": "on"}`` or ``{... : "trusted"}``
        from the header, otherwise ``{}`` (the standard tools).
    """
    value = headers.get(BROWSER_AGENT_TOOLS_HEADER, "").strip().lower()
    return {BROWSER_AGENT_TOOLS_LABEL: value} if value in _LABEL_MODES else {}


def apply_browser_agent_tools_creation_labels(
    labels: dict[str, str], headers: Mapping[str, str]
) -> None:
    """
    Set a new session's browser-tool label from its create request.

    The app's header decides. Without one, a label already in *labels* (a
    continuation, e.g. a native ``/clear`` replacement, copying its
    predecessor's labels) is kept when it names a mode, and dropped otherwise.

    :param labels: The create request's labels; updated in place.
    :param headers: The create request's headers.
    """
    seeded = labels.pop(BROWSER_AGENT_TOOLS_LABEL, None)
    if seeded in _LABEL_MODES:
        labels[BROWSER_AGENT_TOOLS_LABEL] = seeded
    labels.update(browser_agent_tools_creation_labels(headers))


def browser_agent_tools_mode(labels: Mapping[str, str] | None) -> BrowserAgentToolsMode:
    """
    The browser tools a session's labels give its agents.

    :param labels: Session labels, e.g. ``{"omnigent.browser_agent_tools": "on"}``.
    :returns: ``"on"``, ``"trusted"`` or (by default) ``"standard"``.
    """
    return _LABEL_MODES.get((labels or {}).get(BROWSER_AGENT_TOOLS_LABEL, ""), "standard")


def browser_agent_tools_enabled(labels: Mapping[str, str] | None) -> bool:
    """
    Whether a session's labels turn the advanced browser tools on.

    :param labels: Session labels, e.g. ``{"omnigent.browser_agent_tools": "on"}``.
    :returns: ``True`` only when the label is ``"on"``.
    """
    return browser_agent_tools_mode(labels) == "on"


def browser_agent_tools_turn_flag(labels: Mapping[str, str] | None) -> dict[str, bool]:
    """
    Fields to spread into the server's per-turn runner message.

    :param labels: The session's labels.
    :returns: ``{"browser_agent_tools": <on>, "browser_trusted_input": <on or trusted>}``.
    """
    mode = browser_agent_tools_mode(labels)
    return {
        BROWSER_AGENT_TOOLS_TURN_KEY: mode == "on",
        BROWSER_TRUSTED_INPUT_TURN_KEY: mode != "standard",
    }


def _schema_function(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """The name-bearing dict of a nested (OpenAI) or flat (relay) schema."""
    function = schema.get("function")
    return function if isinstance(function, Mapping) else schema


def _basic_schema(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """Copy of an always-available tool's schema without its advanced-only parts."""
    args, tail = _ADVANCED_TAILS[name]
    basic = copy.deepcopy(schema)
    function = basic["function"] if isinstance(basic.get("function"), dict) else basic
    description = function.get("description")
    if isinstance(description, str) and tail in description:
        function["description"] = description.split(tail, 1)[0]
    parameters = function.get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if isinstance(properties, dict):
        for arg in args:
            properties.pop(arg, None)
    return basic


def _standard_schema(schema: dict[str, Any], name: str) -> dict[str, Any]:
    """Copy of a click / type schema with the standard tool's description and arguments."""
    from omnigent.tools.builtins.browser import BrowserClickTool, BrowserTypeTool

    standard_cls = BrowserClickTool if name == "browser_click" else BrowserTypeTool
    standard = standard_cls().get_schema()["function"]
    replaced = copy.deepcopy(schema)
    function = replaced["function"] if isinstance(replaced.get("function"), dict) else replaced
    function["description"] = standard["description"]
    function["parameters"] = copy.deepcopy(standard["parameters"])
    return replaced


def filter_browser_agent_tool_schemas(
    schemas: list[dict[str, Any]], *, mode: BrowserAgentToolsMode
) -> list[dict[str, Any]]:
    """
    Fit a tool-schema list's browser tools to a session's mode.

    Below ``"on"``, drops the advanced tools. ``"trusted"`` strips
    ``browser_click``'s ``button`` / ``double_click`` options and description
    advice that names a hidden tool; ``"standard"`` gives click and type
    their standard schemas (``omnigent/tools/builtins/browser.py``).
    Handles both the nested OpenAI shape (``{"function": {"name": ...}}``)
    and the flat relay shape (``{"name": ...}``). Other schemas are returned
    untouched.

    :param schemas: Tool schemas, e.g. a session's builtin surface.
    :param mode: The session's mode, from ``browser_agent_tools_mode``.
    :returns: ``schemas`` itself when ``"on"``, otherwise a filtered copy.
    """
    if mode == "on":
        return schemas
    kept: list[dict[str, Any]] = []
    for schema in schemas:
        name = _schema_function(schema).get("name")
        if name in ADVANCED_BROWSER_TOOL_NAMES:
            continue
        if name in _ADVANCED_TAILS:
            schema = (
                _basic_schema(schema, name)
                if mode == "trusted"
                else _standard_schema(schema, name)
            )
        kept.append(schema)
    return kept


def filter_turn_browser_tools(
    schemas: list[dict[str, Any]], msg_body: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """
    Filter a request-driven turn's spec tools by the server's per-turn flags.

    Absent flags (an older server, or a runner-internal turn) count as
    the standard tools.

    :param schemas: The turn's spec tool schemas.
    :param msg_body: The runner message for the turn.
    :returns: The schemas the model may see this turn.
    """
    mode: BrowserAgentToolsMode = (
        "on"
        if msg_body.get(BROWSER_AGENT_TOOLS_TURN_KEY) is True
        else "trusted"
        if msg_body.get(BROWSER_TRUSTED_INPUT_TURN_KEY) is True
        else "standard"
    )
    return filter_browser_agent_tool_schemas(schemas, mode=mode)


async def filter_native_relay_browser_tools(
    schemas: list[dict[str, Any]],
    *,
    session_id: str,
    server_client: httpx.AsyncClient | None,
    session_labels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Filter a native harness's session-scoped relay surface by the label.

    :param schemas: Flat relay schemas from ``build_native_relay_tool_schemas``.
    :param session_id: The session, e.g. ``"conv_abc123"``.
    :param server_client: Runner server client, used to read the labels
        when the caller doesn't have them. ``None`` with no labels counts
        as off.
    :param session_labels: The session's labels, when already known.
    :returns: The relay schemas the native harness may advertise.
    """
    labels = session_labels
    if labels is None and server_client is not None:
        from omnigent.runner.native.orchestration import _session_labels_for_runner_spawn

        labels = await _session_labels_for_runner_spawn(
            server_client=server_client, session_id=session_id
        )
    return filter_browser_agent_tool_schemas(schemas, mode=browser_agent_tools_mode(labels))
