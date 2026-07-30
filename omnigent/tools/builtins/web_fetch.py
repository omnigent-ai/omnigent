"""Built-in tool: web_fetch — LLM-powered web research via sub-agent.

Declares a built-in ``__web_researcher`` sub-agent. The actual spawn
runs in the runner's tool dispatch (see
``omnigent/runner/tool_dispatch.py::_execute_web_fetch_tool``) which
funnels into ``_execute_subagent_tool`` — the same path
``sys_session_send`` uses. The Tool here owns the schema, the parent's
sub-agent registration, and the researcher spec; ``invoke`` itself is
never reached because the runner dispatches the call before the
in-process loop sees it.

Usage in config.yaml::

    tools:
      builtins:
        - web_fetch
"""

from __future__ import annotations

import logging
import shutil
import sys

# Any: tool schemas are heterogeneous dicts, AgentSpec.params
# has heterogeneous values.
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    InteractionConfig,
    ToolsConfig,
)
from omnigent.tools.base import Tool

_logger = logging.getLogger(__name__)

# ``ExecutorSpec.type`` defaults to this. It is an executor type, never a
# registered harness, so a spec whose ``harness_kind`` resolves to it cannot be
# spawned — the runner aborts with ``unknown harness 'omnigent'``.
_UNBOOTABLE_DEFAULT_HARNESS: str = "omnigent"

# Internal sub-agent name. Double-underscore prefix prevents
# collision with user-declared sub-agent names (which use
# [a-z0-9-]+ naming convention).
RESEARCHER_NAME: str = "__web_researcher"

_RESEARCHER_INSTRUCTIONS: str = """\
You are a fast web research assistant. Speed is critical — the caller
is waiting for your result synchronously.

You have a sys_os_shell tool that runs bash commands. Use it to run
commands that fetch web content. Be direct: fetch, extract the
answer, return it. Do not write elaborate scripts or over-analyze.

## Speed rules (most important)

- **One tool call when possible.** If a URL is given, fetch it in a
  single sys_os_shell call. Don't plan first — just do it.
- **Minimal script.** Use curl or a short Python one-liner. Don't
  write multi-function scripts with error handling classes.
- **Answer immediately.** Once you have the data, return the answer.
  Don't fetch additional sources unless the first one failed.
- **No unnecessary reasoning.** Don't explain your approach — just
  execute and return results.

## What you receive

- A **query**: what the caller wants to know
- An optional **URL**: a starting point to fetch

## What you do

1. If a URL is provided, fetch it immediately.
2. If no URL, search the web for the query.
3. Extract the relevant answer from the content.
4. Return the answer with source URLs. Be concise.

## Quick patterns

Fetch a URL (prefer curl for speed):
```
curl -sL "https://example.com" | head -200
```

Fetch JSON API:
```
curl -s "https://api.github.com/repos/owner/repo" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['stargazers_count'])"
```

Search the web:
```
curl -sL "https://html.duckduckgo.com/html/?q=your+query" | grep -oP 'href="\\K[^"]+' | head -5
```

## If the first attempt fails

Try ONE alternative approach, then return whatever you have. Don't
loop endlessly. If nothing works, say so.
"""


def _ensure_default_sandbox_runnable() -> None:
    """
    Fail at spec-build time when the platform-default sandbox the
    researcher would inherit cannot run on this host.

    A parent with no ``os_env`` leaves the researcher's ``sandbox``
    unset, which resolves to the platform default (see
    ``omnigent.inner.sandbox._default_sandbox_for_platform``) without
    probing for its binary. The spawn then failed mid-run with a hint
    to set ``os_env.sandbox.type`` — unreachable for a spawn-only
    parent, which cannot add an ``os_env`` block without also
    registering OS tools on itself. Probe here and point at the actual
    remediation: the missing host dependency.

    Windows needs no probe: ``windows_jobobject`` drives kernel Job
    Objects through ``ctypes`` with no external binary.

    :raises OmnigentError: On Linux when ``bwrap`` is not on ``PATH``,
        or on macOS when ``sandbox-exec`` is not on ``PATH``.
    """
    if sys.platform.startswith("linux") and shutil.which("bwrap") is None:
        raise OmnigentError(
            "web_fetch's __web_researcher sub-agent runs under the "
            "platform-default linux_bwrap sandbox, which requires the "
            "'bwrap' binary on PATH. Install bubblewrap on this host "
            "(e.g. `apt install bubblewrap` or `dnf install bubblewrap`).",
            code=ErrorCode.INVALID_INPUT,
        )
    if sys.platform == "darwin" and shutil.which("sandbox-exec") is None:
        raise OmnigentError(
            "web_fetch's __web_researcher sub-agent runs under the "
            "platform-default darwin_seatbelt sandbox, which requires "
            "the 'sandbox-exec' binary on PATH. It ships with macOS at "
            "/usr/bin/sandbox-exec; verify your PATH includes /usr/bin.",
            code=ErrorCode.INVALID_INPUT,
        )


