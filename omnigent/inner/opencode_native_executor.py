"""Executor that bridges Omnigent web turns into a native OpenCode session.

Built on :class:`omnigent.native_server_harness.NativeServerHarness`: the
runner owns the ``opencode serve`` process + SSE forwarder, and this
executor injects the latest web turn over the
:class:`omnigent.opencode_http_transport.OpenCodeHttpTransport` using the
loopback URL + auth secret published in the bridge state. Output is
streamed back by the runner-side forwarder, so ``run_turn`` only admits the
prompt and yields ``TurnComplete`` — the same injection/completion split as
codex-native.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omnigent.native_server_harness import NativeServerHarness
from omnigent.native_server_transport import NativePrompt
from omnigent.opencode_http_transport import OpenCodeHttpTransport
from omnigent.opencode_native_bridge import (
    OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR,
    OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    read_bridge_state,
)

# Canonical harness id, surfaced in harness error messages.
OPENCODE_NATIVE_HARNESS_ID = "opencode-native"


class OpenCodeNativeExecutor(NativeServerHarness):
    """
    Harness-side executor for ``omnigent opencode`` web UI turns.

    :param bridge_dir: Optional bridge directory override. ``None`` reads
        :data:`OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR`.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        super().__init__(
            harness_id=OPENCODE_NATIVE_HARNESS_ID,
            # OpenCode has no live-steer endpoint, so a mid-turn message is
            # admitted as a new prompt and the native server's own queue
            # promotes it when the active turn finishes.
            supports_enqueue=True,
            transport=OpenCodeHttpTransport(bridge_dir=self._bridge_dir),
            resolve_session_id=self._resolve_opencode_session_id,
            build_prompt=self._build_prompt_with_model_override,
        )

    def _build_prompt_with_model_override(self, content: Any) -> NativePrompt | None:
        """
        Build a prompt, pinning the resolved model so it governs from turn one.

        OpenCode's ``POST /session`` create body does NOT accept a model
        (verified against the OpenCode SDK ``SessionCreateData``); the model
        is a per-prompt field (``{"providerID", "modelID"}``). So the
        session's ``model_override`` is applied to EVERY injected prompt
        here. Because OpenCode persists the last-used model as the session
        default, pinning the first injected turn also governs subsequent
        TUI-typed turns — the override controls the run from the start, not
        just a later web turn. A per-turn ``config.model`` (if any) still
        wins: the base ``run_turn`` only fills the model when the prompt
        leaves it unset, so it skips a prompt this method already pinned.

        :param content: Executor message content (string or content blocks).
        :returns: The prompt with the resolved model applied, or ``None``
            when there is nothing to send.
        """
        prompt = _content_to_native_prompt(content)
        if prompt is None or prompt.model:
            return prompt
        state = read_bridge_state(self._bridge_dir)
        model = state.model_override if state is not None else None
        if not model:
            return prompt
        return dataclasses.replace(prompt, model=model)

    async def _resolve_opencode_session_id(self) -> str | None:
        """
        Resolve the OpenCode session id from bridge state.

        :returns: The OpenCode session id when this harness may inject into
            it, else ``None``.
        """
        state = read_bridge_state(self._bridge_dir)
        if state is None:
            return None
        if not _session_is_active(state.session_id, self._request_session_id):
            return None
        return state.opencode_session_id


def _bridge_dir_from_env() -> Path:
    """
    Resolve the native OpenCode bridge directory from harness spawn env.

    :returns: Bridge directory path.
    :raises RuntimeError: If the env var is missing.
    """
    raw = os.environ.get(OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR} is required")
    return Path(raw)


def _request_session_id_from_env() -> str | None:
    """
    Resolve the Omnigent session id that requested this harness process.

    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None``.
    """
    raw = os.environ.get(OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR, "").strip()
    return raw or None


def _session_is_active(session_id: str, request_session_id: str | None) -> bool:
    """
    Return whether this harness may inject into the native session.

    :param session_id: Session id from bridge state.
    :param request_session_id: Session id from harness spawn env.
    :returns: ``True`` when injection is allowed.
    """
    return request_session_id is None or request_session_id == session_id


def _content_to_native_prompt(content: Any) -> NativePrompt | None:
    """
    Normalize executor message content into a :class:`NativePrompt`.

    Text blocks are concatenated; image/file blocks pass through as
    attachments (the transport renders them as OpenCode file parts using
    their data URIs, so there is no socket-size limit to work around).

    :param content: Message content — a string or a list of content blocks
        such as ``{"type": "input_text", "text": "..."}`` and
        ``{"type": "input_image", "image_url": "data:image/png;base64,..."}``.
    :returns: The prompt, or ``None`` when there is nothing to send.
    """
    if isinstance(content, str):
        return NativePrompt(text=content) if content else None
    if isinstance(content, list):
        texts: list[str] = []
        attachments: list[Mapping[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"input_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
            elif block_type in {"input_image", "input_file"}:
                attachments.append(block)
        if not texts and not attachments:
            return None
        return NativePrompt(text="\n".join(texts), attachments=tuple(attachments))
    if content is None:
        return None
    return NativePrompt(text=json.dumps(content, ensure_ascii=True))
