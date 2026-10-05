"""Resume command parsing and bounded conversation catch-up."""

import re
from urllib.parse import urlsplit

from omnigent_slack.events import OmnigentError


def parse_resume(text: str, web_link: str) -> tuple[str, bool]:
    parts = text.split()
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "--force"):
        raise OmnigentError("Use resume <session_id or Omnigent web URL> [--force].")
    target = parts[1]
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].split("|", 1)[0]
    if "://" in target:
        supplied, expected = urlsplit(target), urlsplit(web_link)
        prefix = expected.path.rsplit("/", 1)[0] + "/"
        if (supplied.scheme, supplied.netloc) != (
            expected.scheme,
            expected.netloc,
        ) or not supplied.path.startswith(prefix):
            raise OmnigentError(
                "That URL belongs to another server. Use this bot's configured Omnigent server."
            )
        if supplied.query != expected.query:
            raise OmnigentError(
                "That URL belongs to another workspace. Use this bot's configured workspace."
            )
        target = supplied.path[len(prefix) :]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", target):
        raise OmnigentError("Invalid session ID. Copy the ID or conversation URL from Omnigent.")
    return target, len(parts) == 3


def recap(items: list[dict]) -> str:
    messages = []
    for item in items:
        if item.get("type") != "message":
            continue
        data = item.get("data", item)
        if not isinstance(data, dict) or data.get("role") not in ("user", "assistant"):
            continue
        content = data.get("content", [])
        text = (
            content
            if isinstance(content, str)
            else " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and isinstance(block.get("text"), str)
            )
            if isinstance(content, list)
            else ""
        )
        if text:
            escaped = text[:500].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            messages.append(
                f"{data['role'].capitalize()}: {escaped}" + ("…" if len(text) > 500 else "")
            )
    return "\n".join(messages[-4:]) or "No recent messages are available."
