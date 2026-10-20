"""Author and configure trusted team extensions without loading their server code."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import click
import yaml
from packaging.version import Version

from omnigent.extensions import ExtensionManifest, ExtensionValidationError, plugin_state
from omnigent.extensions.assets import ExtensionAssetError
from omnigent.extensions.conformance import prepare_extension
from omnigent.extensions.registry import _require_text, _validate_extension_id
from omnigent.spec.parser import _ConfigYamlLoader
from omnigent.version import VERSION


def _installed_manifest(extension_id: str) -> ExtensionManifest:
    manifest = plugin_state().get(extension_id)
    if manifest is None:
        raise click.ClickException(f"Extension {extension_id!r} is not installed or was rejected")
    return manifest


def _write_setting(config_path: Path, extension_id: str, enabled: bool) -> None:
    """Replace only one extension grant, preserving unrelated server settings."""
    _validate_extension_id(extension_id)
    if config_path.is_symlink():
        raise click.ClickException("Refusing to replace a symlinked configuration file")
    settings = {}
    if config_path.exists():
        settings = yaml.load(config_path.read_text(encoding="utf-8"), Loader=_ConfigYamlLoader)
        if not isinstance(settings, dict):
            raise click.ClickException("Server config must be a YAML mapping")
    extensions = settings.setdefault("extensions", {})
    if not isinstance(extensions, dict):
        raise click.ClickException("Server config extensions must be a mapping")
    existing = extensions.get(extension_id, {})
    if not isinstance(existing, dict):
        raise click.ClickException("Existing extension settings must be a mapping")
    updated = dict(existing)
    updated["enabled"] = enabled
    if enabled:
        manifest = _installed_manifest(extension_id)
        updated["version"] = f"=={manifest.version}"
        updated["permissions"] = sorted(item.value for item in manifest.permissions)
    else:
        updated.pop("required", None)
    extensions[extension_id] = updated
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=config_path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            yaml.safe_dump(settings, temporary, sort_keys=False)
        os.replace(temporary_path, config_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _scaffold(extension_id: str, owner: str, directory: Path) -> None:
    _validate_extension_id(extension_id)
    _require_text(owner, "owner")
    if directory.exists():
        raise click.ClickException(f"Destination already exists: {directory}")
    distribution = f"omnigent-{extension_id.replace('.', '-')}"
    package = distribution.replace("-", "_")
    agent_id = f"{extension_id}.assistant"
    base_version = Version(VERSION).base_version
    files = {
        "pyproject.toml": f'''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = {json.dumps(distribution)}
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["omnigent>={VERSION},<1"]

[project.entry-points."omnigent.extensions"]
extension = "{package}.plugin:get_manifest"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
{package} = ["agents/**/*"]
''',
        f"src/{package}/__init__.py": '"""Team-owned Omnigent extension."""\n',
        f"src/{package}/plugin.py": f'''"""Declarative team-extension manifest."""

from omnigent.extensions import AgentContribution, ExtensionManifest


def get_manifest() -> ExtensionManifest:
    return ExtensionManifest(
        id={extension_id!r},
        display_name={extension_id!r},
        owner={owner!r},
        distribution={distribution!r},
        version="0.1.0",
        requires_omnigent=">={base_version},<1",
        extension_api=1,
        agents=(AgentContribution({agent_id!r}, "Team assistant", "agents/assistant"),),
    )
''',
        f"src/{package}/agents/assistant/config.yaml": yaml.safe_dump(
            {
                "spec_version": 1,
                "name": agent_id.replace(".", "-"),
                "description": "A team-owned assistant with portable bundled skills.",
                "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            },
            sort_keys=False,
        ),
        f"src/{package}/agents/assistant/AGENTS.md": (
            "Help the team investigate its task. Use the bundled team-context skill.\n"
        ),
        f"src/{package}/agents/assistant/skills/team-context/SKILL.md": (
            "---\nname: team-context\ndescription: Read the team's working conventions.\n---\n\n"
            "Ask for missing project context before making external changes.\n"
        ),
        "tests/test_package.py": f'''from pathlib import Path

from omnigent.extensions import check_extension_package
from {package}.plugin import get_manifest


def test_extension_package():
    root = Path(__file__).parents[1]
    check_extension_package(
        get_manifest(), project_root=root, package_root=root / "src" / "{package}"
    )
''',
        "README.md": f"""# {extension_id}

