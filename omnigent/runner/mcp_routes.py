"""Runner routes for MCP tool execution, summarization, and elicitation replies."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from omnigent.llms.client import Client as LLMClient
    from omnigent.runner.mcp_manager import RunnerMcpManager
    from omnigent.runtime.filesystem_registry import FilesystemRegistry
    from omnigent.terminals.registry import TerminalRegistry

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from omnigent.debug_logging import runner_primary_session_id
from omnigent.errors import OmnigentError
from omnigent.llms.summarize import (
    build_summarization_input,
    build_summarization_prompt,
    extract_summary_text,
)
from omnigent.runner.app_support import (
    SpecResolver,
    _BodyRequest,
    _client_safe_error_detail,
    _SpecEntry,
)
from omnigent.runner.mcp_execution_registry import (
    McpExecutionConflict,
    McpExecutionRegistry,
    McpExecutionResult,
)
from omnigent.runner.native import (
    _forward_harness_response,
    _resolved_workdir_for_spec,
    _unwrap_resolved_spec,
)
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError
from omnigent.spec.types import AgentSpec
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger("omnigent.runner.app")


# Lazy singleton LLM client for the runner process. Created on first use so
# the runner does not import llms at startup (imports are expensive and the
# /v1/summarize endpoint is optional). The concrete type is imported only
# during type checking to keep the runtime import graph lazy.
_runner_llm_client: LLMClient | None = None


def _get_runner_llm_client() -> LLMClient:
    """Return the runner-process LLM client, creating it on first use.

    The client is constructed from the runner process's environment
    variables, which include the Databricks credentials set up by the
    runner entry point. This is intentionally separate from the AP
    server's ``_get_llm_client()`` — the runner may have different
    (or more) credentials than the Omnigent server.

    :returns: A ``llms.Client`` instance bound to this runner process.
    """
    global _runner_llm_client
    if _runner_llm_client is None:
        from omnigent.llms import Client as LLMClient

        _runner_llm_client = LLMClient()
    return _runner_llm_client


def register_mcp_routes(
    app: FastAPI,
    *,
    _publish_event: Callable[[str, Mapping[str, object]], None],
    _recover_undrained_subagent_results: Callable[[str], Coroutine[Any, Any, None]],
    _resolve_conversation_id: Callable[[str], Coroutine[Any, Any, str | None]],
    _resolve_session_agent_spec_or_none: Callable[[str], Coroutine[Any, Any, AgentSpec | None]],
    _resolve_session_spec_entry: Callable[[str], Coroutine[Any, Any, _SpecEntry | None]],
    _session_agent_ids: dict[str, str],
    _session_async_tasks: dict[str, dict[str, tuple[asyncio.Task[str], asyncio.Event]]],
    _session_harness_name: Callable[[str], str | None],
    _session_inboxes: dict[str, asyncio.Queue[_JsonObject]],
    _session_runtime_cwd: Callable[[str], Coroutine[Any, Any, Path | None]],
    _session_spec_cache: dict[str, _SpecEntry | None],
    filesystem_registry: FilesystemRegistry | None,
    mcp_execution_registry: McpExecutionRegistry,
    mcp_manager: RunnerMcpManager | None,
    process_manager: HarnessProcessManager | None,
    resource_registry: SessionResourceRegistry,
    runner_workspace: Path | None,
    server_client: httpx.AsyncClient,
    spec_resolver: SpecResolver | None,
    terminal_registry: TerminalRegistry | None,
) -> None:
    """Register ``/mcp/execute``, ``/v1/summarize``, and ``/v1/elicitations`` on *app*.

    The keyword arguments are the runner app's shared session state and helpers.
    """

    @app.post("/v1/sessions/{session_id}/mcp/execute")
    async def mcp_execute(session_id: str, request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=400,
                content={"error": {"code": -32700, "message": "Parse error: invalid JSON"}},
            )
        method: str = body.get("method") or ""
        params: _JsonObject = body.get("params") or {}

        raw_operation = body.get("_omnigent_operation")
        if method == "tools/call" and raw_operation is not None:
            operation = raw_operation if isinstance(raw_operation, dict) else {}
            operation_id = operation.get("id")
            operation_step = operation.get("step")
            if not (
                isinstance(operation_id, str)
                and 1 <= len(operation_id) <= 128
                and isinstance(operation_step, str)
                and 1 <= len(operation_step) <= 64
            ):
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": "Invalid runner MCP operation metadata",
                        }
                    },
                )

            nested_body = cast("_JsonObject", {**body, "_omnigent_operation": None})
            nested_body.pop("_omnigent_operation")

            async def _run_retained_mcp_execution() -> McpExecutionResult:
                nested_response = await mcp_execute(
                    session_id,
                    cast("Request", _BodyRequest(nested_body)),
                )
                nested_content = json.loads(bytes(nested_response.body))
                if not isinstance(nested_content, dict):
                    raise RuntimeError("Runner MCP execution returned a non-object response")
                return McpExecutionResult(
                    status_code=nested_response.status_code,
                    content=cast("_JsonObject", nested_content),
                )

            try:
                retained = await mcp_execution_registry.execute(
                    session_id=session_id,
                    operation_id=operation_id,
                    step=operation_step,
                    params=cast("_JsonObject", {"method": method, "params": params}),
                    run=_run_retained_mcp_execution,
                )
            except McpExecutionConflict as exc:
                return JSONResponse(
                    status_code=200,
                    content={"error": {"code": -32000, "message": str(exc)}},
                )
            return JSONResponse(status_code=retained.status_code, content=retained.content)

        if method == "tools/list":
            if mcp_manager is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": -32000,
                            "message": "Runner MCP manager not configured",
                        }
                    },
                )
            spec_entry = _session_spec_cache.get(session_id)
            spec = _unwrap_resolved_spec(spec_entry)
            if spec is None and spec_resolver is not None:
                spec = await _resolve_session_agent_spec_or_none(session_id)
            if spec is None:
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": f"No spec available for session {session_id!r}",
                        }
                    },
                )
            try:
                result = await mcp_manager.schemas_for(spec)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(
                    status_code=200,
                    content={
                        "error": {
                            "code": -32000,
                            "message": _client_safe_error_detail(exc, context="MCP tool dispatch"),
                        }
                    },
                )
            return JSONResponse(
                content={
                    "result": {
                        "schemas": result.schemas,
                        "tool_names": list(result.tool_names),
                        "failures": result.failures,
                    }
                }
            )

        if method == "tools/call":
            import json as _json

            from omnigent.runner.tool_dispatch import execute_tool

            tool_name = cast(str, params.get("name") or "")
            arguments = cast(_JsonObject, params.get("arguments") or {})
            input_responses = cast(_JsonObject | None, params.get("inputResponses"))
            request_state = cast(str | None, params.get("requestState"))
            if not tool_name:
                return JSONResponse(
                    status_code=200,
                    content={"error": {"code": -32000, "message": "Missing tool name"}},
                )

            if tool_name == "sys_read_inbox":
                # A scan that failed at initialization must not leave the
                # drain reporting an empty inbox; this is a no-op once done.
                await _recover_undrained_subagent_results(session_id)

            if "__" in tool_name:
                if mcp_manager is None:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": {
                                "code": -32000,
                                "message": "Runner MCP manager not configured",
                            }
                        },
                    )
                spec_entry = _session_spec_cache.get(session_id)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    spec = await _resolve_session_agent_spec_or_none(session_id)
                if spec is None:
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": f"No spec available for session {session_id!r}",
                            }
                        },
                    )
                from omnigent.tools.mcp import McpElicitationRequired

                try:
                    if input_responses is not None:
                        route = mcp_manager._resolve_tool_route(spec, tool_name)
                        if route is None:
                            raise RuntimeError(
                                f"runner has no live MCP serving tool {tool_name!r}"
                            )
                        owning, bare_tool = route
                        if owning.connection is None:
                            raise RuntimeError(
                                f"runner has no live MCP serving tool {tool_name!r}"
                            )
                        output = await owning.connection.call_tool_with_elicitation(
                            bare_tool,
                            arguments,
                            input_responses=input_responses,
                            request_state=request_state,
                        )
                    else:
                        output = await mcp_manager.call_tool(
                            spec,
                            tool_name,
                            arguments,
                            session_id=session_id,
                        )
                except McpElicitationRequired as elicit:
                    return JSONResponse(
                        content={
                            "result": {
                                "input_required": {
                                    "inputRequests": elicit.input_requests,
                                    "requestState": elicit.request_state,
                                },
                            },
                        },
                    )
                except Exception as exc:
                    _logger.exception(
                        "MCP tool dispatch failed for %s",
                        tool_name,
                        extra={"session_id": session_id},
                    )
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            else:
                spec_entry = _session_spec_cache.get(session_id)
                spec_workdir = _resolved_workdir_for_spec(spec_entry, runner_workspace)
                spec = _unwrap_resolved_spec(spec_entry)
                if spec is None and spec_resolver is not None:
                    try:
                        resolved_entry = await _resolve_session_spec_entry(session_id)
                        spec_workdir = _resolved_workdir_for_spec(resolved_entry, runner_workspace)
                        spec = _unwrap_resolved_spec(resolved_entry)
                    except (OmnigentError, httpx.HTTPError, RuntimeError):
                        pass
                _agent_id_local = _session_agent_ids.get(session_id)
                try:
                    dispatch_workspace = await _session_runtime_cwd(session_id)
                    output = await execute_tool(
                        tool_name=tool_name,
                        arguments=_json.dumps(arguments),
                        server_client=server_client,
                        terminal_registry=terminal_registry,
                        resource_registry=resource_registry,
                        agent_spec=spec,
                        conversation_id=session_id,
                        task_id=session_id,
                        agent_id=_agent_id_local,
                        agent_name=getattr(spec, "name", None),
                        runner_workspace=dispatch_workspace,
                        local_tool_workdir=spec_workdir,
                        mcp_manager=None,
                        session_inbox=_session_inboxes.get(session_id),
                        session_async_tasks=_session_async_tasks.get(session_id),
                        harness_client=None,
                        publish_event=_publish_event,
                        filesystem_registry=filesystem_registry,
                        effective_harness=_session_harness_name(session_id),
                    )
                except Exception as exc:
                    _logger.exception(
                        "MCP tool dispatch failed for %s",
                        tool_name,
                        extra={"session_id": session_id},
                    )
                    return JSONResponse(
                        status_code=200,
                        content={
                            "error": {
                                "code": -32000,
                                "message": _client_safe_error_detail(
                                    exc, context="MCP tool dispatch"
                                ),
                            }
                        },
                    )
            return JSONResponse(content={"result": {"output": output}})

        return JSONResponse(
            status_code=200,
            content={"error": {"code": -32601, "message": f"Method not found: {method!r}"}},
        )

    def _resolve_summarize_connection(
        session_id: str,
        model: str,
    ) -> dict[str, str] | None:
        from omnigent.spec.types import ApiKeyAuth, DatabricksAuth, ProviderAuth

        spec_entry = _session_spec_cache.get(session_id)
        if spec_entry is None:
            return None
        spec = spec_entry.spec if hasattr(spec_entry, "spec") else spec_entry
        if spec is None:
            return None

        auth = getattr(spec.executor, "auth", None)

        if isinstance(auth, ProviderAuth):
            return _resolve_provider_connection(auth.name, model)

        if isinstance(auth, DatabricksAuth):
            return _resolve_databricks_connection(auth.profile, session_id)

        if isinstance(auth, ApiKeyAuth):
            conn: dict[str, str] = {"api_key": auth.api_key}
            if auth.base_url:
                conn["base_url"] = auth.base_url
            return conn

        _spec_has_legacy_profile = bool(
            spec.executor.profile or (spec.executor.config or {}).get("profile")
        )
        if auth is None and not _spec_has_legacy_profile:
            from omnigent.runtime.workflow import _load_global_auth

            global_auth = _load_global_auth()
            if isinstance(global_auth, DatabricksAuth):
                return _resolve_databricks_connection(global_auth.profile, session_id)
            if isinstance(global_auth, ApiKeyAuth):
                conn = {"api_key": global_auth.api_key}
                if global_auth.base_url:
                    conn["base_url"] = global_auth.base_url
                return conn

        if model.startswith(("databricks/", "databricks-")):
            _db_profile = (
                spec.executor.profile or (spec.executor.config or {}).get("profile") or "DEFAULT"
            )
            return _resolve_databricks_connection(_db_profile, session_id)

        return None

    def _resolve_provider_connection(
        provider_name: str,
        model: str = "",
    ) -> dict[str, str] | None:
        try:
            from omnigent.onboarding.detected import effective_config_with_detected
            from omnigent.onboarding.provider_config import (
                load_config,
                load_providers,
            )

            config = load_config()
            providers = load_providers(effective_config_with_detected(config))
            entry = providers.get(provider_name)
            if entry is None:
                return None
            if entry.kind == "databricks" and entry.profile:
                return _resolve_databricks_connection(entry.profile, provider_name)
            _is_anthropic = model.startswith(("anthropic/", "claude"))
            _preferred = "anthropic" if _is_anthropic else "openai"
            _fallback = "openai" if _is_anthropic else "anthropic"
            family = entry.family(_preferred) or entry.family(_fallback)
            if family is None:
                return None
            conn: dict[str, str] = {}
            if family.api_key:
                conn["api_key"] = family.api_key
            if family.base_url:
                conn["base_url"] = family.base_url
            return conn or None
        except Exception:  # noqa: BLE001
            _logger.warning(
                "/v1/summarize: failed to resolve provider %r",
                provider_name,
                exc_info=True,
                extra={"session_id": runner_primary_session_id()},
            )
            return None

    def _resolve_databricks_connection(
        profile: str,
        context: str,
    ) -> dict[str, str] | None:
        from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

        try:
            creds = resolve_databricks_workspace(profile)
        except OSError:
            _logger.warning(
                "/v1/summarize: failed to resolve Databricks profile %r (context=%s)",
                profile,
                context,
                exc_info=True,
                extra={"session_id": runner_primary_session_id()},
            )
            return None
        return {
            "base_url": creds.host.rstrip("/") + "/serving-endpoints",
            "api_key": creds.token,
        }

    @app.post("/v1/summarize")
    async def summarize(request: Request) -> JSONResponse:
        body = await request.json()
        messages = body.get("messages")
        model = body.get("model")
        if not isinstance(messages, list) or not model:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_input",
                        "message": "'messages' (list) and 'model' (str) are required",
                    }
                },
            )
        connection: dict[str, str] | None = body.get("connection") or None
        if connection is None:
            session_id: str | None = body.get("session_id")
            if session_id is not None:
                connection = _resolve_summarize_connection(
                    session_id,
                    model,
                )
        llm_client = _get_runner_llm_client()
        resp = await llm_client.responses.create(
            model=model,
            input=build_summarization_input(messages),
            instructions=build_summarization_prompt(messages),
            tools=[],
            connection_params=connection,
        )
        summary_text = extract_summary_text(resp)
        import tiktoken

        bare = model.split("/", 1)[-1] if "/" in model else model
        try:
            enc = tiktoken.encoding_for_model(bare)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(summary_text))
        return JSONResponse(content={"text": summary_text, "token_count": token_count})

    @app.post("/v1/elicitations/{elicitation_id}")
    async def elicitation(elicitation_id: str, request: Request) -> Response:
        if process_manager is None:
            return JSONResponse(
                status_code=501,
                content={"error": "not_implemented", "detail": "Runner not configured"},
            )
        body = await request.json()
        response_id = body.get("response_id")
        if not response_id:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_request",
                    "detail": "response_id required in elicitation body",
                },
            )
        conv_id = await _resolve_conversation_id(response_id)
        if conv_id is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "detail": f"Cannot resolve response {response_id}"},
            )
        try:
            client = await process_manager.get_client(conv_id, "any")
        except NoLiveHarnessError:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "no_live_harness",
                    "detail": "no harness subprocess is running for this conversation",
                },
            )
        try:
            event_body = {
                "type": "approval",
                "elicitation_id": elicitation_id,
                "action": body.get("action"),
            }
            if body.get("content") is not None:
                event_body["content"] = body["content"]
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json=event_body,
                timeout=30.0,
            )
            return _forward_harness_response(resp)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=502,
                content={
                    "error": "elicitation_failed",
                    "detail": _client_safe_error_detail(exc, context="elicitation forward"),
                },
            )
