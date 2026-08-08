"""Discovery and packaging of workspace-authored agent configs.

A repo can carry agent definitions under ``.omnigent/agent-configs/`` —
the same subdirectory the runner's ``sys_agent_list`` tool scans (see
``omnigent.runner.tool_dispatch._AGENT_CONFIG_SUBDIR``). Two shapes are
recognized, matching :func:`omnigent.spec.materialize_bundle`:

- flat ``*.yaml`` files (the single-file omnigent dialect), and
- ``<name>/config.yaml`` directories (full agent images with tools,
  skills, and ``agents/`` sub-agent images).

The scanner returns light summaries for the picker and consent dialog;
the packager builds deterministic ``.tar.gz`` bytes for the standard
multipart bundle upload, so the digest a user consents to is exactly the
digest of what gets uploaded. Both are read-only and never execute
anything from the workspace.
"""

from __future__ import annotations

import gzip
import io
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

import yaml

AGENT_CONFIG_SUBDIR = ".omnigent/agent-configs"

# Ceiling for packaged bundle bytes. Bundles are text configs plus small
# tool/skill files; anything larger is almost certainly a mistake (or a
# repo trying to smuggle bulk data through the discovery path).
MAX_PACKAGED_BUNDLE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class WorkspaceAgentConfig:
    """One repo-declared agent config, summarized for discovery.

    :param slug: Stable id derived from the display name (unique within
        one scan; collisions get ``-2`` / ``-3`` … suffixes).
    :param name: Display name (config ``name:``, else the file/dir stem).
    :param path: Path relative to the workspace root, e.g.
        ``".omnigent/agent-configs/helper.yaml"`` — the packager input.
    :param kind: ``"file"`` (single-file YAML) or ``"bundle"``
        (directory image).
    :param description: Config ``description:``, when present.
    :param harness: Declared harness (``executor.config.harness`` else
        ``executor.type``), when readable.
    :param sub_agents: ``tools.agents`` allowlist names (bundles only).
    :param has_local_tools: Bundle declares ``tools/python|typescript``.
    :param has_mcp_servers: Bundle declares ``tools/mcp/*.yaml``.
    """

    slug: str
    name: str
    path: str
    kind: str
    description: str | None = None
    harness: str | None = None
    sub_agents: tuple[str, ...] = ()
    has_local_tools: bool = False
    has_mcp_servers: bool = False

    def as_dict(self) -> dict[str, object]:
        """Serialize for the host frame / REST payload."""
        return {
            "slug": self.slug,
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "description": self.description,
            "harness": self.harness,
            "sub_agents": list(self.sub_agents),
            "has_local_tools": self.has_local_tools,
            "has_mcp_servers": self.has_mcp_servers,
        }


def _slugify(name: str) -> str:
    """Lowercase, non-alnum runs to ``-``; empty falls back to ``agent``."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "agent"


def _read_mapping(path: Path) -> dict[str, object] | None:
    """Load a YAML mapping, or ``None`` on any read/parse failure."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _declared_harness(config: dict[str, object]) -> str | None:
    """Pull the declared harness out of an ``executor:`` block, if any."""
    executor = config.get("executor")
    if not isinstance(executor, dict):
        return None
    exec_config = executor.get("config")
    if isinstance(exec_config, dict):
        harness = exec_config.get("harness")
        if isinstance(harness, str) and harness:
            return harness
    etype = executor.get("type")
    return etype if isinstance(etype, str) and etype else None


def _sub_agent_names(config: dict[str, object]) -> tuple[str, ...]:
    """The ``tools.agents`` allowlist, as a tuple of names."""
    tools = config.get("tools")
    if not isinstance(tools, dict):
        return ()
    agents = tools.get("agents")
    if not isinstance(agents, list):
        return ()
    return tuple(a for a in agents if isinstance(a, str))


