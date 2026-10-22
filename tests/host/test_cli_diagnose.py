"""CLI-level tests for ``omnigent diagnose`` (command wiring + output shape)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import respx
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.version import VERSION

_BASE = "http://localhost:6767"

# Shape mirrors the real GET /v1/info (omnigent/server/app.py): a local
# single-user server reports accounts_enabled=false, single_user=true.
_INFO = {
    "accounts_enabled": False,
    "single_user": True,
    "login_url": None,
    "server_version": VERSION,
}


def _patch_server(base_url: str | None) -> Any:
    """Patch server resolution so the CLI targets *base_url* without spawning one."""
    return patch("omnigent.cli._resolve_attach_server", return_value=base_url)


@respx.mock
def test_diagnose_json_with_server() -> None:
    """--json against a reachable server reports its version + derived auth mode."""
    respx.get(f"{_BASE}/v1/info").mock(return_value=httpx.Response(200, json=_INFO))

    with _patch_server(_BASE):
        result = CliRunner().invoke(cli, ["diagnose", "--server", _BASE, "--json"])

    assert result.exit_code == 0, result.output
    snap = json.loads(result.output)
    assert snap["cli_version"] == VERSION
    assert snap["server_version"] == VERSION
    assert snap["server_url"] == _BASE
    assert snap["auth_source"] == "single_user"
    assert snap["auth_source_origin"] == "server"


@respx.mock
def test_diagnose_human_output() -> None:
    """Default (non-JSON) output renders the expected labeled lines."""
    respx.get(f"{_BASE}/v1/info").mock(return_value=httpx.Response(200, json=_INFO))

    with _patch_server(_BASE):
        result = CliRunner().invoke(cli, ["diagnose", "--server", _BASE])

    assert result.exit_code == 0, result.output
    assert f"cli     {VERSION}" in result.output
    assert f"server  {VERSION}" in result.output
    assert "auth    single_user (server)" in result.output


def test_diagnose_local_only_no_server() -> None:
    """With no resolvable server, server fields are empty and auth is local-env."""
    with _patch_server(None):
        result = CliRunner().invoke(cli, ["diagnose", "--json"])

    assert result.exit_code == 0, result.output
    snap = json.loads(result.output)
    assert snap["cli_version"] == VERSION
    assert snap["server_version"] is None
    assert snap["server_url"] is None
    assert snap["auth_source_origin"] == "local-env"
