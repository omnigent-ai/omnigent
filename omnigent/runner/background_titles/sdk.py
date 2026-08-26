"""Background title generation through an isolated SDK harness process."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from omnigent.debug_logging import runner_primary_session_id
from omnigent.runner.background_titles.service import (
    BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS,
    BACKGROUND_TITLE_INSTRUCTIONS,
    BACKGROUND_TITLE_MAX_OUTPUT_TOKENS,
    BackgroundTitleContext,
    BackgroundTitleHarnessError,
)

_logger = logging.getLogger("omnigent.runner.background_titles.sdk")


async def generate_background_title(context: BackgroundTitleContext) -> str | None:
    """Generate a title with a synthetic tool-free SDK harness session."""
    spawn_env = dict(context.spawn_env)
    if context.harness == "codex":
        spawn_env.update(
            {
                "HARNESS_CODEX_DISABLE_NATIVE_TOOLS": "1",
                "HARNESS_CODEX_ENABLE_WEB_SEARCH": "0",
                "HARNESS_CODEX_MINIMAL_CONFIG": "1",
                "HARNESS_CODEX_SKILLS_FILTER": json.dumps("none"),
            }
        )
        spawn_env.pop("HARNESS_CODEX_AGENT_NAME", None)
        spawn_env.pop("HARNESS_CODEX_BUNDLE_DIR", None)
    else:
        spawn_env["HARNESS_CLAUDE_SDK_SKILLS_FILTER"] = json.dumps("none")
        spawn_env.pop("HARNESS_CLAUDE_SDK_AGENT_NAME", None)
        spawn_env.pop("HARNESS_CLAUDE_SDK_BUNDLE_DIR", None)

    process_key = uuid.uuid4().hex
    event_body = {
        "type": "message",
        "role": "user",
        "content": f"<user_message>\n{context.prompt}\n</user_message>",
        "model": "session-title",
        "tools": [],
        "instructions": BACKGROUND_TITLE_INSTRUCTIONS,
        "reasoning": {"effort": "low"},
        "max_output_tokens": BACKGROUND_TITLE_MAX_OUTPUT_TOKENS,
    }
    try:
        client = await context.process_manager.get_client(
            process_key,
            context.harness,
            env=spawn_env,
        )
        text_parts: list[str] = []
        async with asyncio.timeout(BACKGROUND_TITLE_INFERENCE_TIMEOUT_SECONDS):
            async with client.stream(
                "POST",
                f"/v1/sessions/{process_key}/events",
                json=event_body,
                timeout=None,
            ) as response:
                if response.status_code != 200:
                    raise BackgroundTitleHarnessError(
                        f"Harness returned HTTP {response.status_code}."
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        continue
                    event = json.loads(payload)
                    event_type = event.get("type")
                    if event_type == "policy_evaluation.requested":
                        evaluation_id = event.get("evaluation_id")
                        phase = event.get("phase")
                        if not isinstance(evaluation_id, str) or not evaluation_id:
                            raise BackgroundTitleHarnessError(
                                "Harness requested policy evaluation without an id."
                            )
                        action = (
                            "POLICY_ACTION_DENY"
                            if phase == "PHASE_TOOL_CALL"
                            else "POLICY_ACTION_ALLOW"
                        )
                        await client.post(
                            f"/v1/sessions/{process_key}/events",
                            json={
                                "type": "policy_verdict",
                                "evaluation_id": evaluation_id,
                                "action": action,
                            },
                        )
                    elif event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            text_parts.append(delta)
                    elif event_type == "response.failed":
                        response_payload = event.get("response")
                        error_payload = (
                            response_payload.get("error")
                            if isinstance(response_payload, dict)
                            else None
                        )
                        error_message = (
                            error_payload.get("message")
                            if isinstance(error_payload, dict)
                            else None
                        )
                        detail = (
                            error_message.strip()
                            if isinstance(error_message, str) and error_message.strip()
                            else "Harness title generation failed."
                        )
                        _logger.warning(
                            "background title harness failed process=%s detail=%s",
                            process_key,
                            detail,
                            extra={"session_id": runner_primary_session_id()},
                        )
                        raise BackgroundTitleHarnessError(detail)
                    elif event_type == "response.completed":
                        break
        title = "".join(text_parts).strip()
        return title or None
    finally:
        await context.process_manager.release(process_key)
