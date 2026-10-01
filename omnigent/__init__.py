"""Omnigent: A declarative agent authoring and runtime framework."""

# Some libraries we transitively depend on call ``hashlib.md5()``
# without ``usedforsecurity=False`` for non-security content hashes.
# On FIPS-enabled OpenSSL builds the bare md5 constructor raises
# ``ValueError: digital envelope routines: EVP_DigestInit_ex disabled
# for FIPS``, which crashes the entire framework boot. Patch md5 here,
# at the package import boundary, so every consumer — including
# subprocesses spawned via ``-m omnigent`` in e2e tests — picks up
# the fix before any dependency import touches it. The flag is the
# standard Python 3.9+ opt-out for non-security md5 calls and is a
# harmless no-op on non-FIPS hosts.
import hashlib as _fips_safe_hashlib

_fips_safe_orig_md5 = _fips_safe_hashlib.md5


def _fips_safe_md5(*args, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("usedforsecurity", False)
    return _fips_safe_orig_md5(*args, **kwargs)


_fips_safe_hashlib.md5 = _fips_safe_md5

# Mirror legacy ``OMNIAGENTS_*`` env vars onto their new ``OMNIGENT_*`` names
# before any submodule below reads the environment, so the dual-read
# backward-compat fallback is in effect for the entire package.
from omnigent._env_compat import mirror_legacy_env as _mirror_legacy_env  # noqa: E402

_mirror_legacy_env()

# The public names below re-export lazily (PEP 562). This package init is on
# the hot path of every ``python -m omnigent.<hook>`` subprocess Claude Code
# spawns — once per streamed text chunk (the TUI blocks on the MessageDisplay
# hook), per statusline refresh, and per tool call — and eagerly importing the
# datamodel/executor graph here cost those spawns ~250 ms each. Names resolve
# on first attribute access and are cached in module globals; the import-graph
# guards live in tests/test_claude_native_message_display_hook.py and the
# wall-clock trend in the ``native_hook_spawn`` benchmark journey.
import importlib  # noqa: E402
from typing import TYPE_CHECKING, Any  # noqa: E402

if TYPE_CHECKING:
    from omnigent.core.datamodel import (
        AgentDef as AgentDef,
    )
    from omnigent.core.datamodel import (
        Connection as Connection,
    )
    from omnigent.core.datamodel import (
        Credentials as Credentials,
    )
    from omnigent.core.datamodel import (
        History as History,
    )
    from omnigent.core.datamodel import (
        Memory as Memory,
    )
    from omnigent.core.datamodel import (
        MemoryConfig as MemoryConfig,
    )
    from omnigent.core.datamodel import (
        Message as Message,
    )
    from omnigent.core.datamodel import (
        ParamDef as ParamDef,
    )
    from omnigent.core.datamodel import (
        SessionState as SessionState,
    )
    from omnigent.core.executor import (
        Executor as Executor,
    )
    from omnigent.core.executor import (
        ExecutorConfig as ExecutorConfig,
    )
    from omnigent.core.executor import (
        ExecutorError as ExecutorError,
    )
    from omnigent.core.executor import (
        ExecutorEvent as ExecutorEvent,
    )
    from omnigent.core.executor import (
        TextChunk as TextChunk,
    )
    from omnigent.core.executor import (
        ToolCallComplete as ToolCallComplete,
    )
    from omnigent.core.executor import (
        ToolCallRequest as ToolCallRequest,
    )
    from omnigent.core.executor import (
        TurnCancelled as TurnCancelled,
    )
    from omnigent.core.executor import (
        TurnComplete as TurnComplete,
    )
    from omnigent.core.loader import load_agent_def as load_agent_def
    from omnigent.core.policies import (
        FunctionPolicy as FunctionPolicy,
    )
    from omnigent.core.policies import (
        Policy as Policy,
    )
    from omnigent.core.policies import (
        PolicyAction as PolicyAction,
    )
    from omnigent.core.policies import (
        PolicyResult as PolicyResult,
    )
    from omnigent.core.policies import (
        PromptPolicy as PromptPolicy,
    )
    from omnigent.core.tools import (
        AgentTool as AgentTool,
    )
    from omnigent.core.tools import (
        CancellableFunctionTool as CancellableFunctionTool,
    )
    from omnigent.core.tools import (
        FunctionTool as FunctionTool,
    )
    from omnigent.core.tools import (
        HandoffTool as HandoffTool,
    )
    from omnigent.core.tools import (
        InheritedTool as InheritedTool,
    )
    from omnigent.core.tools import (
        MCPTool as MCPTool,
    )
    from omnigent.core.tools import (
        SkillTool as SkillTool,
    )
    from omnigent.core.tools import (
        Tool as Tool,
    )
    from omnigent.harnesses.claude_sdk.executor import ClaudeSDKExecutor as ClaudeSDKExecutor
    from omnigent.harnesses.codex.executor import CodexExecutor as CodexExecutor
    from omnigent.harnesses.databricks.executor import DatabricksExecutor as DatabricksExecutor
    from omnigent.harnesses.open_responses.executor import (
        OpenResponsesExecutor as OpenResponsesExecutor,
    )
    from omnigent.harnesses.openai_agents.executor import (
        OpenAIAgentsSDKExecutor as OpenAIAgentsSDKExecutor,
    )
    from omnigent.observability.tracing import (
        disable_tracing as disable_tracing,
    )
    from omnigent.observability.tracing import (
        enable_tracing as enable_tracing,
    )
    from omnigent.observability.tracing import (
        is_tracing_enabled as is_tracing_enabled,
    )

# Public name → defining module for the always-present re-exports.
_LAZY_EXPORTS = {
    "AgentDef": "omnigent.core.datamodel",
    "Connection": "omnigent.core.datamodel",
    "Credentials": "omnigent.core.datamodel",
    "History": "omnigent.core.datamodel",
    "Memory": "omnigent.core.datamodel",
    "MemoryConfig": "omnigent.core.datamodel",
    "Message": "omnigent.core.datamodel",
    "ParamDef": "omnigent.core.datamodel",
    "SessionState": "omnigent.core.datamodel",
    "Executor": "omnigent.core.executor",
    "ExecutorConfig": "omnigent.core.executor",
    "ExecutorError": "omnigent.core.executor",
    "ExecutorEvent": "omnigent.core.executor",
    "TextChunk": "omnigent.core.executor",
    "ToolCallComplete": "omnigent.core.executor",
    "ToolCallRequest": "omnigent.core.executor",
    "TurnCancelled": "omnigent.core.executor",
    "TurnComplete": "omnigent.core.executor",
    "FunctionPolicy": "omnigent.core.policies",
    "Policy": "omnigent.core.policies",
    "PolicyAction": "omnigent.core.policies",
    "PolicyResult": "omnigent.core.policies",
    "PromptPolicy": "omnigent.core.policies",
    "AgentTool": "omnigent.core.tools",
    "CancellableFunctionTool": "omnigent.core.tools",
    "FunctionTool": "omnigent.core.tools",
    "HandoffTool": "omnigent.core.tools",
    "InheritedTool": "omnigent.core.tools",
    "MCPTool": "omnigent.core.tools",
    "SkillTool": "omnigent.core.tools",
    "Tool": "omnigent.core.tools",
    "load_agent_def": "omnigent.core.loader",
    "disable_tracing": "omnigent.observability.tracing",
    "enable_tracing": "omnigent.observability.tracing",
    "is_tracing_enabled": "omnigent.observability.tracing",
}

# Optional executors resolve to ``None`` when their extra's dependencies are
# absent, matching the former eager try/except imports. Databricks also
# tolerates ``OSError``: its SDK can raise one probing credentials at import.
_OPTIONAL_EXPORTS = {
    "DatabricksExecutor": ("omnigent.harnesses.databricks.executor", (OSError, ImportError)),
    "ClaudeSDKExecutor": ("omnigent.harnesses.claude_sdk.executor", (ImportError,)),
    "OpenResponsesExecutor": ("omnigent.harnesses.open_responses.executor", (ImportError,)),
    "OpenAIAgentsSDKExecutor": ("omnigent.harnesses.openai_agents.executor", (ImportError,)),
    "CodexExecutor": ("omnigent.harnesses.codex.executor", (ImportError,)),
}


def __getattr__(name: str) -> Any:
    """Resolve a lazy re-export (or submodule) on first attribute access."""
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        value = getattr(importlib.import_module(target), name)
        globals()[name] = value
        return value
    optional = _OPTIONAL_EXPORTS.get(name)
    if optional is not None:
        target, absent_exceptions = optional
        try:
            value = getattr(importlib.import_module(target), name)
        except absent_exceptions:
            value = None
        globals()[name] = value
        return value
    # The eager imports used to bind ``inner`` (and other submodules touched
    # by them) as package attributes; keep ``omnigent.<submodule>`` access
    # working for consumers that only ran ``import omnigent``.
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name != f"{__name__}.{name}":
            raise
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__() -> list[str]:
    """Include the lazy re-exports in ``dir(omnigent)``."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AgentDef",
    "AgentTool",
    "CancellableFunctionTool",
    "ClaudeSDKExecutor",
    "CodexExecutor",
    "Connection",
    "Credentials",
    "DatabricksExecutor",
    "Executor",
    "ExecutorConfig",
    "ExecutorError",
    "ExecutorEvent",
    "FunctionPolicy",
    "FunctionTool",
    "HandoffTool",
    "History",
    "InheritedTool",
    "MCPTool",
    "Memory",
    "MemoryConfig",
    "Message",
    "OpenAIAgentsSDKExecutor",
    "OpenResponsesExecutor",
    "ParamDef",
    "Policy",
    "PolicyAction",
    "PolicyResult",
    "PromptPolicy",
    "SessionState",
    "SkillTool",
    "TextChunk",
    "Tool",
    "ToolCallComplete",
    "ToolCallRequest",
    "TurnCancelled",
    "TurnComplete",
    "disable_tracing",
    "enable_tracing",
    "is_tracing_enabled",
    "load_agent_def",
]
