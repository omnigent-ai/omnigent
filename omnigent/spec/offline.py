"""Read-only, data-only validation of local AgentSpec v1 images."""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Hashable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from omnigent.errors import OmnigentError
from omnigent.spec import offline_scope as scope
from omnigent.spec.parser import (
    _CONTEXT_FILE_PRIORITY,
    _FRONTMATTER_RE,
    _SKILL_NAMESPACE_MAX_DEPTH,
    _ConfigYamlLoader,
    _parse_http_mcp_server,
    _parse_stdio_mcp_server,
    parse_config,
    parse_skill_text,
)
from omnigent.spec.types import AgentSpec, LocalToolInfo, SkillSpec
from omnigent.spec.validator import ValidationError, validate

MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 1000
MAX_DEPTH = 32
MAX_YAML_NODES = 50000

SKIPPED_CHECKS = (
    (
        "HOST_AUTH",
        "Host readiness, credentials, environment expansion and provider auth are not checked.",
    ),
    ("LIVE_SERVICES", "MCP, model and network availability are not checked."),
    (
        "CODE",
        "Tool implementations and policy handlers/arguments are not imported, "
        "resolved or executed.",
    ),
    ("PLUGINS", "Community harness plugins and optional-package availability are not checked."),
)

_STATIC_FIELDS = {
    "spec_version": "Only AgentSpec version 1 is supported.",
    "executor": "Executor configuration violates AgentSpec rules.",
    "llm": "Model configuration violates AgentSpec rules.",
    "interaction": "Interaction modalities violate AgentSpec rules.",
    "skills": "Skill names or descriptions violate AgentSpec rules.",
    "mcp_servers": "MCP declarations contain invalid fields or colliding names.",
    "local_tools": "Local tool declarations contain invalid fields or colliding names.",
    "tools": "A tool references an undeclared sub-agent.",
    "sub_agents": "A sub-agent's metadata or references violate AgentSpec rules.",
    "name": "Agent names must be legal, non-reserved and unique across the bundle.",
    "compaction": "Compaction settings violate AgentSpec rules.",
}


def _static_diagnostic(error: ValidationError, filename: str) -> Diagnostic:
    # Nested validator paths embed authored names. Only expose a fixed field
    # group, not the original path or message (either can contain credentials).
    group = next(
        (
            name
            for name in _STATIC_FIELDS
            if error.path == name or error.path.startswith((name + ".", name + "["))
        ),
        "",
    )
    return Diagnostic(
        "INVALID_SPEC",
        _STATIC_FIELDS.get(group, "Agent metadata or references violate AgentSpec rules."),
        filename,
        group,
    )


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    file: str = ""
    field: str = ""
    line: int | None = None
    column: int | None = None
    severity: str = "error"


@dataclass
class OfflineResult:
    """Version 1 result envelope, with no timings, paths to the host, or input values."""

    diagnostics: list[Diagnostic] = field(default_factory=list)
    exit_code: int = 0

    def to_dict(self) -> dict[str, object]:
        ordered = sorted(
            self.diagnostics,
            key=lambda item: (item.file, item.field, item.line or 0, item.column or 0, item.code),
        )
        return {
            "schema_version": 1,
            "mode": "offline",
            "status": {0: "valid", 1: "invalid", 2: "invalid_invocation"}[self.exit_code],
            "exit_code": self.exit_code,
            "diagnostics": [asdict(item) for item in ordered],
            "skipped_checks": [
                {"code": code, "reason": reason} for code, reason in SKIPPED_CHECKS
            ],
        }


def invocation_error() -> OfflineResult:
    return OfflineResult(
        [
            Diagnostic(
                "INVALID_INVOCATION", "Expected one readable local YAML file or bundle directory."
            )
        ],
        exit_code=2,
    )


