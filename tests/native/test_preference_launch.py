"""Temporary intent survives every native session's multipart creation boundary."""

import email
import importlib
import json

import click
import httpx
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "harness",
    [
        "antigravity",
        "claude",
        "codex",
        "cursor",
        "devin",
        "goose",
        "hermes",
        "kimi",
        "kiro",
        "opencode",
        "pi",
        "qwen",
    ],
)
@pytest.mark.parametrize("temporary", [False, True])
async def test_native_create_preserves_temporary_intent(harness, temporary):
    captured = []

    def handle(request):
        message = email.message_from_bytes(
            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
        )
        for part in message.walk():
            if part.get_param("name", header="content-disposition") == "metadata":
                captured.append(json.loads(part.get_payload(decode=True)))
        return httpx.Response(201, json={"session_id": "new-session"})

    module = importlib.import_module(f"omnigent.harnesses.{harness}_native.main")
    create = getattr(module, f"_create_{harness}_session")
    kwargs = {"bridge_id": "bridge"} if harness in {"antigravity", "claude", "codex"} else {}
    with click.Context(click.Command(harness)) as context:
        context.meta["temporary_preferences"] = temporary
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handle)
        ) as client:
            assert await create(client, b"bundle", **kwargs) == "new-session"
    assert len(captured) == 1
    labels = captured[0]["labels"]
    assert labels["omnigent.wrapper"]
    assert labels.get("omnigent.preferences.temporary") == ("true" if temporary else None)
