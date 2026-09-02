from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from omnigent_company_brain.adapters.common import (
    AdaptedDocument,
    OrgSharedRequiredError,
    build_document,
    markdown_frontmatter,
)


def _slack_timestamp(value: str) -> datetime:
    return datetime.fromtimestamp(float(value), tz=UTC)


def _display_name(user_id: str, users: Mapping[str, str]) -> str:
    return users.get(user_id, user_id)


def _message_markdown(message: Mapping[str, Any], users: Mapping[str, str]) -> str:
    author = _display_name(str(message.get("user") or "unknown"), users)
    timestamp = _slack_timestamp(str(message["ts"])).isoformat().replace("+00:00", "Z")
    text = str(message.get("text") or "").strip()
    edited = " (edited)" if message.get("edited") else ""
    lines = [f"### {author} · {timestamp}{edited}", "", text or "_No text_"]
    reactions = sorted(
        (
            (str(reaction.get("name") or ""), int(reaction.get("count") or 0))
            for reaction in message.get("reactions") or []
        ),
        key=lambda item: item[0],
    )
    if reactions:
        rendered = ", ".join(f":{name}: ×{count}" for name, count in reactions)
        lines.extend(("", f"Reactions: {rendered}"))
    return "\n".join(lines)


def transform_slack_thread(
    *,
    connection_id: str,
    workspace_domain: str,
    channel: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    users: Mapping[str, str],
) -> AdaptedDocument:
    if channel.get("is_private") or channel.get("is_im") or channel.get("is_mpim"):
        raise OrgSharedRequiredError("Slack resource is not an organization-shared channel")
    if not messages:
        raise ValueError("Slack thread must contain at least one message")
    ordered = sorted(messages, key=lambda message: float(str(message["ts"])))
    root = ordered[0]
    root_ts = str(root.get("thread_ts") or root["ts"])
    channel_id = str(channel["id"])
    channel_name = str(channel["name"])
    source_url = (
        f"https://{workspace_domain}.slack.com/archives/{channel_id}/p{root_ts.replace('.', '')}"
    )
    root_text = str(root.get("text") or "Slack thread").strip()
    title_excerpt = " ".join(root_text.split())[:96] or "Slack thread"
    title = f"#{channel_name}: {title_excerpt}"
    created_at = _slack_timestamp(str(root["ts"]))
    modified_at = max(_slack_timestamp(str(message["ts"])) for message in ordered)
    frontmatter = markdown_frontmatter(
        title=title,
        provider="slack",
        external_resource_id=f"{channel_id}:{root_ts}",
        source_url=source_url,
        modified_at=modified_at,
    )
    participants = sorted(
        {_display_name(str(message.get("user") or "unknown"), users) for message in ordered},
        key=str.casefold,
    )
    rendered_messages = "\n\n".join(_message_markdown(message, users) for message in ordered)
    markdown = (
        f"{frontmatter}\n\n# {title}\n\n"
        f"Channel: [#{channel_name}]({source_url})  \n"
        f"Participants: {', '.join(participants)}\n\n"
        f"{rendered_messages}\n\n"
        f"[Open thread in Slack]({source_url})\n"
    )
    return build_document(
        provider="slack",
        connection_id=connection_id,
        external_resource_id=f"{channel_id}:{root_ts}",
        title=title,
        markdown=markdown,
        source_url=source_url,
        created_at=created_at,
        modified_at=modified_at,
        raw_payload={"channel": dict(channel), "messages": list(messages), "users": dict(users)},
        transform_schema_version="slack-thread.v1",
    )