def build_researcher_spec(parent_spec: AgentSpec) -> AgentSpec:
    """
    Build the ``__web_researcher`` AgentSpec from the parent's spec.

    The researcher gets:
    - The parent's ``llm`` config (model + connection + extras)
    - The parent executor's harness (``config``), ``auth``, ``model``, and
      ``connection`` (with ``max_iterations`` capped low) — the researcher
      runs on the SAME harness leg as its parent and routes through the
      parent's provider. Without this the child defaults to ``type="omnigent"``
      with no harness, which the runner rejects as ``unknown harness
      'omnigent'`` before any model routing (Layer 1), and even past that
      a gateway model loses its provider and hits the native router's
      ``Unknown provider`` (Layer 2).
    - An ``os_env`` block — registers ``sys_os_shell`` for one-shot
      bash commands (curl, python3 one-liners). The previous
      implementation used ``terminal_run``; that family was deleted
      per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §3a in favor of
      ``sys_os_shell`` for one-shot cases.
    - The parent's ``os_env.sandbox`` (filesystem grants, egress
      rules, private-destination policy). The runner treats the
      resolved child spec as authoritative for ``sys_os_*`` and only
      wires the egress proxy from ``spec.sandbox`` (see
      ``omnigent/inner/os_env.py::create_os_environment``). If the
      child dropped the parent's sandbox, an egress-restricted parent
      would silently gain an unrestricted network path through the
      researcher — so the child must never be more privileged than
      its parent.
    - Non-conversational mode (one-shot task)
    - Inline instructions for web research

    :param parent_spec: The parent agent's parsed spec.
    :returns: A complete AgentSpec for the web researcher sub-agent.
    :raises OmnigentError: If the parent declares no bootable harness, so the
        researcher would spawn the unspawnable literal harness ``"omnigent"``;
        or when the parent declares no ``os_env`` and the platform-default
        sandbox cannot run on this host (missing ``bwrap`` on Linux,
        ``sandbox-exec`` on macOS).
    """
    from omnigent.inner.datamodel import OSEnvSpec

    parent_os_env = parent_spec.os_env
    if parent_os_env is None:
        _ensure_default_sandbox_runnable()
    # Inherit the parent's sandbox so the child is bound by the same
    # filesystem and egress policy. ``sandbox`` carries
    # ``egress_rules`` / ``egress_allow_private_destinations``, which
    # is the only state ``create_os_environment`` reads to start the
    # MITM egress proxy. ``cwd`` is intentionally left at the default
    # (inherit the parent process working dir) — the one-shot curl /
    # python invocations don't need a specific workspace.
    child_os_env = OSEnvSpec(
        type=parent_os_env.type if parent_os_env is not None else "caller_process",
        sandbox=parent_os_env.sandbox if parent_os_env is not None else None,
    )

    # Inherit the parent leg's routing-relevant executor fields (harness, model,
    # auth, connection, type) so the researcher runs on the SAME harness with the
    # SAME credentials and model. The prior code built a bare
    # ``ExecutorSpec(max_iterations=5)``, which defaults to the unspawnable
    # ``type="omnigent"`` harness and strips the parent's provider. Drop
    # ``context_window`` (auto-detected), ``profile`` (deprecated, subsumed by
    # ``auth``), and the inline ``config["os_env"]`` (superseded by ``os_env``
    # below) — none are routing inputs.
    parent_executor = parent_spec.executor
    child_executor_config = {
        key: value for key, value in parent_executor.config.items() if key != "os_env"
    }
    child_executor = ExecutorSpec(
        type=parent_executor.type,
        max_iterations=5,  # one-shot: 1 fetch + 1 retry + final response
        config=child_executor_config,
        model=parent_executor.model,
        connection=parent_executor.connection,
        auth=parent_executor.auth,
    )

    # Fail loud if the inherited executor still has no bootable harness. The
    # child is spawned solely from this static spec (no per-session
    # ``harness_override`` is threaded here), so a harness that lives only in
    # resolved session state can't be recovered — better an actionable
    # build-time error naming the parent than a cryptic runner-side crash.
    if child_executor.harness_kind == _UNBOOTABLE_DEFAULT_HARNESS:
        raise OmnigentError(
            f"web_fetch cannot build its {RESEARCHER_NAME} sub-agent: parent agent "
            f"{parent_spec.name or '<unnamed>'!r} declares no bootable harness "
            f"(executor.type={parent_executor.type!r} with no "
            f"executor.config['harness']), so the researcher would spawn the "
            f"unknown harness 'omnigent'. Set executor.config.harness on the parent "
            f"(e.g. 'claude-sdk', 'codex', or 'pi') so the researcher runs on the "
            f"parent's harness.",
            code=ErrorCode.INVALID_INPUT,
        )

    return AgentSpec(
        spec_version=1,
        name=RESEARCHER_NAME,
        description="Internal sub-agent for web_fetch — searches and fetches web content.",
        llm=parent_spec.llm,
        interaction=InteractionConfig(conversational=False),
        tools=ToolsConfig(),
        os_env=child_os_env,
        instructions=_RESEARCHER_INSTRUCTIONS,
        executor=child_executor,
    )


