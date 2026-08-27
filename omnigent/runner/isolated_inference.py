"""One-shot, tool-free inference that leaves a session untouched.

Some runner features need a model answer *about* a session without
touching it: background session titles, and ``/btw`` side questions.
Each spawns a throwaway process, asks exactly one question with no
tools, and drops it — the live session's transcript, context, and
harness state never move.

Three mechanisms live here, one per harness family, because the reason
they differ is authentication rather than capability: an SDK harness
answers over its HTTP event stream, while ``claude-native`` and
``codex-native`` shell out to their own CLI in one-shot mode so a
session logged in with a Claude or ChatGPT subscription can answer
without an API key.

Callers supply an :class:`IsolatedPrompt` — what to ask, how to frame
it, and what it may spend. Everything else is shared, so a second
caller cannot drift from the first.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx

from omnigent.debug_logging import runner_primary_session_id

if TYPE_CHECKING:
    from omnigent.spec.types import AgentSpec

_logger = logging.getLogger("omnigent.runner.isolated_inference")


class IsolatedInferenceProcessManager(Protocol):
    """Process-manager operations required to run isolated inference."""

    async def get_client(
        self,
        conversation_id: str,
        harness: str,
        env: dict[str, str] | None = None,
    ) -> httpx.AsyncClient:
        pass

    async def release(
        self,
        conversation_id: str,
        *,
        only_if_idle_cutoff: float | None = None,
    ) -> None:
        pass


class IsolatedInferenceError(RuntimeError):
    """A safe harness failure a runner endpoint can return verbatim."""


@dataclass(frozen=True)
class IsolatedPrompt:
    """
    One question to ask a throwaway process, and what it may spend.

    :param agent_label: Names the synthetic session in logs and cost
        attribution, e.g. ``"side-question"``. Sent as the SDK event's
        ``model`` field, which carries the *agent name*, not an LLM id.
    :param instructions: System prompt for the one-shot session.
    :param prompt: User-role content, already wrapped in whatever
        data-fencing tags the caller wants the model to see. Fencing is
        the caller's job because only it knows which parts are
        untrusted.
    :param max_output_tokens: Output budget. Honored by the SDK path
        (mapped to the executor's ``max_tokens``); the native CLIs
        expose no equivalent flag and ignore it.
    :param timeout_seconds: Wall-clock cap on the whole exchange.
    :param reasoning_effort: Effort level for the single turn.
    """

    agent_label: str
    instructions: str
    prompt: str
    max_output_tokens: int
    timeout_seconds: float
    reasoning_effort: str = "low"


def tool_free_spawn_env(harness: str, spawn_env: dict[str, str]) -> dict[str, str]:
    """
    Strip a spawn environment down to a bare, skill-free harness.

    The synthetic process answers one question; agent bundles, skills,
    and native tools would only add startup cost and give the answer a
    way to reach outside the model.

    :param harness: Canonical harness id, e.g. ``"claude-sdk"``.
    :param spawn_env: The session's resolved spawn environment.
    :returns: A copy with the harness's skills/tools levers cleared.
    """
    env = dict(spawn_env)
    if harness == "codex":
        env.update(
            {
                "HARNESS_CODEX_DISABLE_NATIVE_TOOLS": "1",
                "HARNESS_CODEX_ENABLE_WEB_SEARCH": "0",
                "HARNESS_CODEX_MINIMAL_CONFIG": "1",
                "HARNESS_CODEX_SKILLS_FILTER": json.dumps("none"),
            }
        )
        env.pop("HARNESS_CODEX_AGENT_NAME", None)
        env.pop("HARNESS_CODEX_BUNDLE_DIR", None)
    else:
        env["HARNESS_CLAUDE_SDK_SKILLS_FILTER"] = json.dumps("none")
        env.pop("HARNESS_CLAUDE_SDK_AGENT_NAME", None)
        env.pop("HARNESS_CLAUDE_SDK_BUNDLE_DIR", None)
    return env


async def run_isolated_sdk_inference(
    prompt: IsolatedPrompt,
    *,
    harness: str,
    spawn_env: dict[str, str],
    process_manager: IsolatedInferenceProcessManager,
    error_cls: type[IsolatedInferenceError] = IsolatedInferenceError,
) -> str | None:
    """
    Ask one tool-free question over a synthetic SDK harness session.

    :param prompt: What to ask and what it may spend.
    :param harness: Canonical harness id to spawn, e.g. ``"claude-sdk"``.
    :param spawn_env: Resolved spawn environment; narrowed by
        :func:`tool_free_spawn_env` before use.
    :param process_manager: Supplies and releases the harness process.
    :param error_cls: Raised on harness-side failure. Callers pass their
        own so each endpoint's ``except`` clauses keep naming failures
        in their own vocabulary.
    :returns: The model's text, or ``None`` when it produced none.
    :raises IsolatedInferenceError: Subclass named by ``error_cls``, on
        a non-200 spawn or a ``response.failed`` event.
    :raises TimeoutError: If the exchange outruns the prompt's timeout.
    """
    process_key = uuid.uuid4().hex
    event_body = {
        "type": "message",
        "role": "user",
        "content": prompt.prompt,
        "model": prompt.agent_label,
        "tools": [],
        "instructions": prompt.instructions,
        "reasoning": {"effort": prompt.reasoning_effort},
        "max_output_tokens": prompt.max_output_tokens,
    }
    try:
        client = await process_manager.get_client(
            process_key,
            harness,
            env=tool_free_spawn_env(harness, spawn_env),
        )
        text_parts: list[str] = []
        async with asyncio.timeout(prompt.timeout_seconds):
            async with client.stream(
                "POST",
                f"/v1/sessions/{process_key}/events",
                json=event_body,
                timeout=None,
            ) as response:
                if response.status_code != 200:
                    raise error_cls(f"Harness returned HTTP {response.status_code}.")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        continue
                    event = json.loads(payload)
                    event_type = event.get("type")
                    if event_type == "policy_evaluation.requested":
                        await _auto_verdict(client, process_key, event, error_cls)
                    elif event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str):
                            text_parts.append(delta)
                    elif event_type == "response.failed":
                        detail = _failure_detail(event, prompt.agent_label)
                        _logger.warning(
                            "isolated inference failed label=%s process=%s detail=%s",
                            prompt.agent_label,
                            process_key,
                            detail,
                            extra={"session_id": runner_primary_session_id()},
                        )
                        raise error_cls(detail)
                    elif event_type == "response.completed":
                        break
        text = "".join(text_parts).strip()
        return text or None
    finally:
        await process_manager.release(process_key)


async def _auto_verdict(
    client: httpx.AsyncClient,
    process_key: str,
    event: dict[str, object],
    error_cls: type[IsolatedInferenceError],
) -> None:
    """Answer a policy request: deny tool calls, allow everything else.

    The session declares no tools, so a tool-call phase here means
    something reached for one anyway — denying is what keeps a
    read-only question read-only.
    """
    evaluation_id = event.get("evaluation_id")
    if not isinstance(evaluation_id, str) or not evaluation_id:
        raise error_cls("Harness requested policy evaluation without an id.")
    tool_phase = event.get("phase") == "PHASE_TOOL_CALL"
    await client.post(
        f"/v1/sessions/{process_key}/events",
        json={
            "type": "policy_verdict",
            "evaluation_id": evaluation_id,
            "action": "POLICY_ACTION_DENY" if tool_phase else "POLICY_ACTION_ALLOW",
        },
    )


def _failure_detail(event: dict[str, object], agent_label: str) -> str:
    """Pull a client-safe message out of a ``response.failed`` event."""
    response_payload = event.get("response")
    error_payload = response_payload.get("error") if isinstance(response_payload, dict) else None
    error_message = error_payload.get("message") if isinstance(error_payload, dict) else None
    if isinstance(error_message, str) and error_message.strip():
        return error_message.strip()
    return f"Harness {agent_label} inference failed."


async def run_isolated_claude_print(
    prompt: IsolatedPrompt,
    *,
    spawn_env: dict[str, str],
    cwd: Path | None,
    model_override: str | None,
) -> str | None:
    """
    Ask one tool-free question through isolated Claude Code print mode.

    Used for ``claude-native`` sessions, whose credentials may be a
    Claude subscription that no SDK harness can borrow. Spawned with
    ``--tools ""`` and ``--no-session-persistence``: no tools, and
    nothing written back to the user's Claude Code session store.

    :param prompt: What to ask and what it may spend.
    :param spawn_env: Resolved spawn environment; read for the model.
    :param cwd: Working directory for the process, or ``None``.
    :param model_override: Per-request model, if the caller has one.
    :returns: The process's stdout, or ``None`` when it failed.
    :raises TimeoutError: If the process outruns the prompt's timeout.
    """
    from omnigent.claude_launcher import resolve_claude_launch
    from omnigent.claude_native import (
        build_native_claude_terminal_env,
        resolve_native_claude_config,
    )
    from omnigent.runner.native.orchestration import _claude_terminal_env_unset

    try:
        claude_config = resolve_native_claude_config(spec=None)
    except Exception:  # noqa: BLE001 - match the native terminal's auth fallback
        _logger.warning(
            "isolated Claude Code inference could not resolve provider config; "
            "falling back to Claude Code's native login",
            exc_info=True,
            extra={"session_id": runner_primary_session_id()},
        )
        claude_config = None
    effective_model = (
        spawn_env.get("HARNESS_CLAUDE_SDK_MODEL")
        or model_override
        or (claude_config.model if claude_config is not None else None)
    )
    args = [
        "--safe-mode",
        "--system-prompt",
        prompt.instructions,
        "-p",
        prompt.prompt,
        "--tools",
        "",
        "--output-format",
        "text",
        "--no-session-persistence",
        "--effort",
        prompt.reasoning_effort,
    ]
    if effective_model:
        args.extend(("--model", effective_model))
    if claude_config is not None and claude_config.api_key_helper:
        args.extend(("--settings", json.dumps({"apiKeyHelper": claude_config.api_key_helper})))

    command, launch_args = resolve_claude_launch("claude", args)
    env = dict(os.environ)
    env.update(build_native_claude_terminal_env(claude_config))
    for name in _claude_terminal_env_unset(claude_config):
        env.pop(name, None)

    process = await asyncio.create_subprocess_exec(
        command,
        *launch_args,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(prompt.timeout_seconds):
            stdout, stderr = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        _logger.warning(
            "isolated Claude Code inference failed label=%s returncode=%s detail=%s",
            prompt.agent_label,
            process.returncode,
            detail[-1000:],
            extra={"session_id": runner_primary_session_id()},
        )
        return None
    return stdout.decode(errors="replace").strip()


# Codex exec has no system-prompt flag, so the instructions ride at the
# head of the single prompt argument instead.
_CODEX_TOOL_FREE_OVERRIDES = (
    'approval_policy="never"',
    "features.unified_exec=false",
    "features.shell_tool=false",
    'web_search="disabled"',
    "features.apps=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.image_generation=false",
    "features.multi_agent=false",
    "features.tool_search=false",
)


async def run_isolated_codex_exec(
    prompt: IsolatedPrompt,
    *,
    model_override: str | None,
    session_spec: AgentSpec | None,
    temp_dir_prefix: str = "omnigent-codex-isolated-",
) -> str | None:
    """
    Ask one tool-free question through an isolated native Codex exec.

    Used for ``codex-native`` sessions, for the same subscription-auth
    reason as :func:`run_isolated_claude_print`. Runs ``--ephemeral``
    in a read-only sandbox rooted at a throwaway workspace, with every
    tool feature switched off by config override.

    :param prompt: What to ask and what it may spend.
    :param model_override: Per-request model, if the caller has one.
    :param session_spec: Session's agent spec, threaded through so the
        exec honors spec-level auth.
    :param temp_dir_prefix: Prefix for the throwaway ``CODEX_HOME`` and
        workspace, so a stray directory names its origin.
    :returns: The model's last message, or ``None`` when it failed or
        wrote nothing.
    :raises TimeoutError: If the process outruns the prompt's timeout.
    """
    from omnigent.codex_native_app_server import (
        build_codex_native_server,
        resolve_native_codex_launch,
    )
    from omnigent.inner import _proc
    from omnigent.inner.codex_executor import (
        _codex_home_config_source_from_env,
        _populate_codex_home_config,
        materialize_codex_provider_config,
    )
    from omnigent.runner.native.orchestration import _codex_native_model_from_spec

    model = model_override or _codex_native_model_from_spec(session_spec)
    launch = resolve_native_codex_launch(model=model, spec=session_spec)
    with tempfile.TemporaryDirectory(prefix=temp_dir_prefix) as temp_dir:
        temp_root = Path(temp_dir)
        codex_home = temp_root / "codex-home"
        workdir = temp_root / "workspace"
        codex_home.mkdir(mode=0o700)
        workdir.mkdir()
        _populate_codex_home_config(
            codex_home,
            _codex_home_config_source_from_env(),
            minimal_config=True,
        )
        native_server = build_codex_native_server(
            socket_path=temp_root / "unused.sock",
            codex_home=codex_home,
            cwd=workdir,
            model=launch.model,
            profile=launch.profile,
            bridge_dir=temp_root / "bridge",
            extra_config_overrides=launch.config_overrides,
        )
        native_server.config_overrides = materialize_codex_provider_config(
            codex_home,
            native_server.config_overrides,
        )
        output_path = temp_root / "answer.txt"
        args = [
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(output_path),
            "--color",
            "never",
        ]
        for override in native_server.config_overrides:
            args.extend(("--config", override))
        for override in _CODEX_TOOL_FREE_OVERRIDES:
            args.extend(("--config", override))
        if launch.model:
            args.extend(("--model", launch.model))
        args.append(f"{prompt.instructions} Do not use tools.\n{prompt.prompt}")
        env = {**native_server.env, "CODEX_HOME": str(codex_home)}
        process = await asyncio.create_subprocess_exec(
            native_server.codex_path,
            *args,
            cwd=str(workdir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_proc.spawn_kwargs(),
        )
        try:
            async with asyncio.timeout(prompt.timeout_seconds):
                _stdout, stderr = await process.communicate()
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                _proc.kill_tree(process)
            with contextlib.suppress(Exception):
                await process.wait()
            raise

        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            _logger.warning(
                "isolated native Codex inference failed label=%s returncode=%s detail=%s",
                prompt.agent_label,
                process.returncode,
                detail[-1000:],
                extra={"session_id": runner_primary_session_id()},
            )
            return None
        if not output_path.is_file():
            return None
        return output_path.read_text(errors="replace").strip()
