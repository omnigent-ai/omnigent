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
import sys

# Any: tool schemas are heterogeneous dicts, AgentSpec.params
# has heterogeneous values.
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.spec.types import (
    AgentSpec,
    ExecutorSpec,
    InteractionConfig,
    ToolsConfig,
)
from omnigent.tools.base import Tool

_logger = logging.getLogger(__name__)

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

# Default-deny egress allow-list applied to the researcher when the
# parent agent declares no ``os_env`` of its own (see
# ``build_researcher_spec``). Permits the read-oriented HTTP(S) verbs
# to ANY host; the ``*`` host pattern is load-bearing together with
# ``egress_allow_private_destinations=False`` below — the MITM proxy
# still refuses loopback / link-local / RFC1918 / CGNAT and
# cloud-metadata IPs (e.g. ``169.254.169.254``) at connect time, so the
# net effect is "any *public* host". The write verbs (PUT/DELETE/PATCH/
# TRACE) are intentionally omitted: a web researcher reads pages and
# queries search/JSON APIs, it never needs to mutate remote state.
_DEFAULT_RESEARCHER_EGRESS_RULES: tuple[str, ...] = ("GET,HEAD,POST,OPTIONS *",)


def _platform_egress_backend() -> str | None:
    """
    Return this platform's network-isolating sandbox backend, or
    ``None`` when none can hard-enforce egress here.

    Mirrors the platform decision in
    ``omnigent.inner.sandbox._default_sandbox_for_platform`` but is
    narrowed to the backends that actually isolate the network so the
    L7 egress proxy is the *only* path off the box: ``linux_bwrap`` on
    Linux (unshared network namespace) and ``darwin_seatbelt`` on macOS
    (SBPL ``(deny network*)`` with a narrow loopback allow). Any other
    platform — notably Windows, whose ``windows_jobobject`` backend does
    not unshare the network and where the Unix-socket egress proxy is
    unavailable — returns ``None`` so the caller can fail closed instead
    of falling back to an unsandboxed shell.

    :returns: ``"linux_bwrap"``, ``"darwin_seatbelt"``, or ``None``.
    """
    if sys.platform.startswith("linux"):
        return "linux_bwrap"
    if sys.platform == "darwin":
        return "darwin_seatbelt"
    return None


def _default_researcher_sandbox() -> OSEnvSandboxSpec:
    """
    Build the locked-down default sandbox for a researcher whose parent
    declares no ``os_env``.

    The previous default (``sandbox=None``) resolved to the platform's
    default sandbox with the host network *shared* — handing the child a
    ``sys_os_shell`` that could reach cloud metadata
    (``169.254.169.254``), localhost services, and RFC1918 hosts, and
    that escalated a shell-less parent to an unsandboxed shell. This
    returns a default-deny egress sandbox instead: pinned to a
    network-isolating backend, all traffic forced through the MITM proxy
    (a non-empty ``egress_rules`` is what both starts the proxy and
    triggers the network-namespace isolation), and private / link-local
    / metadata destinations refused
    (``egress_allow_private_destinations=False``).

    :returns: A restrictive :class:`OSEnvSandboxSpec` for the child.
    :raises OmnigentError: When the host platform has no backend that
        can hard-enforce network isolation (e.g. Windows), so a secure
        default cannot be constructed — fail closed rather than fall
        back to an unsandboxed shell on the shared host network.
    """
    backend = _platform_egress_backend()
    if backend is None:
        raise OmnigentError(
            "web_fetch cannot build a secure default sandbox for its "
            "__web_researcher sub-agent on this platform: no sandbox "
            "backend here can hard-enforce network isolation (egress "
            "requires sandbox.type=linux_bwrap on Linux or "
            "sandbox.type=darwin_seatbelt on macOS). Declare an os_env "
            "with an egress policy on the parent agent, or run web_fetch "
            "on a supported platform.",
            code=ErrorCode.INVALID_INPUT,
        )
    return OSEnvSandboxSpec(
        type=backend,
        egress_rules=list(_DEFAULT_RESEARCHER_EGRESS_RULES),
        egress_allow_private_destinations=False,
    )


def build_researcher_spec(parent_spec: AgentSpec) -> AgentSpec:
    """
    Build the ``__web_researcher`` AgentSpec using the parent's LLM config.

    The researcher gets:
    - The parent's ``llm`` config (model + connection + extras)
    - An ``os_env`` block — registers ``sys_os_shell`` for one-shot
      bash commands (curl, python3 one-liners). The previous
      implementation used ``terminal_run``; that family was deleted
      per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §3a in favor of
      ``sys_os_shell`` for one-shot cases.
    - A sandbox that is **never more privileged than the parent**. The
      runner treats the resolved child spec as authoritative for
      ``sys_os_*`` and only wires the egress proxy from ``spec.sandbox``
      (egress_rules / egress_allow_private_destinations — see
      ``omnigent/inner/os_env.py::create_os_environment``). Two cases:

      * **Parent declares an ``os_env``** → inherit its ``type`` and
        ``sandbox`` verbatim. Dropping the sandbox here would silently
        hand an egress-restricted parent an unrestricted network path
        through the researcher.
      * **Parent declares no ``os_env``** → it has neither a
        ``sys_os_shell`` nor a network of its own, so fabricating a
        bare ``OSEnvSpec(sandbox=None)`` resolved to the platform
        default sharing the host network: a default-off SSRF +
        privilege-escalation surface (the child could curl cloud IMDS /
        localhost / RFC1918 and fold attacker-controlled page content
        into the same context that runs shell commands). Build a
        locked-down default-deny egress sandbox instead — see
        :func:`_default_researcher_sandbox`.
    - Non-conversational mode (one-shot task)
    - Inline instructions for web research

    :param parent_spec: The parent agent's parsed spec.
    :returns: A complete AgentSpec for the web researcher sub-agent.
    :raises OmnigentError: When the parent declares no ``os_env`` and
        the host platform cannot hard-enforce network isolation, so no
        secure default sandbox can be built (fail closed).
    """
    parent_os_env = parent_spec.os_env
    # ``cwd`` is intentionally left at the default (inherit the parent
    # process working dir) — the one-shot curl / python invocations
    # don't need a specific workspace.
    if parent_os_env is not None:
        child_os_env = OSEnvSpec(
            type=parent_os_env.type,
            sandbox=parent_os_env.sandbox,
        )
    else:
        child_os_env = OSEnvSpec(
            type="caller_process",
            sandbox=_default_researcher_sandbox(),
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
        # Low max_iterations to keep the sub-agent fast.
        # 1 fetch + 1 retry = 2 tool calls max, plus the
        # final response = ~3 iterations.
        executor=ExecutorSpec(max_iterations=5),
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