class WebFetchTool(Tool):
    """
    Web research tool that spawns a sub-agent with a persistent shell.

    The sub-agent searches the web and/or fetches specific URLs,
    extracts text, and returns findings. The parent agent sees
    this as a synchronous function tool call.

    Only works with the ``llm`` executor. Returns an error for
    ``claude_sdk`` and ``agents_sdk`` executors (which don't
    support sub-agents).

    :param parent_spec: The parent agent's parsed AgentSpec.
        Used to copy LLM config into the researcher sub-agent.
    """

    def __init__(self, parent_spec: AgentSpec) -> None:
        """
        Build the researcher sub-agent spec and append it to the
        parent's sub_agents list.

        :param parent_spec: The parent agent's AgentSpec.
        """
        self._parent_spec = parent_spec
        self.researcher_spec = build_researcher_spec(parent_spec)
        # Append to parent's sub_agents so _resolve_agent_spec_for_task
        # can find it when the spawned task runs. This is permanent for
        # the lifetime of the ToolManager (one workflow execution).
        # Safe for parallel tool calls — all read the same spec.
        parent_spec.sub_agents.append(self.researcher_spec)

    @classmethod
    def name(cls) -> str:
        """
        :returns: ``"web_fetch"``.
        """
        return "web_fetch"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Human-readable description of the tool.
        """
        return (
            "Deep web research — fetches live web pages and "
            "summarizes relevant content. Always gets the "
            "latest version of a page. Use this when you "
            "need to read what a page actually says or need "
            "the most current info. Optionally provide a URL "
            "as a starting point; if it doesn't answer the "
            "query, other sources will be searched. Slower "
            "and less comprehensive than web_search but "
            "returns actual page content."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema for web_fetch.

        :returns: A function tool schema with ``query`` (required)
            and ``url`` (optional) parameters.
        """
        return {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": (
                    "Deep web research — fetches live web pages and "
                    "summarizes relevant content. Always gets the "
                    "latest version of a page. Use this when you "
                    "need to read what a page actually says or need "
                    "the most current info. Optionally provide a URL "
                    "as a starting point; if it doesn't answer the "
                    "query, other sources will be searched. Slower "
                    "and less comprehensive than web_search but "
                    "returns actual page content."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look up.",
                        },
                        "url": {
                            "type": "string",
                            "description": (
                                "Optional starting URL to fetch. If the "
                                "content doesn't answer the query, other "
                                "sources will be searched."
                            ),
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run web_fetch synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of
            this tool, not the per-call arguments.
        :returns: ``False`` — web_fetch always runs synchronously.
        """
        del arguments
        return False


def build_web_fetch_prompt(query: str, url: str | None) -> str:
    """
    Build the user input for the web researcher sub-agent.

    Used by the runner-side dispatcher to construct the message
    passed to the spawned ``__web_researcher`` session.

    :param query: What to look up.
    :param url: Optional starting URL.
    :returns: Formatted prompt string.
    """
    if url:
        return f"Query: {query}\n\nStart with this URL: {url}"
    return f"Query: {query}"
