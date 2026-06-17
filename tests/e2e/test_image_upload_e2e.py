"""
End-to-end smoke test for image upload + multimodal inference
through every harness wrap (claude-sdk, codex, pi, openai-agents).

Parametrized across the four user-facing harnesses. For each:
register an agent, create a runner-bound session, upload an image
via the session-scoped files API, post a user message with an
``input_image`` content block referencing the file, and verify the
LLM's response demonstrates it actually saw the image (not "no image
attached").

Gated on ``--profile`` (the existing ``tests/conftest.py`` option).
Without it, tests skip. Run with::

    .venv/bin/python -m pytest \\
        tests/e2e/test_image_upload_e2e.py \\
        --profile oss -v

Each harness's parametrize row surfaces in the test ID
(``[claude-sdk]``, ``[codex]``, etc.) so a per-harness failure
is visible without re-reading the parametrize tuple.

The turn is driven through a runner-bound session and the terminal
result is read from the session snapshot — the legacy
``POST /v1/responses`` route was removed.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.e2e._harness_probes import (
    HARNESS_PROBES,
    HarnessProbe,
    skip_if_harness_cli_missing,
)
from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Checked-in test image: 100x100 red square with a blue center.
_TEST_IMAGE_PATH = _REPO_ROOT / "tests" / "resources" / "test_image.png"

_IMAGE_HARNESS_PROBES: list[HarnessProbe] = list(HARNESS_PROBES)
_IMAGE_HARNESS_IDS: list[str] = [p.harness for p in _IMAGE_HARNESS_PROBES]


@pytest.fixture
def databricks_profile(request: pytest.FixtureRequest) -> str:
    """
    Return the ``--profile`` CLI arg, or skip if not provided.

    :param request: Pytest fixture request.
    :returns: The profile name, e.g. ``"oss"``.
    """
    profile: str = request.config.getoption("--profile")
    if not profile:
        pytest.skip("image upload e2e requires --profile <name> (e.g. --profile oss)")
    return profile


@pytest.mark.parametrize("probe", _IMAGE_HARNESS_PROBES, ids=_IMAGE_HARNESS_IDS)
def test_image_upload_reaches_llm(
    probe: HarnessProbe,
    databricks_profile: str,
    http_client: httpx.Client,
    live_runner_id: str,
) -> None:
    """
    Upload an image, send it to an agent, verify the LLM saw it.

    Full AP-side e2e per harness:

    1. Register an agent with the parametrized harness + model.
    2. Create a runner-bound session and upload a test PNG via the
       session-scoped files API.
    3. Post a user message (text + ``input_image``) asking the model
       to identify the dominant color; poll the snapshot until terminal.
    4. Assert the model's response mentions "red" or "blue"
       (the two colors in the test image), proving it actually
       received and analyzed the image.

    The test image is a red square with a blue center. The
    assertion checks that the model mentions "red" or "blue,"
    proving it actually saw the image rather than claiming
    no image was provided.

    :param probe: The harness probe (harness name + model).
    :param databricks_profile: The ``--profile`` value.
    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: The live runner id sessions bind to.
    """
    skip_if_harness_cli_missing(probe.harness)

    agent_name = f"image-e2e-{probe.harness}"
    # The returned name differs from agent_name on llm_flaky reruns.
    agent_name = register_inline_agent(
        http_client,
        name=agent_name,
        harness=probe.harness,
        model=probe.model,
        profile=databricks_profile,
        prompt=(
            "You are a vision assistant. When the user sends an "
            "image, describe what you see. Be specific about "
            "colors, shapes, and content."
        ),
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    # test_image.png is a checked-in resource; its absence means the
    # test setup is broken, not a skip.
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

    assert body["status"] == "completed", (
        f"[{probe.harness}] response failed: {body.get('error', 'unknown')}"
    )

    text = final_assistant_text(body).lower().strip()
    assert text, f"[{probe.harness}] no assistant output text in response"

    # The test image is a 100x100 red square with a blue center.
    # If the model received the image, it will mention "red"
    # (background) or "blue" (center) — either proves it saw the
    # image. If multimodal content was dropped, the model has no
    # image to analyze and won't mention either color.
    assert "red" in text or "blue" in text, (
        f"[{probe.harness}] LLM did not identify any color in "
        f"the image — multimodal content likely dropped before "
        f"reaching the model. Full response:\n{text}"
    )
