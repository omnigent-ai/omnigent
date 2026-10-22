"""CLI controls for explicit shared and machine preference intent."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote
from uuid import UUID

import click
import httpx
import yaml
from filelock import Timeout

from omnigent.config import load_global_config
from omnigent.preference_connection import (
    PreferenceConnection,
    connect_preferences,
    load_connection,
    open_preference_http,
)
from omnigent.preference_sync import SharedPreferenceKey
from omnigent.preference_workspace import ImportPreview


def _key(text: str) -> SharedPreferenceKey:
    if text in ("model", "opencode_model"):
        return SharedPreferenceKey(kind="model", name=text)
    if text == "harness":
        return SharedPreferenceKey(kind="harness", name="default")
    if text.startswith("args/"):
        return SharedPreferenceKey(kind="harness_args", name=text.removeprefix("args/"))
    parts = text.split("/")
    if len(parts) == 4 and parts[0] == "plugin":
        return SharedPreferenceKey(
            kind="plugin",
            name=dict(
                zip(
                    ("ecosystem", "source", "name"),
                    map(unquote, parts[1:]),
                    strict=True,
                )
            ),
        )
    raise ValueError(
        "Use model, opencode_model, harness, args/HARNESS, or plugin/ECOSYSTEM/SOURCE/NAME"
    )


class _PreferenceGroup(click.Group):
    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except (ValueError, OSError, sqlite3.Error, Timeout, httpx.HTTPError) as exc:
            raise click.ClickException(str(exc)) from exc


@click.group("preferences", cls=_PreferenceGroup)
@click.option("--server", help="Server API URL; defaults to your configured server.")
@click.pass_context
def preferences(ctx: click.Context, server: str | None) -> None:
    """Inspect, import and explicitly save workspace preferences.

    Values are JSON: strings need quotes, [] clears arguments, false disables a
    plugin, and null removes an override. Session switches never call these commands.
    """
    selected = server or load_global_config().get("server")
    if not isinstance(selected, str) or not selected:
        raise click.ClickException("Set --server or configure a server first")
    ctx.obj = selected.rstrip("/")


def _connection() -> PreferenceConnection:
    server = click.get_current_context().obj
    connection = load_connection(server)
    if connection is None:
        raise click.ClickException("Run `omnigent preferences --server URL connect` first")
    return connection


async def _sync(connection: PreferenceConnection) -> object:
    async with await open_preference_http(connection.server_url) as http:
        snapshot = await connection.workspace().client.sync(http)
    return snapshot.model_dump(mode="json")


def _print(value: object) -> None:
    click.echo(json.dumps(value, indent=2, ensure_ascii=False))


@preferences.command("connect")
@click.pass_obj
def connect(server: str) -> None:
    """Follow this authenticated workspace; never upload this machine's defaults."""
    connection = asyncio.run(connect_preferences(server))
    _print(connection.model_dump(mode="json"))


@preferences.command("show")
def show() -> None:
    """Inspect the local snapshot, machine overrides, pending edits and conflicts."""
    workspace = _connection().workspace()
    snapshot = workspace.client.cached()
    from omnigent.preference_workspace import shared_key

    _print(
        {
            "shared": snapshot.model_dump(mode="json") if snapshot else None,
            "machine": [
                {"key": shared_key(key).model_dump(mode="json"), "value": value}
                for key, value in workspace.machine_layer().values.items()
            ],
            "pending": [item.model_dump(mode="json") for item in workspace.client.pending()],
        }
    )


@preferences.command("sync")
def sync() -> None:
    """Retry queued edits and refresh shared defaults without changing a running session."""
    _print(asyncio.run(_sync(_connection())))


@preferences.command("set")
@click.option("--scope", type=click.Choice(["shared", "machine"]), required=True)
@click.option(
    "--resolve", multiple=True, type=click.UUID, help="Conflict ID to resolve explicitly."
)
@click.argument("key")
@click.argument("value")
def set_preference(scope: str, resolve: tuple[UUID, ...], key: str, value: str) -> None:
    """Save a JSON value at the named scope; null removes it to inherit again."""
    connection = _connection()
    workspace = connection.workspace()
    parsed = json.loads(value)
    if scope == "machine":
        if resolve:
            raise ValueError("Conflicts are resolved in the shared scope")
        workspace.set_machine(_key(key), parsed)
        click.echo("Saved on this machine.")
    else:
        operation_id = workspace.client.enqueue(_key(key), parsed, resolves=resolve)
        click.echo(f"Queued {operation_id}; retained locally if synchronization fails.", err=True)
        _print(asyncio.run(_sync(connection)))


@preferences.command("discard")
@click.argument("operation_id", type=click.UUID)
def discard(operation_id: UUID) -> None:
    """Abandon the first pending edit; this does not undo a possible server commit."""
    _print(_connection().workspace().client.discard(operation_id).model_dump(mode="json"))


@preferences.command("import")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def preview(source: Path) -> None:
    """Preview portable YAML defaults. Save stdout to a plan, then use `apply PLAN`."""
    config = yaml.safe_load(source.read_text())
    if not isinstance(config, dict):
        raise ValueError("Import source must be a YAML mapping")
    _print(_connection().workspace().preview_config(config).model_dump(mode="json"))


@preferences.command("restore")
def restore() -> None:
    """Preview undoing the last import; save stdout to a plan, then `apply PLAN`."""
    _print(_connection().workspace().preview_restore().model_dump(mode="json"))


@preferences.command("apply")
@click.argument("plan", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def apply(plan: Path) -> None:
    """Apply exactly a reviewed import/restore plan, retaining a recovery record."""
    connection = _connection()
    preview = ImportPreview.model_validate_json(plan.read_bytes())
    operations = connection.workspace().apply_import(preview)
    click.echo(f"Queued {len(operations)} edits; retained locally if sync fails.", err=True)
    _print(asyncio.run(_sync(connection)))