Install this package into the same environment as Omnigent:

```sh
uv pip install -e .
omni extensions check {extension_id}
omni extensions enable {extension_id} --config ./server.yaml
omni server --config ./server.yaml
```

Enablement pins the installed version and grants its declared permissions.
Keep configuration operator-owned; Python extensions run with full process trust.
Use `omni extensions export {extension_id} {agent_id} --output assistant.tar.gz`
to obtain a portable agent bundle. Extract it and run `omni run ./assistant`.
Restart the server after configuration or package changes.
""",
    }
    directory.mkdir(parents=True)
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def register(group: click.Group) -> None:
    """Attach authoring and activation commands to the existing extension CLI."""

    @group.command("init")
    @click.argument("extension_id")
    @click.option("--owner", required=True, help="Team accountable for this extension.")
    @click.option("--path", "directory", required=True, type=click.Path(path_type=Path))
    def init_extension(extension_id: str, owner: str, directory: Path) -> None:
        """Create a separately publishable agent-pack project and conformance test."""
        try:
            _scaffold(extension_id, owner, directory)
        except (ExtensionValidationError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Created {extension_id} in {directory}")
        click.echo(f"Install: uv pip install -e {directory}")
        click.echo(f"Validate: omni extensions check {extension_id}")

    @group.command("check")
    @click.argument("extension_id")
    def check_extension(extension_id: str) -> None:
        """Validate installed agent packs, policy presets, dependencies, and browser assets."""
        from omnigent.extensions.assets import resolve_bundle

        state = plugin_state()
        try:
            manager = prepare_extension(state, extension_id)
            manifest = manager.active[extension_id]
            if manifest.entrypoints.browser is not None:
                resolve_bundle(manifest, package=state.asset_package(extension_id))
        except (ExtensionAssetError, ValueError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"{extension_id}: ok")
        click.echo(f"agents: {len(manifest.agents)}, policy presets: {len(manifest.policies)}")
        click.echo("Server entrypoint: validated without activation")

    @group.command("export")
    @click.argument("extension_id")
    @click.argument("agent_id")
    @click.option("--output", required=True, type=click.Path(path_type=Path))
    def export_agent(extension_id: str, agent_id: str, output: Path) -> None:
        """Export an immutable portable agent bundle without activating server code."""
        try:
            manager = prepare_extension(plugin_state(), extension_id)
            agent = manager.agents.get(agent_id)
            if agent is None or agent.extension_id != extension_id:
                raise click.ClickException("Agent is not contributed by this extension")
            with output.open("xb") as stream:
                stream.write(agent.content)
        except (ExtensionAssetError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Exported {agent.id} {agent.version} to {output} (sha256:{agent.digest})")

    def configure(extension_id: str, config_path: Path, enabled: bool) -> None:
        try:
            _write_setting(config_path, extension_id, enabled)
        except (ExtensionValidationError, OSError, yaml.YAMLError) as exc:
            raise click.ClickException(str(exc)) from exc
        verb = "Enabled" if enabled else "Disabled"
        click.echo(f"{verb} {extension_id} in {config_path}; restart the server to apply")

    @group.command("enable")
    @click.argument("extension_id")
    @click.option("--config", "config_path", required=True, type=click.Path(path_type=Path))
    def enable_extension(extension_id: str, config_path: Path) -> None:
        """Approve this installed version and its declared permissions in operator config."""
        configure(extension_id, config_path, True)

    @group.command("disable")
    @click.argument("extension_id")
    @click.option("--config", "config_path", required=True, type=click.Path(path_type=Path))
    def disable_extension(extension_id: str, config_path: Path) -> None:
        """Disable an extension's contributions and runtime on the next server start."""
        configure(extension_id, config_path, False)
