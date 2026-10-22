"""Explicit cloud save/import using the existing local configuration files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import click
import httpx
import yaml
from filelock import FileLock, Timeout

from omnigent.cli_auth import open_server_client
from omnigent.cli_diagnostics import SUPPRESS_RECOVERY_HINT_ATTR
from omnigent.config import global_config_path, load_global_config
from omnigent.configuration_profile import (
    ConfigurationSave,
    ConfigurationSnapshot,
    PluginOptOuts,
    PortableConfiguration,
)
from omnigent.util.server_url import ServerUrl

_ROUTE = "/v1/preferences/configuration"


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _local(plugin_path: Path | None) -> PortableConfiguration:
    plugins = None
    if plugin_path is not None:
        raw = _read(plugin_path)
        plugins = (
            PluginOptOuts.model_validate_json(raw)
            if raw is not None
            else PluginOptOuts(opted_out=[], disable_all=False)
        )
    return PortableConfiguration.from_config(load_global_config(), plugin_opt_outs=plugins)


async def _request(
    server: str, operation: ConfigurationSave | None = None
) -> ConfigurationSnapshot:
    from omnigent.cli_auth import store_databricks_org_id
    from omnigent.runner._entry import _make_auth_token_factory

    resolved = ServerUrl.from_api_base(server)
    if resolved.is_workspace_hosted and resolved.org_id is not None:
        store_databricks_org_id(resolved.api_base, resolved.org_id)
    server = resolved.api_base

    factory = _make_auth_token_factory(
        server_url=server, _allow_initial_token=False, _allow_delegated_mint=False
    )
    bearer = await asyncio.to_thread(factory) if factory is not None else None
    async with open_server_client(server, bearer_token=bearer, timeout=10) as client:
        response = (
            await client.get(_ROUTE)
            if operation is None
            else await client.put(_ROUTE, json=operation.model_dump(mode="json"))
        )
        if response.status_code == 409:
            raise ValueError("Cloud configuration changed. Preview it again before saving.")
        response.raise_for_status()
        return ConfigurationSnapshot.model_validate_json(response.content)


class _CloudGroup(click.Group):
    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except (ValueError, OSError, Timeout, httpx.HTTPError, yaml.YAMLError) as exc:
            error = click.ClickException(str(exc))
            setattr(error, SUPPRESS_RECOVERY_HINT_ATTR, True)
            raise error from exc


@click.group("cloud", cls=_CloudGroup)
@click.option("--server", help="Server API URL; defaults to your configured server.")
@click.option(
    "--plugin-opt-outs",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Include this launcher's existing plugin opt-out JSON file.",
)
@click.pass_context
def cloud(ctx: click.Context, server: str | None, plugin_opt_outs: Path | None) -> None:
    """Save or import portable defaults. Later edits stay local until explicitly saved."""
    config = load_global_config()
    if not isinstance(config, dict):
        raise ValueError("Local configuration must be a mapping")
    selected = server or os.environ.get("OMNIGENT_SERVER") or config.get("server")
    if not isinstance(selected, str):
        raise ValueError("Set --server or configure a server first")
    parsed = urlsplit(selected)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.fragment
    ):
        raise ValueError("Use the server API URL without credentials or fragment")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query and (
        set(query) != {"o"}
        or len(query["o"]) != 1
        or not query["o"][0].isascii()
        or not query["o"][0].isdecimal()
        or not ServerUrl(selected).is_workspace_hosted
    ):
        raise ValueError("Only the numeric ?o= workspace selector is supported")
    ctx.obj = (selected.rstrip("/"), plugin_opt_outs)


def _preview(before: PortableConfiguration | None, after: PortableConfiguration | None) -> None:
    click.echo(
        json.dumps(
            {
                "before": before.model_dump() if before else None,
                "after": after.model_dump() if after else None,
            },
            indent=2,
        )
    )


@cloud.command("show")
@click.pass_obj
def show(options: tuple[str, Path | None]) -> None:
    """Show the saved snapshot without changing this machine."""
    click.echo(asyncio.run(_request(options[0])).model_dump_json(indent=2))


@cloud.command("save")
@click.option("--yes", is_flag=True, help="Confirm replacement without prompting.")
@click.pass_obj
def save(options: tuple[str, Path | None], yes: bool) -> None:
    """Review and save this machine's portable configuration to the cloud."""
    server, plugin_path = options
    current = asyncio.run(_request(server))
    local = _local(plugin_path)
    if plugin_path is None and current.configuration is not None:
        local = local.model_copy(update={"plugin_opt_outs": current.configuration.plugin_opt_outs})
    _preview(current.configuration, local)
    if not yes:
        click.confirm("Save this configuration to the cloud?", default=False, abort=True)
    saved = asyncio.run(
        _request(
            server,
            ConfigurationSave(
                identity=current.identity,
                expected_revision=current.revision,
                configuration=local,
            ),
        )
    )
    if saved.identity != current.identity or saved.configuration != local:
        raise ValueError("Server returned a different configuration; inspect it before retrying")
    click.echo(
        f"Saved revision {saved.revision}. Other machines change only when you import again."
    )


