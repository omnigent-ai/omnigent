"""List all built-in tools available in Omnigent.

Returns the live registry of builtin tool names and their
descriptions, so the onboarding assistant always recommends
from the current set — not a stale hardcoded list.

Each tool class is imported individually from its own module to
avoid importing the ``omnigent.tools.builtins`` package (which
transitively pulls in modules that conflict with the ``mcp`` pip
package in subprocess environments).
"""

from omnigent_client import tool

# Maps every builtin tool name to (module_path, class_name).
# This is the sole source of truth — when a new builtin is added,
# add it here. Each module is imported individually to avoid the
# transitive import chain from omnigent.tools.builtins.__init__.
_TOOL_CLASSES: dict[str, tuple[str, str]] = {
    "download_file": ("omnigent.tools.builtins.download_file", "DownloadFileTool"),
    "export_agent": ("omnigent.tools.builtins.export_agent", "ExportAgentTool"),
    "list_files": ("omnigent.tools.builtins.list_files", "ListFilesTool"),
    "search_conversations": (
        "omnigent.tools.builtins.search_conversations",
        "SearchConversationsTool",
    ),
    "upload_file": ("omnigent.tools.builtins.upload_file", "UploadFileTool"),
    "web_fetch": ("omnigent.tools.builtins.web_fetch", "WebFetchTool"),
    "web_search": ("omnigent.tools.builtins.web_search", "WebSearchTool"),
}


def _hindsight_available() -> bool:
    """Return True when the optional ``hindsight-client`` SDK is installed."""
    import importlib.util

    return importlib.util.find_spec("hindsight_client") is not None


# Hindsight memory tools (optional ``hindsight`` extra). Advertised only when
# the SDK is installed, so the assistant never recommends unusable tools.
if _hindsight_available():
    _TOOL_CLASSES.update(
        {
            "hindsight_retain": ("omnigent.tools.builtins.hindsight", "HindsightRetainTool"),
            "hindsight_recall": ("omnigent.tools.builtins.hindsight", "HindsightRecallTool"),
            "hindsight_reflect": ("omnigent.tools.builtins.hindsight", "HindsightReflectTool"),
        }
    )


@tool
def list_builtin_tools() -> str:
    """
    List all built-in tools available in Omnigent.

    Returns tool names and descriptions. Call this before
    recommending tools for a new agent.
    """
    import importlib

    lines: list[str] = []
    for name in sorted(_TOOL_CLASSES):
        module_path, class_name = _TOOL_CLASSES[name]
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        lines.append(f"- {name}: {cls.description()}")

    return "\n".join(lines)
