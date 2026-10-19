"""Session routes that start and stop chat code runs on the session's runner.

The owner runs a code block from an assistant message. Checks run in a fixed
order so each refusal is predictable, policy is evaluated as the owner's own
``sys_os_shell`` call, and the runner executes the run in a dedicated sandbox
helper. The runner later posts the ``finished`` item itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from omnigent.debug_logging import add_audit_attrs
from omnigent.entities import CodeRunData, Conversation, MessageData, NewConversationItem
from omnigent.entities.conversation import (
    CODE_RUN_START_REFUSALS,
    CodeRunOutputSegment,
    code_run_source_id,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.types import PolicyResult
from omnigent.runtime import get_policy_store
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.feature_flags import Feature
from omnigent.server.routes._auth_helpers import attribution_user
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._sessions.helpers import (
    _build_evaluation_context,
    _external_item_stable_id,
    _get_runner_client_for_resource_access,
    _message_text,
    _publish_code_run_item,
)
from omnigent.server.schemas import SessionCodeRunSnapshotEvent
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import ConversationNotFoundError
from omnigent.stores.permission_store import PermissionStore

CODE_RUN_MAX_CODE_BYTES = 64 * 1024
CODE_RUN_DEFAULT_TIMEOUT_S = 120
CODE_RUN_MAX_TIMEOUT_S = 600
CODE_RUN_MAX_RUNNING = 4
_CODE_RUN_LANGUAGES = frozenset({"shell", "python", "node"})
_PROMPT_PREFIXES = ("$ ", "% ")
# Runner calls here return at once (the run itself is detached on the runner).
_RUNNER_CALL_TIMEOUT_S = 30.0


def derive_code_run_command(language: str, code: str, *, console: bool) -> str:
    """
    Turn a block's raw text into the command that runs.

    Shell blocks lose their leading ``$ `` / ``% `` prompts; a ``console``
    block keeps only its prompt lines, dropping the sample output between
    them. Python and Node source is unchanged.

    :param language: ``"shell"``, ``"python"``, or ``"node"``.
    :param code: The block's raw text.
    :param console: Whether the block is tagged ``console``.
    :returns: The command or source to run.
    """
    if language != "shell":
        return code
    lines: list[str] = []
    for line in code.splitlines():
        prefix = next((p for p in _PROMPT_PREFIXES if line.startswith(p)), None)
        if prefix is not None:
            lines.append(line[len(prefix) :])
        elif not console:
            lines.append(line)
    return "\n".join(lines)


# Fence tags a block can run as, keyed to the run language.
_FENCE_LANGUAGES = {
    "bash": "shell",
    "sh": "shell",
    "shell": "shell",
    "zsh": "shell",
    "console": "shell",
    "python": "python",
    "javascript": "node",
    "node": "node",
}
# Any indent: a fence inside a (nested) list item is indented past three spaces.
_FENCE_OPEN = re.compile(r"^( *)(`{3,}|~{3,})(.*)$")


@dataclass(frozen=True)
class CodeFence:
    """A runnable fenced code block of a message.

    :param language: Run language, ``"shell"``, ``"python"``, or ``"node"``.
    :param console: Whether the fence is tagged ``console``.
    """

    language: str
    console: bool


def _normalize_block_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``(info string, body)`` for each fenced code block in *text*.

    Follows CommonMark fences: a run of at least three backticks or tildes
    opens a block, a run of the same character at least as long closes it,
    and an unclosed block runs to the end of the text. Fences may be indented
    (list items); body lines lose up to the opening fence's indent.
    """
    blocks: list[tuple[str, str]] = []
    lines = _normalize_block_text(text).split("\n")
    index = 0
    while index < len(lines):
        opened = _FENCE_OPEN.match(lines[index])
        index += 1
        if opened is None or (opened.group(2)[0] == "`" and "`" in opened.group(3)):
            continue
        indent, fence, info = len(opened.group(1)), opened.group(2), opened.group(3).strip()
        body: list[str] = []
        while index < len(lines):
            line = lines[index]
            index += 1
            closing = line.strip()
            if closing.startswith(fence) and closing == fence[0] * len(closing):
                break
            body.append(line[min(indent, len(line) - len(line.lstrip(" "))) :])
        blocks.append((info, "\n".join(body)))
    return blocks


