"""Resolve portable feature packs into bounded, reproducible agent bundles."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path

import yaml

from omnigent.extensions.api import AgentContribution, ExtensionManifest, PolicyContribution
from omnigent.extensions.assets import (
    ExtensionAssetError,
    _read_asset,
    _resource_root,
    _safe_parts,
)
from omnigent.policies.registry import function_policy_handler_allowed
from omnigent.spec import load, parse_default_policies
from omnigent.spec.parser import _ConfigYamlLoader
from omnigent.spec.tar_utils import pack_directory
from omnigent.spec.types import FunctionPolicySpec

MAX_PACK_BYTES = 32 * 1024 * 1024
MAX_PACK_FILES = 256
MAX_PACK_DEPTH = 16
LOCK_FILE = "omnigent-extension-lock.json"


@dataclass(frozen=True)
class ResolvedAgentContribution:
    """An immutable agent snapshot safe to copy to any supported runner."""

    extension_id: str
    id: str
    title: str
    version: str
    content: bytes
    digest: str

    @property
    def url(self) -> str:
        return f"/v1/extensions/{self.extension_id}/agents/{self.id}/{self.digest}/bundle"


@dataclass(frozen=True)
class ResolvedPolicyContribution:
    """A validated default-policy mapping; it is applied only when selected."""

    extension_id: str
    id: str
    title: str
    policies: dict[str, object]


def pack_root(package: str | None, root_override: Path | None = None) -> Traversable:
    """Reuse browser assets' verified-package and filesystem boundaries."""
    if root_override is not None:
        return root_override
    if package is None:
        raise ExtensionAssetError("feature pack has no verified resource package")
    return _resource_root(package)


def resolve_policy(
    manifest: ExtensionManifest, contribution: PolicyContribution, root: Traversable
) -> ResolvedPolicyContribution:
    """Parse the existing policy grammar without importing policy handlers."""
    raw = yaml.load(_read_asset(root, contribution.path, MAX_PACK_BYTES), Loader=_ConfigYamlLoader)
    if not isinstance(raw, dict) or not raw or any(not isinstance(key, str) for key in raw):
        raise ExtensionAssetError("policy preset must be a non-empty policy mapping")
    if any(not key.startswith(f"{manifest.id}.") for key in raw):
        raise ExtensionAssetError("policy names must be namespaced under the extension id")
    for policy in parse_default_policies(raw, expand_env=False):
        if isinstance(policy, FunctionPolicySpec):
            function = policy.function
            if function is None or not function_policy_handler_allowed(
                function.path, function.arguments
            ):
                raise ExtensionAssetError("policy presets may only reference registered handlers")
    return ResolvedPolicyContribution(manifest.id, contribution.id, contribution.title, raw)


def _copy_agent(root: Traversable, declared: str, target: Path) -> None:
    _safe_parts(declared)
    source = root.joinpath(declared)
    if not source.is_dir():
        raise ExtensionAssetError("agent path must be a directory containing config.yaml")
    pending = [(source, declared, target, 0)]
    total_bytes = 0
    file_count = 0
    while pending:
        directory, relative, destination, depth = pending.pop()
        if depth >= MAX_PACK_DEPTH:
            raise ExtensionAssetError("agent directory exceeds the depth limit")
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            # Python imports may leave cache directories in editable packages.
            if child.name == "__pycache__":
                continue
            file_count += 1
            if file_count > MAX_PACK_FILES:
                raise ExtensionAssetError("agent directory exceeds the entry count limit")
            child_path = f"{relative}/{child.name}"
            _safe_parts(child_path, allow_hidden=True)
            if isinstance(child, Path) and child.is_symlink():
                raise ExtensionAssetError("agent directories cannot contain symlinks")
            if child.is_dir():
                pending.append((child, child_path, destination / child.name, depth + 1))
                continue
            content = _read_asset(
                root, child_path, MAX_PACK_BYTES - total_bytes, allow_hidden=True
            )
            total_bytes += len(content)
            (destination / child.name).write_bytes(content)


def resolve_agent(
    manifest: ExtensionManifest,
    contribution: AgentContribution,
    root: Traversable,
    *,
    policies: Mapping[str, ResolvedPolicyContribution],
    versions: Mapping[str, str],
) -> ResolvedAgentContribution:
    """Apply selected presets and record the resolved extension versions in the bundle."""
    with tempfile.TemporaryDirectory(prefix="omnigent-extension-") as temporary:
        destination = Path(temporary) / "agent"
        _copy_agent(root, contribution.path, destination)
        config_path = destination / "config.yaml"
        if not config_path.is_file():
            raise ExtensionAssetError("agent directory must contain config.yaml")
        config = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_ConfigYamlLoader)
        if not isinstance(config, dict):
            raise ExtensionAssetError("agent config must be a mapping")
        if contribution.policies:
            guardrails = config.setdefault("guardrails", {})
            if not isinstance(guardrails, dict):
                raise ExtensionAssetError("agent guardrails must be a mapping")
            merged = guardrails.setdefault("policies", {})
            if not isinstance(merged, dict):
                raise ExtensionAssetError("agent guardrails.policies must be a mapping")
            for preset_id in contribution.policies:
                preset = policies.get(preset_id)
                if preset is None:
                    raise ExtensionAssetError(f"policy preset {preset_id!r} is unavailable")
                if set(merged).intersection(preset.policies):
                    raise ExtensionAssetError("policy presets cannot override existing policies")
                merged.update(preset.policies)
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        lock_path = destination / LOCK_FILE
        if lock_path.exists():
            raise ExtensionAssetError(f"{LOCK_FILE} is reserved for the extension manager")
        lock_path.write_text(
            json.dumps({"api_version": 1, "extensions": dict(sorted(versions.items()))}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        load(destination, expand_env=False, enforce_handler_allowlist=True)
        content = pack_directory(destination)
    return ResolvedAgentContribution(
        manifest.id,
        contribution.id,
        contribution.title,
        manifest.version,
        content,
        hashlib.sha256(content).hexdigest(),
    )
