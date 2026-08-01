"""Pre-launch model catalog for the GitHub Copilot harness.

Mirrors ``omnigent.claude_native.claude_native_model_options`` for the SDK
copilot harness: the host daemon calls this to answer the Web UI's pre-launch
``/model-options`` probe. The catalog comes from the Copilot backend itself
(``CopilotClient.list_models()``), so it reflects exactly the models the
signed-in seat may use, enterprise policy included, rather than a
hardcoded list.

Auth mirrors the runtime's no-spec precedence
(:func:`omnigent.runtime.workflow._build_copilot_spawn_env` with no session
spec, which is the pre-launch situation): a token registered via
``omnigent setup``, else an ambient ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` /
``GITHUB_TOKEN``, else ``None``, which leaves the SDK's auto-login on and
lists models as the Copilot CLI's logged-in user.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

# One short-lived CLI subprocess is spawned per probe; bound the whole
# start -> list dance so a wedged CLI can't hang the host tunnel reply.
_LIST_MODELS_TIMEOUT_S = 30.0
_STOP_TIMEOUT_S = 5.0


async def copilot_model_options() -> list[dict[str, Any]]:
    """Resolve the Copilot model catalog for the pre-launch picker.

    :returns: Picker option dicts (``id`` / ``model`` / ``displayName`` /
        ``isDefault``), in backend order.
    :raises Exception: On SDK import, auth, spawn, or timeout failures; the
        caller maps any failure to a failed model-options frame.
    """
    from copilot import CopilotClient  # lazy: optional dependency

    from omnigent.onboarding.copilot_auth import (
        COPILOT_TOKEN_ENV_VARS,
        resolve_copilot_github_token,
    )

    token = resolve_copilot_github_token()
    if token is None:
        token = next(
            (value for var in COPILOT_TOKEN_ENV_VARS if (value := os.environ.get(var))),
            None,
        )
    # The SDK rejects a relative working_directory (same constraint the
    # executor works around), so always hand it an absolute path.
    client = CopilotClient(
        github_token=token,
        working_directory=os.path.abspath(os.getcwd()),
        log_level="error",
    )

    async def _start_and_list() -> list[Any]:
        await client.start()
        return list(await client.list_models())

    try:
        infos = await asyncio.wait_for(_start_and_list(), timeout=_LIST_MODELS_TIMEOUT_S)
    finally:
        # ``start()`` spawns the bundled CLI subprocess even when the call
        # later fails; only ``stop()`` reaps it (same reason the executor
        # stops on its bring-up error path). A reap failure must not mask
        # the real outcome.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.stop(), timeout=_STOP_TIMEOUT_S)

    return [
        {
            "id": info.id,
            "model": info.id,
            "displayName": getattr(info, "name", None) or info.id,
            "isDefault": False,
        }
        for info in infos
        if getattr(info, "id", None)
    ]