def find_code_fence(message_text: str, code: str, language: str) -> CodeFence | None:
    """
    Find the fence of *message_text* whose whole body is *code*, run as *language*.

    Line endings and trailing newlines are normalized on both sides. The
    fence's tag decides the language; an untagged fence runs as shell when
    every non-empty line starts with ``$ ``.

    :returns: The matching fence, or ``None`` when no whole fence body equals
        *code* with that language.
    """
    wanted = _normalize_block_text(code)
    for info, body in _fenced_blocks(message_text):
        if _normalize_block_text(body) != wanted:
            continue
        tag = info.split(maxsplit=1)[0].lower() if info else ""
        if tag:
            fence_language = _FENCE_LANGUAGES.get(tag)
        else:
            lines = [line for line in body.split("\n") if line.strip()]
            prompts_only = bool(lines) and all(line.startswith("$ ") for line in lines)
            fence_language = "shell" if prompts_only else None
        if fence_language == language:
            return CodeFence(language=language, console=tag == "console")
    return None


# ``"started"``, ``"ambiguous"``, or a definitive ``(status, code, reason)`` refusal.
_StartOutcome = Literal["started", "ambiguous"] | tuple[int, str, str]


def _classify_runner_start(response: httpx.Response, run_id: str) -> _StartOutcome:
    """
    Decide what a runner's reply to a forwarded run proves.

    Only the runner route's own structured refusals, which it sends before
    starting a run, prove the run did not start; a 202 echoing the run id
    proves it did. Anything else (a 5xx or other reply a tunnel or proxy may
    have written, an unparseable body) may follow a started run.

    :param response: The runner's reply to ``POST .../code-runs``.
    :param run_id: The forwarded run's id.
    :returns: The outcome.
    """
    try:
        body = response.json()
    except ValueError:
        return "ambiguous"
    if not isinstance(body, dict):
        return "ambiguous"
    if response.status_code == 202:
        return "started" if body.get("run_id") == run_id else "ambiguous"
    code = body.get("code")
    if code is not None and CODE_RUN_START_REFUSALS.get(response.status_code) == code:
        reason = body.get("reason")
        return (
            response.status_code,
            code,
            reason if isinstance(reason, str) and reason else "The runner refused the code run",
        )
    return "ambiguous"


