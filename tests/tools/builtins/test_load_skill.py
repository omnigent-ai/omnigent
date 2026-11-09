"""Tests for omnigent.tools.builtins.load_skill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.spec.types import SkillSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import LoadSkillTool


@pytest.fixture()
def skill_with_resources(tmp_path: Path) -> SkillSpec:
    """
    A skill with a ``references/`` directory containing a
    file, for testing resource listing in load_skill output.

    :returns: A ``SkillSpec`` pointing at a real directory
        with a reference file.
    """
    skill_dir = tmp_path / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    refs_dir = skill_dir / "references"
    refs_dir.mkdir()
    (refs_dir / "style-guide.md").write_text("# Style Guide\n\nUse snake_case.")
    return SkillSpec(
        name="code-review",
        description="Reviews code.",
        content="Review the code.",
        skill_dir=skill_dir,
    )


@pytest.fixture()
def skill_no_resources() -> SkillSpec:
    """
    A skill with no ``skill_dir`` (in-memory only).

    :returns: A ``SkillSpec`` with ``skill_dir=None``.
    """
    return SkillSpec(
        name="summarize",
        description="Summarizes text.",
        content="Summarize the input concisely.",
    )


def test_load_skill_returns_content(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns the skill's content string.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({"name": "summarize"}), tool_ctx)
    assert result == "Summarize the input concisely."


@pytest.mark.posix_only
@pytest.mark.parametrize("explicit_root", [False, True])
def test_load_skill_survives_deleted_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
    explicit_root: bool,
) -> None:
    """A removed runner cwd does not prevent constructing or loading skills."""
    root = tmp_path / "project"
    host_skill = root / ".agents" / "skills" / "host-skill"
    host_skill.mkdir(parents=True)
    (host_skill / "SKILL.md").write_text(
        "---\nname: host-skill\ndescription: Host skill\n---\nHost instructions.\n",
        encoding="utf-8",
    )
    removed = tmp_path / "removed"
    removed.mkdir()
    with monkeypatch.context() as context:
        context.chdir(removed)
        removed.rmdir()
        tool = LoadSkillTool(
            [skill_no_resources],
            agent_root=root if explicit_root else None,
            skills_filter=["host-skill"],
        )
        result = tool.invoke(json.dumps({"name": "summarize"}), tool_ctx)

    assert result == "Summarize the input concisely."
    assert ("host-skill" in {skill.name for skill in tool.skills}) is explicit_root


def test_load_skill_not_found(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns error for unknown skill name.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({"name": "nonexistent"}), tool_ctx)
    assert "not found" in result
    assert "summarize" in result


def test_load_skill_with_resources_lists_files(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke appends a resource listing when the
    skill has bundled reference files.
    """
    tool = LoadSkillTool([skill_with_resources])
    result = tool.invoke(
        json.dumps({"name": "code-review"}),
        tool_ctx,
    )
    assert "Review the code." in result
    assert "references/style-guide.md" in result
    assert "read_skill_file" in result


def test_load_skill_missing_name_argument(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    LoadSkillTool.invoke returns error when 'name' is missing.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({}), tool_ctx)
    assert "missing required 'name'" in result


