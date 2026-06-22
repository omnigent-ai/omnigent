from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-google")

_CREATED_DOCS: dict[str, str] = {}
_CREATED_DRAFTS: dict[str, str] = {}
_DOC_COUNTER = 0
_DRAFT_COUNTER = 0


@mcp.tool()
def docs_document_create(title: str) -> dict:
    global _DOC_COUNTER
    _DOC_COUNTER += 1
    document_id = f"created-doc-{_DOC_COUNTER}"
    _CREATED_DOCS[document_id] = title
    return {"document_id": document_id, "title": title}


@mcp.tool()
def docs_document_batch_update(document_id: str, requests: list[dict] | None = None) -> dict:
    if document_id not in _CREATED_DOCS:
        return {
            "status": "updated-preexisting",
            "document_id": document_id,
            "requests": requests or [],
        }
    return {"status": "updated-created", "document_id": document_id, "requests": requests or []}


@mcp.tool()
def gmail_draft_create(to: str, subject: str = "", body: str = "") -> dict:
    global _DRAFT_COUNTER
    _DRAFT_COUNTER += 1
    draft_id = f"created-draft-{_DRAFT_COUNTER}"
    _CREATED_DRAFTS[draft_id] = to
    return {"draft_id": draft_id, "message": {"to": to, "subject": subject, "body": body}}


@mcp.tool()
def gmail_draft_update(draft_id: str, subject: str = "", body: str = "") -> dict:
    status = "updated-created" if draft_id in _CREATED_DRAFTS else "updated-preexisting"
    return {"status": status, "draft_id": draft_id, "subject": subject, "body": body}


if __name__ == "__main__":
    mcp.run()