def _invalid(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _refusal(status_code: int, code: str, reason: str | None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": code, "reason": reason or ""})


def _shell_call(command: str, timeout: int) -> dict[str, Any]:
    """The TOOL_CALL payload a run is evaluated as: the owner's ``sys_os_shell``."""
    return {"name": "sys_os_shell", "arguments": {"command": command, "timeout": timeout}}


async def _evaluate_run_policy(
    session_id: str,
    conversation: Conversation,
    agent_store: AgentStore,
    conversation_store: ConversationStore,
    *,
    command: str,
    timeout: int,
    actor: dict[str, str],
) -> tuple[PolicyResult | None, PolicyEngine | None]:
    """
    Evaluate TOOL_CALL policy for a run as the owner's ``sys_os_shell`` call.

    Builds a fresh engine per request, as the native ``PreToolUse`` hook does
    in ``POST /sessions/{id}/policies/evaluate``.

    :returns: The composed result and its engine, or ``(None, None)`` when no
        policy applies.
    """
    from omnigent.server.routes import sessions as _sf

    agent = agent_store.get(conversation.agent_id) if conversation.agent_id else None
    if agent is None:
        return None, None
    loaded = _sf.get_agent_cache().load(
        agent.id, agent.bundle_location, expand_env=agent.operator_authored
    )
    caps = _sf.get_caps()
    if not any_policies_apply(
        spec=loaded.spec,
        conversation_id=session_id,
        default_policies=caps.default_policies,
        policy_store=get_policy_store(),
        phase=Phase.TOOL_CALL,
        tool_name="sys_os_shell",
        conversation=conversation,
    ):
        return None, None
    engine = build_policy_engine(
        spec=loaded.spec,
        conversation_id=session_id,
        conversation_store=conversation_store,
        conversation=conversation,
        expected_agent_id=agent.id,
        default_policies=caps.default_policies,
        policy_store=get_policy_store(),
        server_llm=caps.llm,
        host_connection=(
            caps.policy_llm_connection_factory() if caps.policy_llm_connection_factory else None
        ),
    )
    data = _shell_call(command, timeout)
    ctx = _build_evaluation_context(
        Phase.TOOL_CALL, data, {"type": "PHASE_TOOL_CALL", "data": data}, actor=actor
    )
    return await engine.evaluate(ctx), engine


def _code_block_run_enabled(request: Request) -> bool:
    flags = getattr(request.app.state, "feature_flags", None)
    return flags is not None and flags.enabled(Feature.CODE_BLOCK_RUN)


async def code_run_snapshot_events(
    request: Request, runner_client: httpx.AsyncClient, session_id: str
) -> list[dict[str, Any]]:
    """
    Build one ``session.code_run.snapshot`` per run the runner has going.

    Used by the stream's connect snapshot, so a viewer that subscribes
    mid-run sees the output so far. Empty when the flag is off.

    :param request: The subscribing request (for the feature flags).
    :param runner_client: The session's runner client.
    :param session_id: Session/conversation identifier.
    :returns: Snapshot event dicts.
    """
    if not _code_block_run_enabled(request):
        return []
    response = await runner_client.get(
        f"/v1/sessions/{session_id}/code-runs", params={"status": "running"}
    )
    if response.status_code != 200:
        return []
    return [
        SessionCodeRunSnapshotEvent(
            type="session.code_run.snapshot",
            run_id=run["run_id"],
            offset=run["offset"],
            output=run["output"],
        ).model_dump()
        for run in response.json().get("data", [])
    ]


def register_code_runs_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register ``/sessions/{id}/code-runs`` routes on *router*."""

    def _require_code_block_run(request: Request) -> None:
        if not _code_block_run_enabled(request):
            raise HTTPException(status_code=404, detail="Not Found")

    async def _owner_conversation(
        request: Request, session_id: str
    ) -> tuple[str | None, Conversation]:
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_OWNER, permission_store, conversation_store
        )
        conversation = access.conversation
        if conversation is None:
            conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise _session_not_found()
        return user_id, conversation

    async def _save_code_run_item(
        session_id: str, run_id: str, user_id: str | None, data: CodeRunData
    ) -> None:
        # The id is the one the runner's own post of this phase would get, so a
        # late post of the same item dedupes instead of duplicating it.
        item = NewConversationItem(
            type="code_run",
            response_id=run_id,
            created_by=attribution_user(user_id),
            stable_id=_external_item_stable_id(session_id, code_run_source_id(run_id, data.phase)),
            data=data,
        )
        [persisted] = await asyncio.to_thread(conversation_store.append, session_id, [item])
        if not persisted.deduplicated:
            _publish_code_run_item(session_id, persisted)

    async def _runner_client(session_id: str, conversation: Conversation) -> httpx.AsyncClient:
        client = await _get_runner_client_for_resource_access(
            session_id, conversation=conversation
        )
        if client is None:
            raise HTTPException(status_code=502, detail="no runner available for code runs")
        return client

    @router.post(
        "/sessions/{session_id}/code-runs",
        status_code=202,
        response_model=None,
        # The flag check runs first so a disabled deployment answers 404 to everyone.
        dependencies=[Depends(_require_code_block_run)],
    )
    async def start_code_run(request: Request, session_id: str) -> Any:
        """
        Run a code block from an assistant message on the session's runner.

        Owner-only. Body: ``message_id``, ``block_hash`` (SHA-256 hex of
        ``code``), ``language`` (``shell`` / ``python`` / ``node``), ``code``,
        optional ``timeout`` (seconds, default 120) and ``approved`` (the
        owner confirmed a policy ASK).

        :returns: 202 ``{"run_id"}``; 403 ``policy_denied``, 409
            ``needs_approval`` / ``copy_on_write``, 429 ``too_many_runs``.
        """
        user_id, conversation = await _owner_conversation(request, session_id)
        require_json_content_type(request)
        run_id = f"run_{secrets.token_hex(12)}"
        # Every outcome records these; unknown values stay unset.
        audit: dict[str, object] = {
            "code_run_id": run_id,
            "code_run_policy_decision": "not_evaluated",
            "code_run_approved_by": "",
        }
        try:
            return await _start(request, session_id, user_id, conversation, run_id, audit)
        finally:
            add_audit_attrs(**audit)

    async def _start(
        request: Request,
        session_id: str,
        user_id: str | None,
        conversation: Conversation,
        run_id: str,
        audit: dict[str, object],
    ) -> Any:
        try:
            body = await request.json()
        except ValueError as exc:
            raise _invalid("Request body must be JSON") from exc
        if not isinstance(body, dict):
            raise _invalid("Request body must be a JSON object")

        language = body.get("language")
        if not isinstance(language, str) or language not in _CODE_RUN_LANGUAGES:
            raise _invalid("language must be one of shell, python, node")
        audit["code_run_language"] = language
        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            raise _invalid("code must be a non-empty string")
        if len(code.encode("utf-8")) > CODE_RUN_MAX_CODE_BYTES:
            raise _invalid("code must be at most 64 KiB")
        if body.get("block_hash") != hashlib.sha256(code.encode("utf-8")).hexdigest():
            raise _invalid("block_hash does not match code")

        message_id = body.get("message_id")
        message = (
            await asyncio.to_thread(conversation_store.get_item, session_id, message_id)
            if isinstance(message_id, str) and message_id
            else None
        )
        message_text = (
            _message_text(message.data.content)
            if message is not None
            and isinstance(message.data, MessageData)
            and message.data.role == "assistant"
            else None
        )
        fence = find_code_fence(message_text, code, language) if message_text else None
        if fence is None:
            raise _invalid("code is not a whole runnable code block of the assistant message")

        runner_client = await _runner_client(session_id, conversation)
        try:
            listing = await runner_client.get(
                f"/v1/sessions/{session_id}/code-runs",
                params={"status": "running"},
                timeout=_RUNNER_CALL_TIMEOUT_S,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(status_code=502, detail="runner code-run listing failed") from exc
        if listing.status_code >= 400:
            raise HTTPException(status_code=502, detail="runner code-run listing failed")
        runner_state = listing.json()
        if runner_state.get("copy_on_write"):
            return _refusal(
                409,
                "copy_on_write",
                "Code runs are unavailable in sessions whose sandbox uses copy-on-write roots",
            )

        timeout = body.get("timeout", CODE_RUN_DEFAULT_TIMEOUT_S)
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= CODE_RUN_MAX_TIMEOUT_S
        ):
            raise _invalid("timeout must be an integer from 1 to 600 seconds")
        if len(runner_state.get("data") or []) >= CODE_RUN_MAX_RUNNING:
            return _refusal(429, "too_many_runs", "At most 4 code runs per session")

        command = derive_code_run_command(
            language, _normalize_block_text(code), console=fence.console
        )
        audit["code_run_command_length"] = len(command)
        actor = {"initiated_by": "user"}
        if user_id is not None:
            actor = {"run_as": user_id, **actor}
        result, engine = await _evaluate_run_policy(
            session_id,
            conversation,
            agent_store,
            conversation_store,
            command=command,
            timeout=timeout,
            actor=actor,
        )
        action = result.action if result is not None else PolicyAction.ALLOW
        audit["code_run_policy_decision"] = action.value
        approved_by: str | None = None
        # Labels a non-ASK verdict wrote land now, as on the native hook route.
        if result is not None and action != PolicyAction.ASK and result.set_labels:
            if engine is not None:
                engine.apply_label_writes(result.set_labels)
        if result is not None and action == PolicyAction.DENY:
            return _refusal(403, "policy_denied", result.reason)
        # The native shell hook enforces verdicts but never applies a policy's
        # rewritten arguments, so a run whose command a policy changed is refused.
        if (
            result is not None
            and result.data is not None
            and result.data != _shell_call(command, timeout)
        ):
            return _refusal(
                403,
                "policy_modified_unsupported",
                "A policy changed this command; rewritten code runs are not supported",
            )
        if result is not None and action == PolicyAction.ASK:
            if body.get("approved") is not True:
                return _refusal(409, "needs_approval", result.reason)
            approved_by = user_id or "owner"
            audit["code_run_approved_by"] = approved_by
            # Side effects an ASKing policy withheld land once the owner approves.
            if engine is not None and result.set_labels:
                engine.apply_label_writes(result.set_labels)
            if engine is not None and result.state_updates:
                with contextlib.suppress(ConversationNotFoundError):
                    engine.apply_state_updates(result.state_updates)

        # Saved before the runner starts, so ``finished`` can never land first.
        await _save_code_run_item(
            session_id,
            run_id,
            user_id,
            CodeRunData(
                run_id=run_id,
                phase="started",
                message_id=message_id,
                block_hash=body["block_hash"],
                language=language,
                command=command,
                timeout=timeout,
                approved_by=approved_by,
                started_at=int(time.time()),
            ),
        )
        try:
            started = await runner_client.post(
                f"/v1/sessions/{session_id}/code-runs",
                json={
                    "run_id": run_id,
                    "language": language,
                    "command": command,
                    "timeout": timeout,
                },
                timeout=_RUNNER_CALL_TIMEOUT_S,
            )
        except (httpx.HTTPError, ConnectionError):
            outcome: _StartOutcome = "ambiguous"
        else:
            outcome = _classify_runner_start(started, run_id)
        if outcome == "ambiguous":
            # The runner may have started the run before the answer was lost or
            # replaced (a timeout, or an error a tunnel or proxy wrote), so save
            # no result of our own: stop it, and its real ``finished`` (or, if it
            # never started, the interrupted state) follows.
            with contextlib.suppress(httpx.HTTPError, ConnectionError):
                await runner_client.post(
                    f"/v1/sessions/{session_id}/code-runs/{run_id}/stop",
                    json={},
                    timeout=_RUNNER_CALL_TIMEOUT_S,
                )
            return _refusal(502, "runner_unavailable", "The runner did not confirm the run")
        if outcome != "started":
            status_code, code, reason = outcome
            await _save_code_run_item(
                session_id,
                run_id,
                user_id,
                CodeRunData(
                    run_id=run_id,
                    phase="finished",
                    status="failed",
                    exit_code=None,
                    duration_ms=0,
                    output=[CodeRunOutputSegment(stream="stderr", text=reason + "\n")],
                    truncated=False,
                ),
            )
            return _refusal(status_code, code, reason)
        return JSONResponse(status_code=202, content={"run_id": run_id})

    @router.post(
        "/sessions/{session_id}/code-runs/{run_id}/stop",
        response_model=None,
        dependencies=[Depends(_require_code_block_run)],
    )
    async def stop_code_run(request: Request, session_id: str, run_id: str) -> Any:
        """
        Stop a running code run. Owner-only.

        Proxied to the runner like a terminal close; a finished or unknown
        run returns 200 and nothing happens.

        :returns: ``{"stopped": bool}``.
        """
        _, conversation = await _owner_conversation(request, session_id)
        require_json_content_type(request)
        add_audit_attrs(code_run_id=run_id)
        runner_client = await _runner_client(session_id, conversation)
        try:
            response = await runner_client.post(
                f"/v1/sessions/{session_id}/code-runs/{run_id}/stop",
                json={},
                timeout=_RUNNER_CALL_TIMEOUT_S,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(status_code=502, detail="runner code-run stop failed") from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="runner code-run stop failed")
        stopped = bool(response.json().get("stopped"))
        add_audit_attrs(code_run_stopped=stopped)
        return {"stopped": stopped}
