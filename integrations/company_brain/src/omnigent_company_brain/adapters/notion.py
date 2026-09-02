from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent_company_brain.adapters.common import (
    AdaptedDocument,
    OrgSharedRequiredError,
    build_document,
    markdown_frontmatter,
    parse_timestamp,
)


def _rich_text(items: Sequence[Mapping[str, Any]]) -> str:
    rendered: list[str] = []
    for item in items:
        text = str(item.get("plain_text") or item.get("text", {}).get("content") or "")
        href = item.get("href")
        annotations = item.get("annotations") or {}
        if annotations.get("code"):
            escaped = text.replace("`", "\\`")
            text = f"`{escaped}`"
        else:
            if annotations.get("bold"):
                text = f"**{text}**"
            if annotations.get("italic"):
                text = f"*{text}*"
            if annotations.get("strikethrough"):
                text = f"~~{text}~~"
        if href:
            text = f"[{text}]({href})"
        rendered.append(text)
    return "".join(rendered)


def _render_blocks(blocks: Sequence[Mapping[str, Any]], depth: int = 0) -> list[str]:
    lines: list[str] = []
    numbered_index = 0
    for block in blocks:
        block_type = str(block.get("type") or "unsupported")
        value = block.get(block_type) or {}
        text = _rich_text(value.get("rich_text") or [])
        if block_type != "numbered_list_item":
            numbered_index = 0
        if block_type == "paragraph":
            lines.append(text)
        elif block_type in {"heading_1", "heading_2", "heading_3"}:
            level = int(block_type[-1])
            lines.append(f"{'#' * level} {text}")
        elif block_type == "bulleted_list_item":
            lines.append(f"{'  ' * depth}- {text}")
        elif block_type == "numbered_list_item":
            numbered_index += 1
            lines.append(f"{'  ' * depth}{numbered_index}. {text}")
        elif block_type == "to_do":
            marker = "x" if value.get("checked") else " "
            lines.append(f"{'  ' * depth}- [{marker}] {text}")
        elif block_type == "quote":
            lines.append(f"> {text}")
        elif block_type == "callout":
            icon = (value.get("icon") or {}).get("emoji")
            lines.append(f"> {icon + ' ' if icon else ''}{text}")
        elif block_type == "code":
            language = str(value.get("language") or "")
            lines.append(f"```{language}\n{text}\n```")
        elif block_type == "divider":
            lines.append("---")
        elif block_type == "bookmark":
            url = str(value.get("url") or "")
            lines.append(f"[{url}]({url})")
        elif block_type == "child_page":
            title = str(value.get("title") or "Untitled child page")
            lines.append(f"- Child page: **{title}**")
        elif block_type == "table":
            rows = value.get("rows") or block.get("children") or []
            rendered_rows = [
                [_rich_text(cell) for cell in row.get("table_row", {}).get("cells") or []]
                for row in rows
            ]
            if rendered_rows:
                width = max(len(row) for row in rendered_rows)
                padded = [row + [""] * (width - len(row)) for row in rendered_rows]
                lines.append("| " + " | ".join(padded[0]) + " |")
                lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
                lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
        else:
            lines.append(f"> Unsupported Notion block preserved in raw provenance: `{block_type}`")
        children = block.get("children") or []
        if children and block_type != "table":
            lines.extend(_render_blocks(children, depth + 1))
    return lines


def _page_title(page: Mapping[str, Any]) -> str:
    properties = page.get("properties") or {}
    for property_value in properties.values():
        if property_value.get("type") == "title":
            title = _rich_text(property_value.get("title") or []).strip()
            if title:
                return title
    return "Untitled Notion page"


def transform_notion_page(
    *,
    connection_id: str,
    page: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
    org_shared: bool,
) -> AdaptedDocument:
    if not org_shared:
        raise OrgSharedRequiredError("Notion page was not shared with the integration")
    if page.get("archived") or page.get("in_trash"):
        raise ValueError("archived Notion pages must be emitted as deletions")
    external_id = str(page["id"])
    title = _page_title(page)
    source_url = str(page["url"])
    modified_at = parse_timestamp(str(page["last_edited_time"]))
    if modified_at is None:
        raise ValueError("Notion last_edited_time is required")
    frontmatter = markdown_frontmatter(
        title=title,
        provider="notion",
        external_resource_id=external_id,
        source_url=source_url,
        modified_at=modified_at,
    )
    body = "\n\n".join(line for line in _render_blocks(blocks) if line)
    markdown = f"{frontmatter}\n\n# {title}\n\n{body}\n\n[Open in Notion]({source_url})\n"
    return build_document(
        provider="notion",
        connection_id=connection_id,
        external_resource_id=external_id,
        title=title,
        markdown=markdown,
        source_url=source_url,
        created_at=page.get("created_time"),
        modified_at=modified_at,
        raw_payload={"page": dict(page), "blocks": list(blocks)},
        transform_schema_version="notion-page.v1",
    )
