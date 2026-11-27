"""Mock-provider readiness and captured request inspection, shared by process and browser tests."""

import httpx


def mock_llm_answers(base_url: str) -> bool:
    """
    Whether the mock LLM server at *base_url* answers ``GET /stats`` yet.

    A booting server can refuse the connection, accept it and close it before
    replying, or answer slowly; each means "not yet", so a readiness loop keeps polling.

    :param base_url: The mock server's base URL, e.g. ``"http://127.0.0.1:8001"``.
    :returns: True once ``/stats`` answers 200.
    """
    try:
        return httpx.get(f"{base_url}/stats", timeout=1.0).status_code == 200
    except httpx.TransportError:
        return False


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


def captured_tool_result(requests: list[dict], call_id: str) -> str:
    """The newest captured result payload for *call_id*, or a note when none exists.

    Covers the Messages ``tool_result`` block and the Responses
    ``function_call_output`` item, so every harness reports it. Returns the
    result's own text verbatim — the runner's JSON — not a re-encoded copy.
    """
    for request in reversed(requests):
        if not isinstance(request, dict):
            continue
        for message in request.get("messages", []):
            if message.get("role") != "user":
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("tool_use_id") == call_id
                ):
                    return tool_result_text(block.get("content"), error=block.get("is_error"))
        for entry in request.get("input", []):
            if (
                isinstance(entry, dict)
                and entry.get("type") == "function_call_output"
                and entry.get("call_id") == call_id
            ):
                return tool_result_text(entry.get("output"), error=None)
    return f"no captured tool result for {call_id!r}"


def tool_result_text(payload: object, *, error: object) -> str:
    """A tool result's own text, prefixed with its error flag when set."""
    if isinstance(payload, str):
        text = payload
    elif isinstance(payload, list):
        text = "\n".join(
            block.get("text", "")
            for block in payload
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    else:
        text = "" if payload is None else str(payload)
    prefix = "is_error=True " if error else ""
    return f"{prefix}{text}"
