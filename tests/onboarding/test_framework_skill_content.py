"""Keep the framework skills in step with the CLI, the tool registry, and the docs.

Every session gets these skills, so a stale command or tool name misleads
every user. These checks catch the drift that left the old Omnigent
reference skill recommending ``ap deploy`` and a ``terminal_run`` builtin
long after both were gone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomllib
import yaml

from omnigent.cli import cli
from omnigent.runner.tool_dispatch import (
    _NATIVE_RELAY_BUILTIN_TOOLS,
    build_native_relay_tool_schemas,
)
from omnigent.spec.framework_skills import (
    FRAMEWORK_SKILL_NAMES,
    framework_skill_specs,
    framework_skills_dir,
)
from omnigent.tools.builtins import BUILTIN_NAMES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FENCED_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_REPO_PATH = re.compile(r"[\w./-]+\.md")
# ``sys_os_*`` is a wildcard mention, not a tool name.
_SYS_TOOL = re.compile(r"\bsys_[a-z0-9_]*[a-z0-9]\b(?!\*)")
# A shell call: an inline span or shell-block line that starts with the program.
_CLI_CALL = re.compile(r"(?:\$\s*)?(omnigent|omni|ap)\s+([a-z][a-z0-9-]*)")
_SHELL_LANGS = frozenset({"", "bash", "sh", "shell", "console", "zsh"})


def _skill_text(skill_name: str) -> str:
    return (framework_skills_dir() / skill_name / "SKILL.md").read_text(encoding="utf-8")


def _code_spans(skill_name: str) -> list[str]:
    """Inline code spans outside fenced blocks."""
    return _CODE_SPAN.findall(_FENCED_BLOCK.sub("", _skill_text(skill_name)))


def _fenced_blocks(skill_name: str) -> list[tuple[str, str]]:
    """``(language, body)`` for each fenced block."""
    return _FENCED_BLOCK.findall(_skill_text(skill_name))


@pytest.mark.parametrize("skill_name", FRAMEWORK_SKILL_NAMES)
def test_skill_parses_with_a_trigger_description(skill_name: str) -> None:
    """Agents pick a skill by its description, which the spec caps at 1024 characters."""
    spec = next(spec for spec in framework_skill_specs() if spec.name == skill_name)

    assert spec.description.strip()
    assert len(spec.description) <= 1024


@pytest.mark.parametrize("skill_name", FRAMEWORK_SKILL_NAMES)
def test_cli_commands_exist(skill_name: str) -> None:
    """Every CLI call the skill shows uses an installed program and a real command.

    ``ap`` is the CLI's former name; a call that still uses it fails here.
    """
    scripts = set(tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"])
    shell_lines = [
        line
        for lang, body in _fenced_blocks(skill_name)
        if lang in _SHELL_LANGS
        for line in body.splitlines()
    ]
    calls = [
        match.groups()
        for text in (*_code_spans(skill_name), *shell_lines)
        if (match := _CLI_CALL.match(text.strip()))
    ]
    stale = [
        f"{program} {command}"
        for program, command in calls
        if program not in scripts or command not in cli.commands
    ]

    assert not stale, stale


@pytest.mark.parametrize("skill_name", FRAMEWORK_SKILL_NAMES)
def test_sys_tool_names_exist(skill_name: str) -> None:
    """Every ``sys_*`` tool the skill names is one Omnigent registers."""
    known = (
        set(BUILTIN_NAMES)
        | set(_NATIVE_RELAY_BUILTIN_TOOLS)
        | {schema["name"] for schema in build_native_relay_tool_schemas(None)}
    )
    used = {name for span in _code_spans(skill_name) for name in _SYS_TOOL.findall(span)}

    assert used <= known, sorted(used - known)


@pytest.mark.parametrize("skill_name", FRAMEWORK_SKILL_NAMES)
def test_yaml_example_builtins_exist(skill_name: str) -> None:
    """Every ``tools.builtins`` entry in a YAML example is a builtin that exists today."""
    named: list[str] = []
    for lang, body in _fenced_blocks(skill_name):
        if lang not in ("yaml", "yml"):
            continue
        doc = yaml.safe_load(body)
        tools = doc.get("tools") if isinstance(doc, dict) else None
        builtins = tools.get("builtins") if isinstance(tools, dict) else None
        for entry in builtins or []:
            named.append(entry["name"] if isinstance(entry, dict) else str(entry))

    assert set(named) <= BUILTIN_NAMES, sorted(set(named) - BUILTIN_NAMES)


@pytest.mark.parametrize("skill_name", FRAMEWORK_SKILL_NAMES)
def test_repo_doc_paths_exist(skill_name: str) -> None:
    """Every repository doc path the skill names exists, so a pinned fetch can succeed."""
    used = {
        span
        for span in _code_spans(skill_name)
        if _REPO_PATH.fullmatch(span) and ("/" in span or span == "README.md")
    }

    missing = sorted(path for path in used if not (_REPO_ROOT / path).is_file())
    assert not missing, missing
