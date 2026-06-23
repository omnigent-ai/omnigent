"""Tests for the Claude Code profiles route (``GET /v1/claude-profiles``).

Issue #503: the new-session picker calls this to discover the operator's
configured ``claude_profiles`` entries. The endpoint returns only
``name`` + ``display`` — never ``config_dir`` or any credential.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest


async def test_list_claude_profiles_empty_when_none_configured(
    client: httpx.AsyncClient,
) -> None:
    """No ``claude_profiles`` block → empty list (picker hides itself)."""
    resp = await client.get("/v1/claude-profiles")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert body["data"] == []


async def test_list_claude_profiles_returns_names_and_display_only(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured profiles surface as ``{name, display}`` only — the
    ``config_dir`` path (which may be sensitive) never leaks."""
    cfg_home = tmp_path / "omnigent"
    cfg_home.mkdir()
    (cfg_home / "config.yaml").write_text(
        "claude_profiles:\n"
        "  active_default: work\n"
        "  profiles:\n"
        "    - name: work\n"
        "      config_dir: /secret/claude/work\n"
        "      display: Work (Anthropic)\n"
        "    - name: personal\n"
        "      config_dir: /secret/claude/personal\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(cfg_home))

    resp = await client.get("/v1/claude-profiles")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data == [
        {"name": "work", "display": "Work (Anthropic)"},
        {"name": "personal", "display": "personal"},  # display falls back to name
    ]
    # No entry leaks the config_dir path or any other field.
    for entry in data:
        assert set(entry.keys()) == {"name", "display"}