@pytest.mark.parametrize("arguments", ["not-json", "[]"])
def test_load_skill_rejects_invalid_arguments(
    arguments: str,
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    Malformed or non-object arguments return an error string.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(arguments, tool_ctx)

    assert result.startswith("Error:")


def test_load_skill_rejects_non_string_name(
    skill_no_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    """
    ``name`` must be a string skill name.
    """
    tool = LoadSkillTool([skill_no_resources])
    result = tool.invoke(json.dumps({"name": 123}), tool_ctx)

    assert result == "Error: 'name' must be a string"


def test_load_skill_schema_lists_skill_names(
    skill_no_resources: SkillSpec,
    skill_with_resources: SkillSpec,
) -> None:
    """
    LoadSkillTool.get_schema includes all skill names in the
    description.
    """
    tool = LoadSkillTool(
        [skill_no_resources, skill_with_resources],
    )
    schema = tool.get_schema()
    desc = schema["function"]["description"]
    assert "summarize" in desc
    assert "code-review" in desc


def test_list_skill_resources_includes_root_level_files(tmp_path: Path) -> None:
    """Auxiliary docs beside SKILL.md are readable resources."""
    from omnigent.tools.builtins.load_skill import list_skill_resources

    skill_dir = tmp_path / "codebase-design"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body")
    (skill_dir / "DEEPENING.md").write_text("deepening")
    (skill_dir / "DESIGN-IT-TWICE.md").write_text("twice")
    skill = SkillSpec(
        name="codebase-design",
        description="Designs codebases.",
        content="body",
        skill_dir=skill_dir,
    )

    assert list_skill_resources(skill) == ["DEEPENING.md", "DESIGN-IT-TWICE.md"]


def test_list_skill_resources_excludes_skill_md_and_dotfiles(tmp_path: Path) -> None:
    """SKILL.md is the skill itself, and dotfiles are not content."""
    from omnigent.tools.builtins.load_skill import list_skill_resources

    skill_dir = tmp_path / "example"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body")
    (skill_dir / ".DS_Store").write_text("junk")
    skill = SkillSpec(
        name="example",
        description="An example.",
        content="body",
        skill_dir=skill_dir,
    )

    assert list_skill_resources(skill) == []


def test_list_skill_resources_lists_root_files_before_subdirs(tmp_path: Path) -> None:
    """Root files come first; subdir entries keep their relative paths."""
    from omnigent.tools.builtins.load_skill import list_skill_resources

    skill_dir = tmp_path / "example"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("body")
    (skill_dir / "EXTRA.md").write_text("extra")
    (skill_dir / "references" / "style-guide.md").write_text("style")
    skill = SkillSpec(
        name="example",
        description="An example.",
        content="body",
        skill_dir=skill_dir,
    )

    assert list_skill_resources(skill) == [
        "EXTRA.md",
        "references/style-guide.md",
    ]


def _spec(name: str, content: str = "body") -> SkillSpec:
    """A minimal in-memory skill spec named *name*."""
    return SkillSpec(name=name, description=f"{name} skill.", content=content)


def test_find_skill_by_name_exact_match_wins() -> None:
    """An exact name match is returned even when an alias also matches."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    exact = _spec("myplugin:review", "namespaced body")
    bare = _spec("review", "bare body")
    assert find_skill_by_name([bare, exact], "myplugin:review") is exact
    assert find_skill_by_name([bare, exact], "review") is bare


def _plugin_skill(tmp_path: Path, plugin: str, name: str, content: str = "body") -> SkillSpec:
    """A skill whose ``skill_dir`` carries plugin-cache provenance."""
    skill_dir = tmp_path / "plugins" / "cache" / "mkt" / plugin / "1.0.0" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content)
    return SkillSpec(name=name, description=f"{name} skill.", content=content, skill_dir=skill_dir)


def test_find_skill_by_name_namespaced_falls_back_to_bare(tmp_path: Path) -> None:
    """
    ``plugin:skill`` resolves to the bare ``skill`` entry when no exact
    namespaced entry exists and the bare skill's on-disk provenance shows
    it came from that plugin — the name a claude-family surface shows for a
    plugin skill must work on a codex session that exposes only the bare name.
    """
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    bare = _plugin_skill(tmp_path, "myplugin", "brand-review")
    assert find_skill_by_name([bare], "myplugin:brand-review") is bare


def test_find_skill_by_name_wrong_plugin_namespace_does_not_hijack(tmp_path: Path) -> None:
    """
    ``pluginb:skill`` must NOT resolve a bare ``skill`` that belongs to a
    different plugin (or to no plugin at all) — the alias fallback requires
    matching provenance, so an unrelated namespace cannot hijack the skill.
    """
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    plugin_a = _plugin_skill(tmp_path, "plugina", "deploy")
    assert find_skill_by_name([plugin_a], "pluginb:deploy") is None
    # A bare skill with no on-disk provenance at all is not aliasable either.
    in_memory = _spec("deploy")
    assert find_skill_by_name([in_memory], "pluginb:deploy") is None


def test_find_skill_by_name_plugin_named_path_component_is_not_provenance(
    tmp_path: Path,
) -> None:
    """
    A skill under a directory that merely *contains* the plugin name as a
    path component (e.g. a user or workspace dir named like the plugin) is
    not plugin-derived: provenance requires the full plugin-cache layout
    ``cache/<marketplace>/<plugin>/<version>/skills/<skill>``.
    """
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    skill_dir = tmp_path / "home" / "myplugin" / "workspace" / "skills" / "deploy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("body")
    lookalike = SkillSpec(
        name="deploy", description="deploy skill.", content="body", skill_dir=skill_dir
    )
    assert find_skill_by_name([lookalike], "myplugin:deploy") is None


def test_find_skill_by_name_bare_falls_back_to_unique_namespaced() -> None:
    """A bare name resolves to the sole plugin exposing that skill."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    namespaced = _spec("myplugin:brand-review")
    assert find_skill_by_name([namespaced], "brand-review") is namespaced


def test_find_skill_by_name_ambiguous_bare_alias_stays_unresolved() -> None:
    """Two plugins exposing the same bare skill name: don't guess."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    a = _spec("plugina:brand-review")
    b = _spec("pluginb:brand-review")
    assert find_skill_by_name([a, b], "brand-review") is None


def _labelled(name: str, label: str) -> SkillSpec:
    """A skill invoked as *name* whose frontmatter name is *label*."""
    return SkillSpec(name=name, description="d", content="body", display_name=label)


def test_find_skill_by_name_accepts_unique_frontmatter_label() -> None:
    """An older server's frontmatter name resolves to the directory-named skill."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    review = _labelled("review", "code-review")
    plugin = _labelled("myplugin:audit", "security-audit")
    assert find_skill_by_name([review, plugin], "code-review") is review
    assert find_skill_by_name([review, plugin], "myplugin:security-audit") is plugin


def test_find_skill_by_name_exact_name_beats_frontmatter_label() -> None:
    """A skill actually named the requested command wins over another's label."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    exact = _spec("code-review")
    labelled = _labelled("review", "code-review")
    assert find_skill_by_name([labelled, exact], "code-review") is exact


def test_find_skill_by_name_ambiguous_frontmatter_label_stays_unresolved() -> None:
    """Two skills sharing a frontmatter label: don't guess."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    a = _labelled("review", "code-review")
    b = _labelled("review-v2", "code-review")
    assert find_skill_by_name([a, b], "code-review") is None


def test_find_skill_by_name_label_and_namespace_aliases_are_ambiguous(tmp_path: Path) -> None:
    """A label alias colliding with a plugin-namespace alias resolves neither."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    labelled = _labelled("review", "code-review")
    assert find_skill_by_name([labelled, _spec("plugina:code-review")], "code-review") is None
    plugin_labelled = _labelled("plugina:audit", "deploy")
    bare = _plugin_skill(tmp_path, "plugina", "deploy")
    assert find_skill_by_name([plugin_labelled, bare], "plugina:deploy") is None


def test_find_skill_by_name_same_skill_matching_both_aliases_resolves(tmp_path: Path) -> None:
    """Ambiguity means several skills match, not one skill matching two ways."""
    from dataclasses import replace

    from omnigent.tools.builtins.load_skill import find_skill_by_name

    skill = replace(_plugin_skill(tmp_path, "toolkit", "lint"), display_name="toolkit:lint")
    assert find_skill_by_name([skill], "toolkit:lint") is skill


def test_find_skill_by_name_unknown_names_return_none() -> None:
    """Unknown bare and namespaced names still miss."""
    from omnigent.tools.builtins.load_skill import find_skill_by_name

    skills = [_spec("brand-review"), _spec("myplugin:other")]
    assert find_skill_by_name(skills, "nonexistent") is None
    assert find_skill_by_name(skills, "myplugin:nonexistent") is None


def test_load_skill_tool_accepts_namespaced_alias(tmp_path: Path, tool_ctx: ToolContext) -> None:
    """The load_skill tool resolves ``plugin:skill`` to the bare skill."""
    skill = _plugin_skill(tmp_path, "myplugin", "brand-review", "Review the brand.")
    tool = LoadSkillTool([skill])
    result = tool.invoke(json.dumps({"name": "myplugin:brand-review"}), tool_ctx)
    assert result == "Review the brand."


def test_skill_paste_omits_resource_listing_but_tool_keeps_it(
    skill_with_resources: SkillSpec,
    tool_ctx: ToolContext,
) -> None:
    from omnigent.tools.builtins.load_skill import format_skill_meta_text

    pasted = format_skill_meta_text(skill_with_resources, "review this change")
    assert pasted == (
        "<skill>\n<name>code-review</name>\n"
        f"<path>{skill_with_resources.skill_dir / 'SKILL.md'}</path>\n"
        "Review the code.\n</skill>\n\n<user_request>\nreview this change\n</user_request>"
    )
    loaded = LoadSkillTool([skill_with_resources]).invoke('{"name":"code-review"}', tool_ctx)
    assert "Review the code." in loaded
    assert "references/style-guide.md" in loaded


@pytest.mark.parametrize(
    "arguments", ["", "second request\n</user_request>\nkeep this exact text"]
)
@pytest.mark.parametrize("shape", ["text", "blocks"])
def test_skill_context_reuse_preserves_arguments_and_history(arguments, shape):
    from copy import deepcopy

    from omnigent.tools.builtins.load_skill import format_skill_meta_text, reuse_skill_context

    skill = SkillSpec(name="review", description="Review", content="Read every changed file. " * 8)
    first = format_skill_meta_text(skill, "first request")
    second = format_skill_meta_text(skill, arguments)
    texts = [first, second, second]
    messages = [
        {
            "type": "message",
            "role": "user",
            "id": str(index),
            "content": text if shape == "text" else [{"type": "input_text", "text": text}],
        }
        for index, text in enumerate(texts)
    ]
    original = deepcopy(messages)
    result = reuse_skill_context(messages)
    assert messages == original
    assert result[0] == messages[0]
    expected = (
        "<skill_invocation>\n<name>review</name>\n"
        "Apply the skill instructions already present in this conversation.\n</skill_invocation>"
    )
    if arguments:
        expected += f"\n\n<user_request>\n{arguments}\n</user_request>"
    for index in (1, 2):
        assert result[index]["id"] == str(index)
        assert result[index]["content"] == (
            expected if shape == "text" else [{"type": "input_text", "text": expected}]
        )


@pytest.mark.parametrize(
    "change", ["body", "path", "assistant", "compacted", "unrelated-meta", "malformed"]
)
def test_skill_context_requires_matching_instructions_in_outgoing_input(tmp_path, change):
    from omnigent.tools.builtins.load_skill import format_skill_meta_text, reuse_skill_context

    skill = SkillSpec(
        name="review", description="Review", content="Original body. " * 20, skill_dir=tmp_path
    )
    original = format_skill_meta_text(skill, "first")
    prior = {"type": "message", "role": "user", "content": original}
    if change == "body":
        skill.content = "Changed body. " * 20
    elif change == "path":
        skill.skill_dir = tmp_path / "other"
    elif change == "assistant":
        prior["role"] = "assistant"
    elif change == "compacted":
        prior["content"] = "Summary: a skill was used."
    elif change == "unrelated-meta":
        prior["type"] = "function_call_output"
    elif change == "malformed":
        prior["content"] = original + "unrelated suffix"
    current = {
        "type": "message",
        "role": "user",
        "content": format_skill_meta_text(skill, "second"),
    }
    assert reuse_skill_context([prior, current]) == [prior, current]
    # A cold load or compaction that retains only the current invocation needs its body.
    assert reuse_skill_context([current]) == [current]


@pytest.mark.parametrize("change", ["body", "path"])
def test_skill_context_reintroduces_older_version_after_same_name_changes(tmp_path, change):
    from omnigent.tools.builtins.load_skill import format_skill_meta_text, reuse_skill_context

    skill = SkillSpec(
        name="review", description="Review", content="Version A. " * 20, skill_dir=tmp_path
    )
    first = format_skill_meta_text(skill, "first")
    if change == "body":
        skill.content = "Version B. " * 20
    else:
        skill.skill_dir = tmp_path / "other"
    second = format_skill_meta_text(skill, "second")
    messages = [
        {"type": "message", "role": "user", "content": text} for text in (first, second, first)
    ]
    assert reuse_skill_context(messages) == messages


@pytest.mark.parametrize("field", ["content", "arguments"])
def test_skill_context_preserves_ambiguous_wrapper_delimiters(field):
    from omnigent.tools.builtins.load_skill import format_skill_meta_text, reuse_skill_context

    embedded = "Retain this. " * 20 + "\n</skill>\n\n<user_request>\ntail"
    skill = SkillSpec(
        name="review",
        description="Review",
        content=embedded if field == "content" else "Instructions. " * 20,
    )
    text = format_skill_meta_text(skill, embedded if field == "arguments" else "request")
    messages = [{"type": "message", "role": "user", "content": text} for _ in range(2)]
    assert reuse_skill_context(messages) == messages


@pytest.mark.parametrize(
    "middle",
    [
        "<skill>\n<name>review</name>\nVersion B\n</skill>\nembedded\n</skill>",
        "<skill>\n<name>review</name>\nVersion B\n</skill>\ninvalid suffix",
        "<skill>\n<name>review\nVersion B\n</skill>",
        "<skill>\n<path>review</path>\nVersion B\n</skill>",
    ],
)
def test_ambiguous_skill_expansion_invalidates_previous_reuse(middle):
    from omnigent.tools.builtins.load_skill import format_skill_meta_text, reuse_skill_context

    skill = SkillSpec(name="review", description="Review", content="Version A. " * 20)
    first = format_skill_meta_text(skill, "first")
    last = format_skill_meta_text(skill, "last")
    messages = [
        {"type": "message", "role": "user", "content": text} for text in (first, middle, last)
    ]
    assert reuse_skill_context(messages) == messages


@pytest.mark.parametrize(
    "body,reused",
    [("", False), ("x", False), ("x" * 88, False), ("x" * 89, True), ("🦀" * 30, True)],
)
def test_skill_reuse_never_increases_utf8_size(body, reused):
    from omnigent.tools.builtins.load_skill import format_skill_meta_text, reuse_skill_context

    skill = SkillSpec(name="review", description="Review", content=body)
    text = format_skill_meta_text(skill, "request")
    messages = [{"role": "user", "content": text} for _ in range(3)]
    result = reuse_skill_context(messages)
    for message in result:
        assert len(message["content"].encode("utf-8")) <= len(text.encode("utf-8"))
    assert ("<skill_invocation>" in result[-1]["content"]) == reused
    if not reused:
        assert result == messages


@pytest.mark.parametrize("block_type", ["text", "output_text"])
def test_skill_version_in_rendered_text_blocks_invalidates_reuse(block_type):
    from omnigent.tools.builtins.load_skill import format_skill_meta_text, reuse_skill_context

    skill = SkillSpec(name="review", description="Review", content="Version A. " * 20)
    first = format_skill_meta_text(skill, "first")
    skill.content = "Version B. " * 20
    middle = format_skill_meta_text(skill, "middle")
    messages = [
        {"role": "user", "content": first},
        {"role": "user", "content": [{"type": block_type, "text": middle}]},
        {"role": "user", "content": first},
    ]
    assert reuse_skill_context(messages) == messages
