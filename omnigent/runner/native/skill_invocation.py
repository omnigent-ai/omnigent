from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import replace
from pathlib import Path

from websockets.exceptions import WebSocketException

from omnigent.harnesses.claude_native.bridge import (
    _INVOCATION_SETTINGS_FILE,
    _arg_value,
    read_claude_context_state,
)
from omnigent.harnesses.codex_native.app_server import client_for_transport
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState
from omnigent.inner.terminal import TerminalInstance
from omnigent.native.skill_invocation import ClaudeSkillInvocation, CodexSkillInvocation
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.spec.parser import _parse_skill
from omnigent.spec.skill_sources import (
    _enabled_plugin_keys,
    _enabled_plugin_settings_files,
    _read_json,
    skill_source_context_from_env,
)
from omnigent.spec.types import SkillSpec

_COMMAND_COMPONENT = re.compile(r"[a-zA-Z0-9_-]+")
_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_CODEX_APP_SERVER_VERSION = re.compile(r"^[^/]+/(\d+)\.(\d+)\.(\d+)(?:\s|$)")
_CLAUDE_CODE_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\s|$)")


def select_claude_skill(
    selected: SkillSpec, arguments: str, instance: TerminalInstance, bridge_dir: Path
) -> ClaudeSkillInvocation | None:
    if (
        selected.skill_dir is None
        or _CONTROL_CHARACTER.search(arguments)
        or arguments.strip() != arguments
        or "</command-args>" in arguments
    ):
        return None
    status = read_claude_context_state(bridge_dir)
    raw_version = status.get("claude_code_version") if status is not None else None
    version = _CLAUDE_CODE_VERSION.match(raw_version) if isinstance(raw_version, str) else None
    if version is None or tuple(map(int, version.groups())) < (2, 1, 318):
        return None
    args = tuple(instance.args)
    flags = [arg.partition("=")[0] for arg in args]
    if (
        Path(instance.command).name not in {"claude", "claude.exe"}
        or flags.count("--plugin-dir") != 1
        or flags.count("--settings") != 1
        or _arg_value(args, "--settings") != str(bridge_dir / _INVOCATION_SETTINGS_FILE)
        or _arg_value(args, "--setting-sources") not in (None, "")
        or any(
            flag in flags
            for flag in ("--bare", "--safe-mode", "--plugin-url", "--disable-slash-commands", "--")
        )
    ):
        return None
    plugin = _arg_value(args, "--plugin-dir")
    if not plugin:
        return None
    plugin_dir = Path(plugin).resolve()
    skill_dir = selected.skill_dir.resolve()
    if skill_dir.parent != plugin_dir / "skills" or not (skill_dir / "SKILL.md").is_file():
        return None
    name = skill_dir.name
    current_skill = _parse_skill(skill_dir / "SKILL.md")
    manifest = _read_json(plugin_dir / ".claude-plugin" / "plugin.json")
    namespace = manifest.get("name") if manifest else None
    if (
        manifest is None
        or not isinstance(namespace, str)
        or not _COMMAND_COMPONENT.fullmatch(namespace)
        or namespace.casefold() == "anthropic-skills"
        or not _COMMAND_COMPONENT.fullmatch(name)
        or not current_skill.user_invocable
        or current_skill.display_name is not None
        or "skills" in manifest
        or "commands" in manifest
        or (plugin_dir / "commands").exists()
    ):
        return None
    command_name = f"{namespace}:{name}"
    cwd = Path(instance.launch_cwd or os.getcwd())
    roots = []
    for directory in (cwd, *cwd.parents):
        roots.append(directory)
        if (directory / ".git").exists():
            break
    env = {**(os.environ if instance.inherit_env else {}), **instance.env}
    for key in instance.env_unset:
        env.pop(key, None)
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    context = replace(
        skill_source_context_from_env(roots=tuple(roots), harness="claude-native"),
        is_native=True,
        home=Path(env["HOME"]) if env.get("HOME") else Path.home(),
        claude_config_dir=(cwd / config_dir).resolve() if config_dir else None,
    )
    if any(
        key.split("@", 1)[0].casefold() == namespace.casefold()
        for key in _enabled_plugin_keys(context)
    ):
        return None
    settings_paths = [
        *_enabled_plugin_settings_files(context),
        *CLAUDE_CODE_MANAGED_SETTINGS_PATHS,
        bridge_dir / _INVOCATION_SETTINGS_FILE,
    ]
    for path in settings_paths:
        if not path.exists():
            continue
        settings = _read_json(path)
        if settings is None:
            return None
        overrides = settings.get("skillOverrides", {})
        if not isinstance(overrides, dict) or any(
            overrides.get(spelling) == "off" for spelling in (name, command_name)
        ):
            return None
    command = f"/{command_name}"
    return ClaudeSkillInvocation(
        command=f"{command} {arguments}" if arguments else command,
        bridge_dir=str(bridge_dir),
        socket_path=str(instance.socket_path),
    )


async def select_codex_skill(
    selected: SkillSpec, arguments: str, state: CodexNativeBridgeState
) -> CodexSkillInvocation | None:
    if (
        selected.skill_dir is None
        or not state.cwd
        or _CONTROL_CHARACTER.search(arguments)
        or "$" in arguments
        or "skill://" in arguments
    ):
        return None
    path = (selected.skill_dir / "SKILL.md").resolve()
    if not path.is_file() or not _parse_skill(path).user_invocable:
        return None
    client = client_for_transport(state.socket_path, client_name="omnigent-skill-selection")
    try:
        async with asyncio.timeout(4):
            initialized = await client.connect()
            server = initialized.get("result")
            user_agent = server.get("userAgent") if isinstance(server, dict) else None
            version = (
                _CODEX_APP_SERVER_VERSION.match(user_agent)
                if isinstance(user_agent, str)
                else None
            )
            if version is None or tuple(map(int, version.groups())) < (0, 167, 0):
                return None
            response = await client.request(
                "skills/list", {"cwds": [state.cwd], "forceReload": True}
            )
    except (OSError, RuntimeError, ValueError, WebSocketException):
        return None
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.close(), timeout=1)
    result = response.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(entry := data[0], dict):
        return None
    rows = entry.get("skills")
    if entry.get("cwd") != state.cwd or entry.get("errors") != [] or not isinstance(rows, list):
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and isinstance(candidate := row.get("path"), str)
        and Path(candidate).is_absolute()
        and Path(candidate).resolve() == path
    ]
    if len(matches) != 1:
        return None
    skill = matches[0]
    name = skill.get("name")
    if (
        skill.get("enabled") is not True
        or skill.get("pluginId") is not None
        or not isinstance(name, str)
        or not _COMMAND_COMPONENT.fullmatch(name)
        or sum(isinstance(row, dict) and row.get("name") == name for row in rows) != 1
    ):
        return None
    command = f"${name}"
    return CodexSkillInvocation(
        command=f"{command} {arguments}" if arguments else command,
        name=name,
        path=skill["path"],
        thread_id=state.thread_id,
        socket_path=state.socket_path,
    )