def scan_workspace_agent_configs(workspace: Path) -> list[WorkspaceAgentConfig]:
    """Summarize the agent configs a workspace declares.

    Scans ``<workspace>/.omnigent/agent-configs/`` for flat ``*.yaml``
    files and ``<name>/config.yaml`` bundle directories, in sorted
    order. Unreadable or non-mapping entries are skipped; any failure
    yields ``[]`` — a broken repo config must never break discovery.

    :param workspace: The selected working directory.
    :returns: Discovered configs (possibly empty).
    """
    try:
        configs_dir = Path(workspace).expanduser() / AGENT_CONFIG_SUBDIR
        if not configs_dir.is_dir():
            return []

        entries: list[WorkspaceAgentConfig] = []
        seen: dict[str, int] = {}

        def _add(
            *,
            name: str,
            path: Path,
            kind: str,
            config: dict[str, object],
            sub_agents: tuple[str, ...] = (),
            has_local_tools: bool = False,
            has_mcp_servers: bool = False,
        ) -> None:
            base = _slugify(name)
            count = seen.get(base, 0) + 1
            seen[base] = count
            description = config.get("description")
            entries.append(
                WorkspaceAgentConfig(
                    slug=base if count == 1 else f"{base}-{count}",
                    name=name,
                    path=str(path.relative_to(Path(workspace).expanduser())),
                    kind=kind,
                    description=description if isinstance(description, str) else None,
                    harness=_declared_harness(config),
                    sub_agents=sub_agents,
                    has_local_tools=has_local_tools,
                    has_mcp_servers=has_mcp_servers,
                )
            )

        for child in sorted(configs_dir.iterdir()):
            if child.is_file() and child.suffix.lower() in {".yaml", ".yml"}:
                config = _read_mapping(child)
                if config is None:
                    continue
                raw_name = config.get("name")
                _add(
                    name=raw_name if isinstance(raw_name, str) and raw_name else child.stem,
                    path=child,
                    kind="file",
                    config=config,
                )
            elif child.is_dir() and (child / "config.yaml").is_file():
                config = _read_mapping(child / "config.yaml")
                if config is None:
                    continue
                raw_name = config.get("name")
                tools_dir = child / "tools"
                _add(
                    name=raw_name if isinstance(raw_name, str) and raw_name else child.name,
                    path=child,
                    kind="bundle",
                    config=config,
                    sub_agents=_sub_agent_names(config),
                    has_local_tools=any(
                        (tools_dir / lang).is_dir() and any((tools_dir / lang).iterdir())
                        for lang in ("python", "typescript")
                    ),
                    has_mcp_servers=(tools_dir / "mcp").is_dir()
                    and any((tools_dir / "mcp").glob("*.yaml")),
                )
        return entries
    except (OSError, ValueError, yaml.YAMLError):
        return []


def package_workspace_agent(
    workspace: Path,
    config_path: str,
    *,
    max_bytes: int = MAX_PACKAGED_BUNDLE_BYTES,
) -> bytes:
    """Build deterministic ``.tar.gz`` bundle bytes for a repo config.

    Materializes the source (file or directory) into the uniform bundle
    shape via :func:`omnigent.spec.materialize_bundle`, then tars it
    with pinned mtimes/modes and sorted members so identical content
    always yields identical bytes — the client hashes these bytes for
    the consent grant and uploads them verbatim, so what the user
    approved is exactly what runs.

    :param workspace: The selected working directory (containment root).
    :param config_path: Workspace-relative path of a discovered config.
    :param max_bytes: Ceiling for the total packaged size.
    :raises ValueError: If the path escapes the workspace, isn't a
        recognized config shape, or the package exceeds *max_bytes*.
    :raises FileNotFoundError: If the source does not exist.
    """
    import tempfile

    from omnigent.spec import materialize_bundle

    root = Path(workspace).expanduser().resolve()
    source = (root / config_path).resolve()
    if not source.is_relative_to(root):
        raise ValueError("config_path escapes the workspace")
    is_yaml_file = source.is_file() and source.suffix.lower() in {".yaml", ".yml"}
    if not is_yaml_file and not (source.is_dir() and (source / "config.yaml").is_file()):
        raise ValueError("config_path is not an agent YAML or bundle directory")

    with tempfile.TemporaryDirectory() as tmpdir:
        bundle_dir = materialize_bundle(source, Path(tmpdir) / "bundle")
        buf = io.BytesIO()
        total = 0
        with (
            gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
            tarfile.open(fileobj=gz, mode="w") as tar,
        ):
            for file_path in sorted(bundle_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                data = file_path.read_bytes()
                total += len(data)
                if total > max_bytes:
                    raise ValueError(f"packaged bundle exceeds {max_bytes} bytes")
                info = tarfile.TarInfo(str(file_path.relative_to(bundle_dir)))
                info.size = len(data)
                info.mtime = 0
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        return buf.getvalue()
