"""Captured mock-provider request inspection shared by process and browser tests."""


def advertised_tool_names(requests: list[dict]) -> set[str]:
    """Collect tool names from Responses, Messages, or chat-completions requests."""
    names: set[str] = set()
    for request in requests:
        if not isinstance(request, dict):
            continue
        for tool in request.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            # openai-agents Responses format: {"type": "function", "name": ...}
            # or a nested {"function": {"name": ...}} chat-completions shape.
            name = tool.get("name")
            if name is None and isinstance(tool.get("function"), dict):
                name = tool["function"].get("name")
            if isinstance(name, str):
                names.add(name)
    return names
