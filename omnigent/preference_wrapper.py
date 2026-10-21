"""One-use preference handoff from a local launcher to its CLI process."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import Field

from omnigent.preference_connection import account_context
from omnigent.preference_startup import PreferenceLaunch, prepare_server_preferences
from omnigent.preference_sync import ProtocolModel

PREFERENCE_HANDOFF_FD_ENV = "OMNIGENT_PREFERENCE_HANDOFF_FD"
MAX_HANDOFF_BYTES = 2 * 1024 * 1024


class PreparedPreferences(ProtocolModel):
    protocol_version: Literal[1] = 1
    server_url: str = Field(max_length=8192)
    account_context: str = Field(min_length=64, max_length=64)
    launch: PreferenceLaunch | None
    harness: str | None = None


async def prepare_wrapper_preferences(server_url: str) -> PreparedPreferences:
    """Refresh once before local harness preparation, without selecting credentials."""
    from omnigent.config import load_global_config, load_local_config
    from omnigent.harness_startup_config import resolve_harness_config

    server_url = server_url.rstrip("/")
    context = account_context(server_url)
    launch = await prepare_server_preferences(server_url)
    if context != account_context(server_url):
        raise ValueError("Login changed while preparing preferences; retry the launch")
    harness = None
    if launch is not None:
        config = launch.resolve(load_global_config(), load_local_config()).config
        harness, _ = resolve_harness_config(config)
    return PreparedPreferences(
        server_url=server_url, account_context=context, launch=launch, harness=harness
    )


def consume_wrapper_preferences(server_url: str) -> PreparedPreferences | None:
    """Consume an inherited, unlinked file descriptor; never propagate it to runners."""
    value = os.environ.pop(PREFERENCE_HANDOFF_FD_ENV, None)
    if value is None:
        return None
    descriptor = int(value)
    if descriptor < 3:
        raise ValueError("Preference handoff must not use a standard stream")
    with os.fdopen(descriptor, "rb") as handoff:
        payload = handoff.read(MAX_HANDOFF_BYTES + 1)
    if len(payload) > MAX_HANDOFF_BYTES:
        raise ValueError("Preference handoff exceeds the size limit")
    prepared = PreparedPreferences.model_validate_json(payload)
    if prepared.server_url != server_url.rstrip("/"):
        raise ValueError("Preference handoff belongs to another server")
    if prepared.account_context != account_context(server_url):
        raise ValueError("Preference login changed after preparation; retry the launch")
    return prepared
