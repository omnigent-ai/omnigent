"""Read-only host launch settings; argument and environment values stay local."""

from __future__ import annotations

import getopt
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent._platform import resolve_cli_binary
from omnigent.config import load_global_config
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_startup_config import resolve_harness_config

SUPPORTED_HARNESSES = {"claude-native", "codex-native"}


class HarnessStartup(BaseModel):
    """The only launch metadata allowed across the host tunnel and HTTP API."""

    model_config = ConfigDict(strict=True)

    command: str
    resolved_path: str | None
    command_source: Literal["env", "config", "default"]
    arg_count: int = Field(ge=0)


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe host defaults for web launches; workspace overrides may differ."""
    from omnigent.host.connect import _build_runner_env

    harness = canonicalize_harness(harness) or harness
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError("launch settings are not reported for this harness")
    _, overrides = resolve_harness_config(load_global_config())
    entry = overrides.get(harness, {})
    env = _build_runner_env(
        os.environ, server_url="", runner_id="", binding_token="", workspace="", parent_pid=0
    )
    name = harness.removesuffix("-native")
    path = env.get("PATH", os.defpath)
    override = env.get(f"OMNIGENT_{name.upper()}_PATH", "").strip()
    command = entry.get("command", name)
    source: Literal["env", "config", "default"] = "config" if "command" in entry else "default"
    # Claude uses env before config; Codex uses config before a resolvable env override.
    if override and (
        harness == "claude-native"
        or (
            "command" not in entry
            and (
                shutil.which(override, path=path)
                or (os.path.isfile(override) and os.access(override, os.X_OK))
            )
        )
    ):
        command, source = override, "env"
    args = entry.get("args", [])
    unwrapped = _unwrap_env(command, args, path)
    if unwrapped is not None:
        command, args, path = unwrapped
        # env searches only its effective PATH, never our extra install directories.
        resolved = shutil.which(command, path=path)
    else:
        resolved = resolve_cli_binary(command, which=lambda cmd: shutil.which(cmd, path=path))
    return HarnessStartup(
        command=command, resolved_path=resolved, command_source=source, arg_count=len(args)
    )


def _unwrap_env(command: str, args: list[str], path: str) -> tuple[str, list[str], str] | None:
    """Unwrap env options and assignments without exporting their values."""
    if Path(command).name != "env":
        return None
    try:
        options, remaining = getopt.getopt(args, "iu:", ["ignore-environment", "unset="])
    except getopt.GetoptError:
        return None
    if remaining[:1] == ["-"]:
        return None  # env's legacy -i spelling; keep this wrapper opaque.
    for option, value in options:
        if option in ("-i", "--ignore-environment") or value == "PATH":
            path = os.defpath
    for index, arg in enumerate(remaining):
        key, separator, value = arg.partition("=")
        if not separator:
            return arg, remaining[index + 1 :], path
        if not key:
            return None
        if key == "PATH":
            path = value
    return None