class _OfflineYamlLoader(_ConfigYamlLoader):
    """Keep the config loader's scalar semantics; reject ambiguous/unbounded YAML."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._nodes = 0
        self._depth = 0

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        self._nodes += 1
        self._depth += 1
        try:
            if self.check_event(yaml.AliasEvent):
                raise scope.ContentError(
                    "UNSUPPORTED_YAML", "", "YAML aliases are outside the offline scope."
                )
            if self._nodes > MAX_YAML_NODES or self._depth > MAX_DEPTH:
                raise scope.ContentError(
                    "INPUT_LIMIT", "", "YAML exceeds the offline complexity limit."
                )
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:  # type: ignore[explicit-any]  # SafeLoader override contract
        if not isinstance(node, yaml.MappingNode):
            raise scope.ContentError("INVALID_YAML", "", "Expected a YAML mapping.")
        result: dict[Hashable, Any] = {}  # type: ignore[explicit-any]  # decoded YAML values
        for key_node, value_node in node.value:
            if key_node.tag != "tag:yaml.org,2002:str":
                raise scope.ContentError(
                    "UNSUPPORTED_YAML", "", "YAML mapping keys must be strings."
                )
            key = key_node.value
            if key in result:
                raise scope.ContentError("DUPLICATE_KEY", "", "Duplicate YAML mapping key.")
            result[key] = self.construct_object(value_node, deep=deep)
        return result

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if any(key.tag == "tag:yaml.org,2002:merge" for key, _ in node.value):
            raise scope.ContentError(
                "UNSUPPORTED_YAML", "", "YAML merges are outside the offline scope."
            )
        super().flatten_mapping(node)


class _OfflineAssetYamlLoader(_OfflineYamlLoader):
    # MCP files and skill frontmatter use SafeLoader's YAML 1.1 scalar rules,
    # unlike config.yaml. Keep that distinction without changing runtime parsing.
    yaml_implicit_resolvers = yaml.SafeLoader.yaml_implicit_resolvers


def _load_yaml(text: str, *, asset: bool = False) -> object:
    return yaml.load(text, Loader=_OfflineAssetYamlLoader if asset else _OfflineYamlLoader)


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _check_path(path: Path) -> os.stat_result:
    # Reject ancestors too: a regular file reached through a directory symlink
    # could otherwise read outside the bundle or traverse a UNC target.
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if _is_link(info):
            raise scope.ContentError(
                "UNSAFE_PATH", "", "Symlinks and reparse points are not supported."
            )
    return info


class _Reader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.current = ""
        self.files = 0
        self.entry_count = 0

    def read(self, path: Path) -> str:
        self.current = path.relative_to(self.root).as_posix()
        self.files += 1
        if self.files > MAX_FILES:
            raise scope.ContentError("INPUT_LIMIT", "", "Bundle exceeds the offline file limit.")
        info = _check_path(path)
        if not stat.S_ISREG(info.st_mode):
            raise scope.ContentError("UNSAFE_PATH", "", "Expected a regular local file.")
        # Nonblocking/no-follow also prevents the final component being swapped
        # to a FIFO or symlink between lstat and open on supported POSIX hosts.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise scope.ContentError("UNSAFE_PATH", "", "Expected a regular local file.")
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise scope.ContentError("INPUT_LIMIT", "", "File exceeds the offline size limit.")
        return content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")

    def entries(self, directory: Path) -> list[Path]:
        self.current = directory.relative_to(self.root).as_posix()
        try:
            info = _check_path(directory)
        except FileNotFoundError:
            return []
        if not stat.S_ISDIR(info.st_mode):
            raise scope.ContentError("UNSAFE_PATH", "", "Expected a bundle directory.")
        entries = []
        with os.scandir(directory) as iterator:
            for entry in iterator:
                self.entry_count += 1
                if self.entry_count > MAX_FILES:
                    raise scope.ContentError(
                        "INPUT_LIMIT", "", "Directory exceeds the offline entry limit."
                    )
                entries.append(Path(entry.path))
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            self.current = entry.relative_to(self.root).as_posix()
            info = _check_path(entry)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise scope.ContentError(
                    "UNSAFE_PATH", "", "Only regular files and directories are supported."
                )
        return entries

    def instructions(self, root: Path, raw: dict[str, object]) -> str | None:
        value = raw.get("instructions", raw.get("prompt"))
        if isinstance(value, str):
            # Preserve inline prose; make common explicit filename references
            # strict instead of silently turning a missing file into instructions.
            if not value or "\n" in value:
                return value
            windows = PureWindowsPath(value)
            if value.startswith(("/", "\\", "~")) or windows.drive or ".." in windows.parts:
                raise scope.ContentError(
                    "UNSAFE_REFERENCE",
                    "instructions",
                    "Instruction paths must stay inside the bundle.",
                )
            candidate = root / value
            looks_like_file = Path(value).suffix.lower() in {".md", ".txt", ".rst"}
            try:
                info = _check_path(candidate)
            except (FileNotFoundError, NotADirectoryError):
                if looks_like_file:
                    raise scope.ContentError(
                        "MISSING_REFERENCE", "instructions", "Instruction file was not found."
                    ) from None
                return value
            except OSError as exc:
                if not looks_like_file and exc.errno in {errno.ENAMETOOLONG, errno.EINVAL}:
                    return value
                raise
            if stat.S_ISDIR(info.st_mode) and not looks_like_file:
                return value
            return self.read(candidate)
        for name in _CONTEXT_FILE_PRIORITY:
            candidate = root / name
            try:
                _check_path(candidate)
            except FileNotFoundError:
                continue
            return self.read(candidate)
        return None

    def skills(self, directory: Path, depth: int = 0) -> list[SkillSpec]:
        result = []
        for entry in self.entries(directory):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            path = entry / "SKILL.md"
            try:
                _check_path(path)
            except FileNotFoundError:
                if depth < _SKILL_NAMESPACE_MAX_DEPTH:
                    result.extend(self.skills(entry, depth + 1))
                    continue
                raise scope.ContentError(
                    "MISSING_REFERENCE", "skills", "Skill directory is missing SKILL.md."
                ) from None
            text = self.read(path)
            match = _FRONTMATTER_RE.match(text)
            if match is None:
                raise scope.ContentError(
                    "INVALID_SKILL", "skills", "SKILL.md requires YAML frontmatter."
                )
            metadata = scope.mapping(
                _load_yaml(match.group(1), asset=True),
                "skills",
                {"name", "description", "user-invocable"},
            )
            scope.require(
                isinstance(metadata.get("name"), str)
                and isinstance(metadata.get("description"), str),
                "skills",
                "Skill name and description must be strings.",
            )
            scope.scalar_fields(metadata, "skills", {"user-invocable"}, bool)
            result.append(parse_skill_text(text, path))
        return result

    def agent(self, path: Path, depth: int = 0) -> AgentSpec:
        if depth > MAX_DEPTH:
            raise scope.ContentError(
                "INPUT_LIMIT", "agents", "Agent nesting exceeds the offline limit."
            )
        raw = scope.config(_load_yaml(self.read(path)))
        spec = parse_config(raw, expand_env=False)
        root = path.parent
        spec.instructions = self.instructions(root, raw)
        spec.skills = self.skills(root / "skills")
        if isinstance(spec.skills_filter, list):
            scope.require(
                set(spec.skills_filter) <= {skill.name for skill in spec.skills},
                "skills",
                "Skill filter references must name bundled skills.",
            )
        for config_path in self.entries(root / "tools" / "mcp"):
            if config_path.suffix == ".yml":
                raise scope.ContentError(
                    "UNSUPPORTED_FORMAT",
                    "tools.mcp",
                    "Bundled MCP declarations require the .yaml extension.",
                )
            if config_path.suffix != ".yaml":
                continue
            data = scope.mcp(
                _load_yaml(self.read(config_path), asset=True), "tools.mcp", inline=False
            )
            parser = (
                _parse_http_mcp_server if data["transport"] == "http" else _parse_stdio_mcp_server
            )
            spec.mcp_servers.append(parser(data["name"], data, config_path, expand_env=False))
        for language, extension in (("python", ".py"), ("typescript", ".ts")):
            for tool in self.entries(root / "tools" / language):
                if tool.suffix == extension:
                    scope.require(
                        tool.is_file(), "local_tools", "Tool declarations must be files."
                    )
                    spec.local_tools.append(
                        LocalToolInfo(tool.stem, str(tool.relative_to(root)), language)
                    )
        for child in self.entries(root / "agents"):
            scope.require(
                child.is_dir(), "agents", "Sub-agent entries must be directories with config.yaml."
            )
            sub_spec = self.agent(child / "config.yaml", depth + 1)
            sub_spec.source_rel_dir = child.name
            spec.sub_agents.append(sub_spec)
        return spec


def validate_path(value: str) -> OfflineResult:
    """Validate local data only; never call spec.load or the standalone YAML loader."""
    if (
        not value
        or value.startswith(("\\\\", "//", "~"))
        or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value)
    ):
        return invocation_error()
    try:
        source = Path(os.path.abspath(value))
        info = _check_path(source)
    except scope.ContentError as exc:
        return OfflineResult([Diagnostic(exc.code, str(exc))], 2)
    except (OSError, ValueError):
        return invocation_error()
    if stat.S_ISDIR(info.st_mode):
        root, config_path = source, source / "config.yaml"
    elif stat.S_ISREG(info.st_mode) and source.suffix.lower() in {".yaml", ".yml"}:
        root, config_path = source.parent, source
    else:
        return invocation_error()
    reader = _Reader(root)
    try:
        spec = reader.agent(config_path)
        errors = validate(spec, offline=True).errors
    except scope.ContentError as exc:
        return OfflineResult([Diagnostic(exc.code, str(exc), reader.current, exc.field)], 1)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        return OfflineResult(
            [
                Diagnostic(
                    "INVALID_YAML",
                    "Invalid YAML document.",
                    reader.current,
                    line=mark.line + 1 if mark is not None else None,
                    column=mark.column + 1 if mark is not None else None,
                )
            ],
            1,
        )
    except OSError:
        return OfflineResult(
            [
                Diagnostic(
                    "UNREADABLE_INPUT", "A required bundle file could not be read.", reader.current
                )
            ],
            1,
        )
    except (OmnigentError, ValueError, TypeError, OverflowError, RecursionError):
        # Parser errors can include complete credentials or policy arguments.
        # Expected content failures are deliberately not logged or interpolated.
        return OfflineResult(
            [
                Diagnostic(
                    "INVALID_SPEC", "Content violates the AgentSpec parser rules.", reader.current
                )
            ],
            1,
        )
    # Validator messages and paths may contain authored names and values too.
    return OfflineResult(
        [_static_diagnostic(error, config_path.name) for error in errors],
        1 if errors else 0,
    )
