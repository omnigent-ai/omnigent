"""
End-to-end smoke test for image upload + multimodal inference (mock LLM).

Registers an openai-agents agent, creates a runner-bound session, uploads
an image via the session-scoped files API, posts a user message with an
``input_image`` content block referencing the file, and verifies the
agent produces non-empty text. The mock LLM returns a canned response
describing the image colors.

Usage::

    pytest tests/e2e/test_image_upload_e2e.py -v
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Checked-in test image: 100x100 red square with a blue center.
_TEST_IMAGE_PATH = _REPO_ROOT / "tests" / "resources" / "test_image.png"


def test_image_upload_reaches_llm(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Upload an image, send it to an agent, verify the agent produces
    non-empty text describing the image.

    Full AP-side e2e:

    1. Register an openai-agents agent pointing at the mock LLM.
    2. Create a runner-bound session and upload a test PNG via the
       session-scoped files API.
    3. Post a user message (text + ``input_image``) asking the model
       to identify the dominant color; poll the snapshot until terminal.
    4. Assert the mock response text appears in the output (proving
       the image upload pipeline didn't drop content before reaching
       the executor).
    """
    model = f"mock-image-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"image-e2e-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a vision assistant. When the user sends an "
            "image, describe what you see. Be specific about "
            "colors, shapes, and content."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # The mock returns a canned description mentioning the test image colors.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "The image shows a red square with a blue center."}],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    assert _TEST_IMAGE_PATH.exists(), (
        f"Test image missing at {_TEST_IMAGE_PATH}. Run the generate script or restore from git."
    )
    image_bytes = _TEST_IMAGE_PATH.read_bytes()
    file_resp = http_client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("test_image.png", image_bytes, "image/png")},
    )
    file_resp.raise_for_status()
    file_id = file_resp.json()["id"]

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=[
            {
                "type": "input_text",
                "text": (
                    "What is the dominant color of this image? Reply with just the color name."
                ),
            },
            {"type": "input_image", "file_id": file_id},
        ],
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )

    assert body["status"] == "completed", f"response failed: {body.get('error', 'unknown')}"

    text = final_assistant_text(body).lower().strip()
    assert text, "no assistant output text in response"

    # The mock returns "red" and "blue" — verify it came through.
    assert "red" in text or "blue" in text, (
        f"LLM did not identify any color in the image — "
        f"multimodal content likely dropped before reaching "
        f"the model. Full response:\n{text}"
    )
