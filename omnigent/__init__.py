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

from typing import TYPE_CHECKING  # noqa: E402

# Re-exports resolve on first access (PEP 562) instead of at import. Importing
# this package eagerly pulled the executor / model-catalog / provider-config
# chain — roughly 700ms — which every short-lived policy-hook subprocess paid at
# startup while using none of it. The md5 patch and env mirror above stay eager,
# so the "patch before any dependency import" ordering they require is unchanged
# (dependencies now load strictly later, never earlier).
_LAZY_EXPORTS = {
    "AgentDef": "omnigent.inner.datamodel",
    "Connection": "omnigent.inner.datamodel",
    "Credentials": "omnigent.inner.datamodel",
    "History": "omnigent.inner.datamodel",
    "Memory": "omnigent.inner.datamodel",
    "MemoryConfig": "omnigent.inner.datamodel",
    "Message": "omnigent.inner.datamodel",
    "ParamDef": "omnigent.inner.datamodel",
    "SessionState": "omnigent.inner.datamodel",
    "Executor": "omnigent.inner.executor",
    "ExecutorConfig": "omnigent.inner.executor",
    "ExecutorError": "omnigent.inner.executor",
    "ExecutorEvent": "omnigent.inner.executor",
    "TextChunk": "omnigent.inner.executor",
    "ToolCallComplete": "omnigent.inner.executor",
    "ToolCallRequest": "omnigent.inner.executor",
    "TurnCancelled": "omnigent.inner.executor",
    "TurnComplete": "omnigent.inner.executor",
    "FunctionPolicy": "omnigent.inner.policies",
    "Policy": "omnigent.inner.policies",
    "PolicyAction": "omnigent.inner.policies",
    "PolicyResult": "omnigent.inner.policies",
    "PromptPolicy": "omnigent.inner.policies",
    "AgentTool": "omnigent.inner.tools",
    "CancellableFunctionTool": "omnigent.inner.tools",
    "FunctionTool": "omnigent.inner.tools",
    "HandoffTool": "omnigent.inner.tools",
    "InheritedTool": "omnigent.inner.tools",
    "MCPTool": "omnigent.inner.tools",
    "SkillTool": "omnigent.inner.tools",
    "Tool": "omnigent.inner.tools",
    "load_agent_def": "omnigent.inner.loader",
    "disable_tracing": "omnigent.inner.tracing",
    "enable_tracing": "omnigent.inner.tracing",
    "is_tracing_enabled": "omnigent.inner.tracing",
}

# Executors whose extras may be absent. The eager form wrapped each import in
# ``try/except`` and fell back to ``None``; missing extras must keep degrading
# gracefully rather than raising at attribute access.
_OPTIONAL_EXPORTS = {
    "DatabricksExecutor": ("omnigent.inner.databricks_executor", (OSError, ImportError)),
    "ClaudeSDKExecutor": ("omnigent.inner.claude_sdk_executor", (ImportError,)),
    "OpenResponsesExecutor": ("omnigent.inner.open_responses_sdk", (ImportError,)),
    "OpenAIAgentsSDKExecutor": ("omnigent.inner.openai_agents_sdk_executor", (ImportError,)),
    "CodexExecutor": ("omnigent.inner.codex_executor", (ImportError,)),
}

if TYPE_CHECKING:
    # Static analysers cannot see ``__getattr__``-provided names; re-declare the
    # re-exports so type checking and IDE completion behave as before.
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
    from omnigent.inner.codex_executor import CodexExecutor
    from omnigent.inner.databricks_executor import DatabricksExecutor
    from omnigent.inner.datamodel import (
        AgentDef,
        Connection,
        Credentials,
        History,
        Memory,
        MemoryConfig,
        Message,
        ParamDef,
        SessionState,
    )
    from omnigent.inner.executor import (
        Executor,
        ExecutorConfig,
        ExecutorError,
        ExecutorEvent,
        TextChunk,
        ToolCallComplete,
        ToolCallRequest,
        TurnCancelled,
        TurnComplete,
    )
    from omnigent.inner.loader import load_agent_def
    from omnigent.inner.open_responses_sdk import OpenResponsesExecutor
    from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor
    from omnigent.inner.policies import (
        FunctionPolicy,
        Policy,
        PolicyAction,
        PolicyResult,
        PromptPolicy,
    )
    from omnigent.inner.tools import (
        AgentTool,
        CancellableFunctionTool,
        FunctionTool,
        HandoffTool,
        InheritedTool,
        MCPTool,
        SkillTool,
        Tool,
    )
    from omnigent.inner.tracing import disable_tracing, enable_tracing, is_tracing_enabled


def __getattr__(name: str) -> object:
    """
    Resolve a re-exported name on first access.

    :param name: Attribute requested from the ``omnigent`` package.
    :returns: The re-exported object, or ``None`` for an optional executor
        whose extra is not installed.
    :raises AttributeError: If *name* is not a documented re-export.
    """
    import importlib

    module_name = _LAZY_EXPORTS.get(name)
    if module_name is not None:
        value = getattr(importlib.import_module(module_name), name)
        globals()[name] = value
        return value

    optional = _OPTIONAL_EXPORTS.get(name)
    if optional is not None:
        module_name, errors = optional
        try:
            value = getattr(importlib.import_module(module_name), name)
        except (*errors, AttributeError):
            value = None
        globals()[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """
    List the package's public names, including lazily-resolved re-exports.

    :returns: Sorted public attribute names.
    """
    return sorted(set(__all__) | set(globals()))


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
