"""Data-only builtin tool catalog shared by validation and runtime dispatch."""

BUILTIN_FACTORIES: dict[str, str] = {
    "web_search": "omnigent.tools.builtins.web_search:WebSearchTool",
    "nimble_research": "omnigent.tools.builtins.nimble_research:NimbleResearchTool",
    "nimble_extract": "omnigent.tools.builtins.nimble_extract:NimbleExtractTool",
    "upload_file": "omnigent.tools.builtins:_create_upload_file",
    "list_files": "omnigent.tools.builtins:_create_list_files",
    "download_file": "omnigent.tools.builtins:_create_download_file",
    "search_conversations": "omnigent.tools.builtins:_create_search_conversations",
    "export_agent": "omnigent.tools.builtins:_create_export_agent",
}

FRAMEWORK_TOOL_NAMES = frozenset(
    {
        "web_fetch",
        "list_comments",
        "update_comment",
        "sys_list_models",
        "sys_advise_models",
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_screenshot",
    }
)

HINDSIGHT_FACTORIES: dict[str, str] = {
    "hindsight_retain": "omnigent.tools.builtins:_create_hindsight_retain",
    "hindsight_recall": "omnigent.tools.builtins:_create_hindsight_recall",
    "hindsight_reflect": "omnigent.tools.builtins:_create_hindsight_reflect",
}

# Offline results must not depend on optional packages installed on this host.
STATIC_BUILTIN_NAMES = (
    frozenset(BUILTIN_FACTORIES) | FRAMEWORK_TOOL_NAMES | frozenset(HINDSIGHT_FACTORIES)
)
