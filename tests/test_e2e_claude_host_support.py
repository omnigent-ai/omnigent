"""Unit checks for the shared Claude host E2E helpers, against a fake sessions API."""

from __future__ import annotations

import json

import httpx

from tests.e2e._claude_host_support import _recall_code_words


def _message(item_id: str, role: str, text: str) -> dict[str, object]:
    block = "input_text" if role == "user" else "output_text"
    return {
        "id": item_id,
        "type": "message",
        "role": role,
        "content": [{"type": block, "text": text}],
    }


def test_recall_reads_only_the_reply_to_its_own_question() -> None:
    """An earlier reply naming the kept word doesn't count as the recall's answer."""
    items = [
        _message("msg_1", "user", "Remember KEPT. Reply with exactly: ACK0"),
        _message("msg_2", "assistant", "ACK0 KEPT"),
    ]

    def sessions_api(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            # The recall is answered once it is sent.
            items.append(
                _message(
                    "msg_3", "user", json.loads(request.content)["data"]["content"][0]["text"]
                )
            )
            items.append(_message("msg_4", "assistant", "KEPT DROPPED"))
            return httpx.Response(200, json={})
        if request.url.path.endswith("/items"):
            listed = list(items)
            if request.url.params.get("order") == "desc":
                listed.reverse()
            after = request.url.params.get("after")
            if after is not None:
                listed = listed[[item["id"] for item in listed].index(after) + 1 :]
            listed = listed[: int(request.url.params.get("limit", "100"))]
            return httpx.Response(200, json={"data": listed, "has_more": False})
        return httpx.Response(200, json={"status": "idle"})

    with httpx.Client(
        transport=httpx.MockTransport(sessions_api), base_url="http://test"
    ) as client:
        reply = _recall_code_words(client, session_id="conv_rewound", expected="KEPT")

    assert reply == "KEPT DROPPED"
