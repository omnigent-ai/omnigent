"""Admission checks run before initial session items can be seeded or dispatched."""

import httpx
import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.server.helpers import create_test_agent


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("event_type", ["message", "slash_command"])
@pytest.mark.parametrize(
    "filename,mime",
    [("clip.mp4", "video/mp4"), ("clip.mp4", "text/plain"), ("payload.exe.txt", "text/plain")],
)
async def test_initial_items_reject_inline_binary_before_seed(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    filename: str,
    mime: str,
    remote: bool,
    app,
) -> None:
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {
            "filesystem_attachment_allowed_extensions": [".mp4"],
            "filesystem_attachment_denied_extensions": [".exe"],
        },
    )
    from omnigent.server.server_config import filesystem_attachment_policy

    app.state.filesystem_attachment_policy = filesystem_attachment_policy()
    agent = await create_test_agent(client)
    conversations = SqlAlchemyConversationStore(db_uri)
    before = {conv.id for conv in conversations.list_conversations(limit=100).data}
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [
                {
                    "type": event_type,
                    "data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image" if remote else "input_file",
                                "filename": filename,
                                **(
                                    {"image_url": f"https://example.com/{filename}"}
                                    if remote
                                    else {"file_data": f"data:{mime};base64,AA=="}
                                ),
                            }
                        ],
                    },
                }
            ],
        },
    )
    assert response.status_code in (400, 415), response.text
    assert filename in response.text
    assert {conv.id for conv in conversations.list_conversations(limit=100).data} == before


@pytest.mark.parametrize("event_type", ["message", "slash_command"])
async def test_wildcard_video_cannot_be_inlined_in_initial_items(client, db_uri, app, event_type):
    from omnigent.server.server_config import filesystem_attachment_policy

    app.state.filesystem_attachment_policy = filesystem_attachment_policy(
        {"filesystem_attachment_allowed_extensions": "*"}
    )
    agent = await create_test_agent(client)
    store = SqlAlchemyConversationStore(db_uri)
    before = {row.id for row in store.list_conversations(limit=100).data}
    for mime in ("text/plain", "image/png", "", "application/octet-stream"):
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "initial_items": [
                    {
                        "type": event_type,
                        "data": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_file",
                                    "filename": "clip.mp4",
                                    "file_data": f"data:{mime};base64,eA==",
                                }
                            ],
                        },
                    }
                ],
            },
        )
        assert response.status_code == 400, response.text
        assert "must be uploaded" in response.text
        assert {row.id for row in store.list_conversations(limit=100).data} == before