def _import_snapshot(
    snapshot: ConfigurationSnapshot,
    plugin_path: Path | None,
    originals: dict[Path, bytes | None],
) -> None:
    configuration = snapshot.configuration
    if configuration is None:
        raise ValueError("No configuration saved yet. Save from your existing machine first.")
    config_path = global_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(config_path) + ".import.lock", timeout=5):
        if any(_read(path) != original for path, original in originals.items()):
            raise ValueError(
                "Local configuration changed during preview; import again to review it"
            )
        updated = configuration.apply_to(load_global_config())
        updated["configuration_import"] = {
            "identity": snapshot.identity.model_dump(),
            "revision": snapshot.revision,
        }
        outputs = {config_path: yaml.safe_dump(updated, sort_keys=True).encode()}
        if plugin_path is not None and configuration.plugin_opt_outs is not None:
            outputs[plugin_path] = configuration.plugin_opt_outs.model_dump_json(indent=2).encode()
        for path in outputs:
            original = originals[path]
            if original is not None:
                digest = hashlib.sha256(original).hexdigest()[:16]
                backup = path.with_name(f"{path.name}.before-import-{digest}")
                _write_private(backup, original)
                click.echo(f"Backup: {backup}")
        written = []
        try:
            for path, payload in outputs.items():
                _write_private(path, payload)
                written.append(path)
        except OSError:
            for path in reversed(written):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _write_private(path, original)
            raise


@cloud.command("import")
@click.option("--yes", is_flag=True, help="Confirm replacement without prompting.")
@click.pass_obj
def import_configuration(options: tuple[str, Path | None], yes: bool) -> None:
    """Review and import a saved snapshot, retaining backups of overwritten files."""
    server, plugin_path = options
    paths = [global_config_path(), *([plugin_path] if plugin_path else [])]
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("Configuration and plugin opt-outs must use different files")
    originals = {path: _read(path) for path in paths}
    snapshot = asyncio.run(_request(server))
    if snapshot.configuration is None:
        raise ValueError("No configuration saved yet. Save from your existing machine first.")
    if plugin_path is None and snapshot.configuration.plugin_opt_outs is not None:
        click.echo("Plugin opt-outs are saved; use your launcher to import those too.", err=True)
    _preview(_local(plugin_path), snapshot.configuration)
    if not yes:
        click.confirm("Import these defaults on this machine?", default=False, abort=True)
    verified = asyncio.run(_request(server))
    if verified != snapshot:
        raise ValueError("Cloud configuration or login changed during preview; import again")
    _import_snapshot(snapshot, plugin_path, originals)
    click.echo("Imported. Later changes require an explicit save and import.")


def onboarding_marker(server: str) -> Path:
    """Locate this login's prompt receipt without contacting the server."""
    from omnigent.cli_auth import _load_entry

    resolved = ServerUrl.from_api_base(server)
    server = resolved.api_base
    entry = _load_entry(server) or {}
    account = {key: entry.get(key) for key in ("user_id", "auth_type", "workspace_host", "org_id")}
    if not entry.get("user_id"):
        account["token"] = entry.get("token")
    account.update(
        {
            key: os.environ.get(key)
            for key in (
                "DATABRICKS_TOKEN",
                "DATABRICKS_HOST",
                "DATABRICKS_CONFIG_PROFILE",
                "OMNIGENT_API_KEY",
            )
        }
    )
    digest = hashlib.sha256(
        json.dumps([server, resolved.org_id, account], sort_keys=True).encode()
    ).hexdigest()
    return global_config_path().parent / "configuration-import-prompts" / (digest + ".json")


@cloud.command("onboard")
@click.option("--check", is_flag=True, hidden=True)
@click.pass_context
def onboard(ctx: click.Context, check: bool) -> None:
    """Offer cloud save/import once per login on interactive startup."""
    if not check and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        return
    server, _ = ctx.obj
    marker = onboarding_marker(server)
    if check:
        click.echo("complete" if marker.exists() else "pending")
        return
    if marker.exists():
        return
    snapshot = asyncio.run(_request(server))
    prompt = (
        "Import your saved cloud configuration?"
        if snapshot.configuration is not None
        else "Save this machine's configuration for use on your other machines?"
    )
    click.echo("Configuration is copied once. Later changes require an explicit save and import.")
    if click.confirm(prompt, default=False):
        try:
            ctx.invoke(
                import_configuration if snapshot.configuration is not None else save, yes=False
            )
        except click.Abort:
            click.echo("Configuration left unchanged.")
    _write_private(
        onboarding_marker(server),
        json.dumps({"identity": snapshot.identity.model_dump()}).encode(),
    )
