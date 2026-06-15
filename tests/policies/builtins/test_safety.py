"""
Tests for the built-in safety policies
(:mod:`omnigent.policies.builtins.safety`).

Covers:

- ``ask_on_os_tools`` — ASKs approval before file/shell tool calls,
  including Omnigent ``sys_os_*`` tools, Claude Code native tools
  (``Bash``, ``Read``, ``Write``, ``Edit``, ``Glob``, ``Grep``),
  and Codex native tools (same ``PreToolUse`` hook contract).
- ``block_skills`` — factory that denies skill loading via two paths:
  1. ``load_skill`` / ``read_skill_file`` tool calls (TOOL_CALL phase).
  2. Slash-command skill loads (REQUEST phase, ``"/<name> <args>"``).
- Existing policies are tested transitively via the registry tests;
  these tests focus on the callable's decision logic.
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.safety import ask_on_os_tools, block_skills
from omnigent.policies.schema import PolicyEvent
from tests.policies.builtins.helpers import tool_call_event as tc

# ── ask_on_os_tools: Omnigent sys_os_* tools ─────────────────────────────


@pytest.mark.parametrize(
    "tool",
    ["sys_os_read", "sys_os_write", "sys_os_edit", "sys_os_shell"],
    ids=["read", "write", "edit", "shell"],
)
def test_ask_on_os_tools_asks_for_sys_os_tools(tool: str) -> None:
    """Each ``sys_os_*`` tool triggers ASK.

    If any returns ALLOW, the policy is not matching the Omnigent
    built-in OS tool set.
    """
    args = {"command": "ls"} if tool == "sys_os_shell" else {"path": "/tmp/f"}
    result = ask_on_os_tools(tc(tool, args))
    assert result["result"] == "ASK"
    # Reason must name the tool so the user knows what is being asked.
    assert tool in result["reason"]


# ── ask_on_os_tools: Claude Code / Codex native tools ─────────────────────


@pytest.mark.parametrize(
    "tool,args,expected_preview",
    [
        ("Bash", {"command": "rm -rf /"}, "rm -rf /"),
        ("Read", {"path": "/etc/passwd"}, "/etc/passwd"),
        ("Write", {"path": "/tmp/out.txt"}, "/tmp/out.txt"),
        ("Edit", {"path": "main.py"}, "main.py"),
        ("Glob", {"pattern": "**/*.py"}, "**/*.py"),
        ("Grep", {"pattern": "secret"}, "secret"),
    ],
    ids=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
)
def test_ask_on_os_tools_asks_for_native_tools(
    tool: str,
    args: dict[str, str],
    expected_preview: str,
) -> None:
    """Claude Code and Codex native tools trigger ASK via the
    ``PreToolUse`` hook contract.

    If any returns ALLOW, the policy does not cover the native tool
    name and the user won't see an approval prompt for harness-native
    file/shell operations.

    :param tool: Native tool name, e.g. ``"Bash"``.
    :param args: Tool arguments dict.
    :param expected_preview: Substring that must appear in the reason
        preview so the user sees what the tool will do.
    """
    result = ask_on_os_tools(tc(tool, args))
    assert result["result"] == "ASK"
    assert tool in result["reason"]
    # The preview should contain the relevant argument value so the
    # user can make an informed approval decision.
    assert expected_preview in result["reason"]


# ── ask_on_os_tools: Pi native tools (lowercase) ──────────────────────────


@pytest.mark.parametrize(
    "tool,args,expected_preview",
    [
        ("bash", {"command": "rm -rf /"}, "rm -rf /"),
        ("read", {"path": "/etc/passwd"}, "/etc/passwd"),
        ("write", {"path": "/tmp/out.txt"}, "/tmp/out.txt"),
        ("edit", {"path": "main.py"}, "main.py"),
    ],
    ids=["bash", "read", "write", "edit"],
)
def test_ask_on_os_tools_asks_for_pi_native_tools(
    tool: str,
    args: dict[str, str],
    expected_preview: str,
) -> None:
    """Pi's lowercase native tools trigger ASK via the pi ``tool_call``
    hook contract.

    Pi names its in-process tools ``read`` / ``bash`` / ``write`` /
    ``edit`` — distinct from the Claude/Codex casing covered above. The
    pi executor routes these through the same TOOL_CALL verdict, so the
    builtin must recognize the lowercase names; otherwise a polly pi
    worker calling native ``read`` (enabled for skills) is silently
    un-gated. If any returns ALLOW, the lowercase name isn't covered.

    Asserts the preview too, proving pi's ``command`` / ``path`` arg keys
    (which match the Omnigent convention, not Claude's ``file_path``)
    resolve through the existing preview branches without a pi-specific
    arg path.

    :param tool: Pi native tool name, e.g. ``"bash"``.
    :param args: Tool arguments dict.
    :param expected_preview: Substring that must appear in the reason.
    """
    result = ask_on_os_tools(tc(tool, args))
    assert result["result"] == "ASK"
    assert tool in result["reason"]
    assert expected_preview in result["reason"]


def test_ask_on_os_tools_allows_non_os_tool() -> None:
    """A tool that is not a file/shell operation passes through.

    If this returns ASK, the policy is over-matching on unrelated tools.
    """
    result = ask_on_os_tools(tc("web_search", {"query": "hello"}))
    assert result["result"] == "ALLOW"


def test_ask_on_os_tools_allows_non_tool_call_phase() -> None:
    """Non-tool_call phases (e.g. ``response``) pass through.

    If this returns ASK, the policy is firing on phases it shouldn't.
    """
    event: PolicyEvent = {
        "type": "response",
        "target": None,
        "data": "I ran Bash for you.",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = ask_on_os_tools(event)
    assert result["result"] == "ALLOW"


# ── block_skills: load_skill ────────────────────────────────────────────────


def test_block_skills_denies_blocked_load_skill() -> None:
    """A ``load_skill`` call for a blocked skill name is denied.

    If this passes when the deny branch is removed, the policy
    is not intercepting load_skill calls.
    """
    policy = block_skills(blocked=["code-review"])
    result = policy(tc("load_skill", {"name": "code-review"}))
    assert result["result"] == "DENY"
    # Reason should mention the blocked skill name so the user
    # understands why the call was denied.
    assert "code-review" in result["reason"]


def test_block_skills_allows_unblocked_load_skill() -> None:
    """A ``load_skill`` call for a skill NOT in the blocked list is allowed.

    If this returns DENY, the policy is over-blocking.
    """
    policy = block_skills(blocked=["code-review"])
    result = policy(tc("load_skill", {"name": "deploy"}))
    assert result["result"] == "ALLOW"


def test_block_skills_case_insensitive() -> None:
    """Skill name matching is case-insensitive.

    Blocked list has lowercase; tool call has mixed case.
    If this returns ALLOW, the case normalization is broken.
    """
    policy = block_skills(blocked=["code-review"])
    result = policy(tc("load_skill", {"name": "Code-Review"}))
    assert result["result"] == "DENY"


@pytest.mark.parametrize(
    "blocked_name,call_name",
    [
        ("Code-Review", "code-review"),
        ("CODE-REVIEW", "code-review"),
        ("deploy", "DEPLOY"),
    ],
    ids=["upper-blocked-lower-call", "allcaps-blocked-lower-call", "lower-blocked-upper-call"],
)
def test_block_skills_case_insensitive_parametrized(blocked_name: str, call_name: str) -> None:
    """Case insensitivity works in both directions.

    :param blocked_name: The name as declared in the blocked list.
    :param call_name: The name the agent passes to ``load_skill``.
    """
    policy = block_skills(blocked=[blocked_name])
    result = policy(tc("load_skill", {"name": call_name}))
    assert result["result"] == "DENY"


# ── block_skills: read_skill_file ───────────────────────────────────────────


def test_block_skills_denies_blocked_read_skill_file() -> None:
    """A ``read_skill_file`` call for a blocked skill is denied.

    ``read_skill_file`` uses ``skill_name`` (not ``name``) as its
    argument key. If this returns ALLOW, the argument key mapping
    for read_skill_file is wrong.
    """
    policy = block_skills(blocked=["code-review"])
    result = policy(tc("read_skill_file", {"skill_name": "code-review", "path": "foo.md"}))
    assert result["result"] == "DENY"
    assert "code-review" in result["reason"]


def test_block_skills_allows_unblocked_read_skill_file() -> None:
    """A ``read_skill_file`` call for a non-blocked skill is allowed.

    If this returns DENY, the policy is over-blocking read_skill_file.
    """
    policy = block_skills(blocked=["code-review"])
    result = policy(tc("read_skill_file", {"skill_name": "deploy", "path": "foo.md"}))
    assert result["result"] == "ALLOW"


# ── block_skills: non-skill tools + non-tool_call phases ────────────────────


def test_block_skills_allows_non_skill_tools() -> None:
    """Tool calls that are not ``load_skill`` or ``read_skill_file``
    pass through regardless of blocked list.

    If this returns DENY, the policy is intercepting unrelated tools.
    """
    policy = block_skills(blocked=["code-review"])
    result = policy(tc("sys_os_shell", {"command": "echo hello"}))
    assert result["result"] == "ALLOW"


def test_block_skills_allows_response_phase() -> None:
    """Non-actionable phases (e.g. ``response``) pass through.

    If this returns DENY, the policy is firing on phases it shouldn't.
    """
    policy = block_skills(blocked=["code-review"])
    event: PolicyEvent = {
        "type": "response",
        "target": None,
        "data": "I loaded the code-review skill for you.",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ALLOW"


def test_block_skills_allows_request_without_slash() -> None:
    """Request-phase events that are plain text (not a slash command)
    pass through even if they mention a blocked skill name.

    If this returns DENY, the policy is over-matching on request content.
    """
    policy = block_skills(blocked=["code-review"])
    event: PolicyEvent = {
        "type": "request",
        "target": None,
        "data": "load the code-review skill",
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }
    result = policy(event)
    assert result["result"] == "ALLOW"


# ── block_skills: multiple blocked names ────────────────────────────────────


def test_block_skills_multiple_blocked_names() -> None:
    """Multiple skill names can be blocked simultaneously.

    If only the first name is blocked, the frozenset construction
    is broken.
    """
    policy = block_skills(blocked=["code-review", "deploy", "admin"])
    # All three blocked
    assert policy(tc("load_skill", {"name": "code-review"}))["result"] == "DENY"
    assert policy(tc("load_skill", {"name": "deploy"}))["result"] == "DENY"
    assert policy(tc("load_skill", {"name": "admin"}))["result"] == "DENY"
    # Not blocked
    assert policy(tc("load_skill", {"name": "safe-skill"}))["result"] == "ALLOW"


# ── block_skills: edge cases ────────────────────────────────────────────────


def test_block_skills_empty_blocked_list_allows_all() -> None:
    """An empty blocked list allows all skill loads.

    If this returns DENY, the empty-set handling is wrong.
    """
    policy = block_skills(blocked=[])
    result = policy(tc("load_skill", {"name": "anything"}))
    assert result["result"] == "ALLOW"


def test_block_skills_missing_name_argument_allows() -> None:
    """A ``load_skill`` call with no ``name`` argument is allowed.

    Malformed calls should not crash the policy; they should pass
    through so the tool itself can report the validation error.
    """
    policy = block_skills(blocked=["code-review"])
    result = policy(tc("load_skill", {}))
    assert result["result"] == "ALLOW"


# ── block_skills: slash-command path (REQUEST phase) ────────────────────────


def _request_event(text: str) -> PolicyEvent:
    """Build a ``request`` :class:`PolicyEvent` with the given text.

    The Omnigent server evaluates skill slash commands at the REQUEST phase
    as synthetic ``"/<name> <args>"`` strings via
    ``_build_skill_slash_command_policy_body``.

    :param text: User input text, e.g. ``"/git-stack create foo"``.
    :returns: A ``request`` event dict.
    """
    return {
        "type": "request",
        "target": None,
        "data": text,
        "context": {"actor": {}, "usage": {}},
        "session_state": {},
    }


def test_block_skills_denies_slash_command_blocked_skill() -> None:
    """A ``/blocked-skill`` slash command is denied at the request phase.

    This is the path the Omnigent server takes when the user types
    ``/skill-name`` in the UI — it converts to a synthetic request
    with text ``"/skill-name"``. If this returns ALLOW, the slash
    command bypass is not covered.
    """
    policy = block_skills(blocked=["git-stack"])
    result = policy(_request_event("/git-stack"))
    assert result["result"] == "DENY"
    assert "git-stack" in result["reason"]


def test_block_skills_denies_slash_command_with_arguments() -> None:
    """A ``/blocked-skill args`` slash command is denied.

    The command name is the first token after ``/``.
    """
    policy = block_skills(blocked=["git-stack"])
    result = policy(_request_event("/git-stack create my-feature"))
    assert result["result"] == "DENY"
    assert "git-stack" in result["reason"]


def test_block_skills_allows_slash_command_unblocked_skill() -> None:
    """A ``/safe-skill`` slash command passes through.

    If this returns DENY, the policy is over-blocking slash commands.
    """
    policy = block_skills(blocked=["git-stack"])
    result = policy(_request_event("/deploy"))
    assert result["result"] == "ALLOW"


def test_block_skills_slash_command_case_insensitive() -> None:
    """Slash-command matching is case-insensitive.

    The user might type ``/Git-Stack`` but the blocked list has
    ``git-stack``.
    """
    policy = block_skills(blocked=["git-stack"])
    result = policy(_request_event("/Git-Stack"))
    assert result["result"] == "DENY"


def test_block_skills_slash_command_bare_slash_allows() -> None:
    """A bare ``/`` with no command name passes through.

    Edge case: should not crash or over-match.
    """
    policy = block_skills(blocked=["git-stack"])
    result = policy(_request_event("/"))
    assert result["result"] == "ALLOW"


@pytest.mark.parametrize("text", ["/ ", "/   ", "/\t"])
def test_block_skills_slash_then_whitespace_allows(text: str) -> None:
    """A slash followed only by whitespace passes through without crashing.

    ``split(None, ...)`` drops empty tokens, so ``"/ ".split()`` is an
    empty list. The command extraction must not index ``[0]`` blindly,
    or evaluation raises ``IndexError`` and the request fails closed.
    """
    policy = block_skills(blocked=["git-stack"])
    result = policy(_request_event(text))
    assert result["result"] == "ALLOW"


# ── ask_on_add_policy ──────────────────────────────────────────


def test_ask_on_add_policy_asks_for_sys_add_policy() -> None:
    """sys_add_policy tool call triggers ASK with a preview."""
    from omnigent.policies.builtins.safety import ask_on_add_policy

    result = ask_on_add_policy(
        {
            "type": "tool_call",
            "data": {
                "name": "sys_add_policy",
                "arguments": {
                    "name": "block_shell",
                    "handler": "omnigent.policies.builtins.cel.cel_policy",
                },
            },
        }
    )
    assert result["result"] == "ASK"
    assert "block_shell" in result["reason"]
    assert "cel_policy" in result["reason"]


def test_ask_on_add_policy_allows_other_tools() -> None:
    """Non-policy tool calls pass through."""
    from omnigent.policies.builtins.safety import ask_on_add_policy

    result = ask_on_add_policy(
        {
            "type": "tool_call",
            "data": {"name": "web_search", "arguments": {}},
        }
    )
    assert result["result"] == "ALLOW"


def test_ask_on_add_policy_allows_non_tool_events() -> None:
    """Non-tool_call events pass through."""
    from omnigent.policies.builtins.safety import ask_on_add_policy

    result = ask_on_add_policy({"type": "request", "data": "hello"})
    assert result["result"] == "ALLOW"


def test_ask_on_add_policy_handles_missing_arguments() -> None:
    """Missing or non-dict arguments doesn't crash."""
    from omnigent.policies.builtins.safety import ask_on_add_policy

    result = ask_on_add_policy(
        {
            "type": "tool_call",
            "data": {"name": "sys_add_policy"},
        }
    )
    assert result["result"] == "ASK"


# ── block_skills: native Skill tool (PreToolUse hook path) ─────────────────


def test_block_skills_denies_native_skill_tool() -> None:
    """The native ``Skill`` tool call for a blocked skill is denied.

    This is the primary enforcement path for native Claude Code and Codex
    harnesses, where there is no ``load_skill`` runner tool. The
    ``PreToolUse`` hook fires → Omnigent server evaluates → this policy denies.
    If this returns ALLOW, native harnesses can load blocked skills.
    """
    policy = block_skills(blocked=["deploy"])
    result = policy(tc("Skill", {"skill": "deploy"}))
    assert result["result"] == "DENY"
    assert "deploy" in result["reason"]


def test_block_skills_allows_unblocked_native_skill_tool() -> None:
    """A native ``Skill`` call for a skill NOT in the blocked list is allowed.

    If this returns DENY, the policy is over-blocking native skill calls.
    """
    policy = block_skills(blocked=["deploy"])
    result = policy(tc("Skill", {"skill": "review"}))
    assert result["result"] == "ALLOW"


def test_block_skills_native_skill_tool_case_insensitive() -> None:
    """Native ``Skill`` tool matching is case-insensitive.

    The blocked list has lowercase; the tool call has mixed case.
    If this returns ALLOW, the case normalization is broken for
    the native tool path.
    """
    policy = block_skills(blocked=["deploy"])
    result = policy(tc("Skill", {"skill": "Deploy"}))
    assert result["result"] == "DENY"


def test_block_skills_native_skill_tool_missing_skill_arg_allows() -> None:
    """A ``Skill`` call with no ``skill`` argument passes through gracefully.

    Edge case: should not crash or over-match when the argument is absent.
    """
    policy = block_skills(blocked=["deploy"])
    result = policy(tc("Skill", {}))
    assert result["result"] == "ALLOW"


def test_block_skills_native_skill_tool_with_args() -> None:
    """A ``Skill`` call with extra ``args`` is still checked correctly.

    The ``skill`` argument is what matters; ``args`` is the optional
    skill argument string and should not affect matching.
    """
    policy = block_skills(blocked=["deploy"])
    result = policy(tc("Skill", {"skill": "deploy", "args": "--force"}))
    assert result["result"] == "DENY"
