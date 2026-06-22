"""Tests for :mod:`omnigent.onboarding.ucode_cleanup`.

The fixture configs mirror what ucode actually writes (see ucode's
``agents/codex.py`` legacy overlay and ``agents/claude.py`` MCP entry), so
the strips are exercised against the real on-disk shapes — not invented
ones.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
import tomllib

from omnigent.errors import OmnigentError
from omnigent.onboarding.ucode_cleanup import (
    UcodeWiringRemoval,
    remove_ucode_sidecars,
    remove_ucode_web_search_mcp,
    remove_ucode_wiring,
    strip_ucode_codex_config,
)

# What ucode's legacy (codex < 0.134.0) layout leaves in the user's real
# ~/.codex/config.toml, deep-merged into pre-existing user settings. The
# user-owned parts (comment, top-level model/approval_policy, the
# "personal" profile, the "kimi" provider) must survive the strip.
_LEGACY_UCODE_CONFIG = textwrap.dedent(
    """\
    # kept: user comment
    model = "moonshotai/kimi-k2.5"
    approval_policy = "never"
    profile = "ucode"

    [profiles.ucode]
    model_provider = "ucode-databricks"
    model = "gpt-5.4"

    [profiles.personal]
    model_provider = "kimi"

    [model_providers.kimi]
    name = "Moonshot"
    base_url = "https://api.moonshot.ai/v1"

    [model_providers.ucode-databricks]
    name = "Databricks AI Gateway"
    base_url = "https://example.databricks.com/ai-gateway/codex/v1"
    wire_api = "responses"

    [model_providers.ucode-databricks.http_headers]
    User-Agent = "ucode/1.0 codex/0.130.0"

    [model_providers.ucode-databricks.auth]
    command = "sh"
    args = ["-c", "databricks auth token"]
    """
)


def test_strip_removes_ucode_keys_and_preserves_user_config(tmp_path: Path) -> None:
    """The strip removes exactly ucode's keys and nothing the user owns.

    This is the core fix for "removing all configs doesn't help": the
    ``profile = "ucode"`` selector is what keeps bare ``codex`` routing
    through the workspace gateway, and nothing else removes it.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(_LEGACY_UCODE_CONFIG, encoding="utf-8")

    assert strip_ucode_codex_config(config_path) is True

    text = config_path.read_text(encoding="utf-8")
    doc = tomllib.loads(text)
    # The top-level profile selector is gone — bare codex no longer
    # activates ucode's profile. If present, the strip missed the one key
    # that causes the reported bug.
    assert "profile" not in doc
    # ucode's tables are gone wholesale (including nested http_headers /
    # auth subtables), while the user's own profile and provider survive
    # with their exact values — a wrong value here means the strip
    # rewrote user-owned data.
    assert doc["profiles"] == {"personal": {"model_provider": "kimi"}}
    assert doc["model_providers"] == {
        "kimi": {"name": "Moonshot", "base_url": "https://api.moonshot.ai/v1"}
    }
    assert doc["model"] == "moonshotai/kimi-k2.5"
    assert doc["approval_policy"] == "never"
    # tomlkit round-trip preserved the user's comment — a missing comment
    # means we rewrote the file destructively instead of editing it.
    assert "# kept: user comment" in text


def test_strip_keeps_foreign_profile_selector(tmp_path: Path) -> None:
    """A ``profile`` pointing at the user's own profile is never touched.

    The selector is only ucode's when it equals ``"ucode"`` — if the user
    re-pointed it after ucode wrote it, removing it would break their setup.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        textwrap.dedent(
            """\
            profile = "personal"

            [profiles.ucode]
            model_provider = "ucode-databricks"

            [profiles.personal]
            model_provider = "kimi"
            """
        ),
        encoding="utf-8",
    )

    # ucode's profile table was still present, so the file changed …
    assert strip_ucode_codex_config(config_path) is True
    doc = tomllib.loads(config_path.read_text(encoding="utf-8"))
    # … but the user's selector survived: only the "ucode" value is ours.
    assert doc["profile"] == "personal"
    assert doc["profiles"] == {"personal": {"model_provider": "kimi"}}


def test_strip_without_ucode_keys_leaves_file_byte_identical(tmp_path: Path) -> None:
    """A config ucode never touched is not rewritten at all.

    Byte-identity (not just semantic equality) proves we don't churn the
    file's formatting on the common no-op path.
    """
    original = '# untouched\nmodel = "gpt-5.4"\n'
    config_path = tmp_path / "config.toml"
    config_path.write_text(original, encoding="utf-8")

    assert strip_ucode_codex_config(config_path) is False
    assert config_path.read_text(encoding="utf-8") == original


def test_strip_missing_file_returns_false(tmp_path: Path) -> None:
    """A missing config is a no-op — and is not created as a side effect."""
    config_path = tmp_path / "config.toml"
    assert strip_ucode_codex_config(config_path) is False
    assert not config_path.exists()


def test_strip_drops_empty_parent_tables(tmp_path: Path) -> None:
    """A config that was *only* ucode's strips down to nothing.

    Leftover empty ``[profiles]`` / ``[model_providers]`` headers would be
    harmless to codex but confusing residue for the user.
    """
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        textwrap.dedent(
            """\
            profile = "ucode"

            [profiles.ucode]
            model_provider = "ucode-databricks"

            [model_providers.ucode-databricks]
            base_url = "https://example.databricks.com/ai-gateway/codex/v1"
            """
        ),
        encoding="utf-8",
    )

    assert strip_ucode_codex_config(config_path) is True
    # Nothing of ucode's remains, including the now-empty parent tables.
    assert tomllib.loads(config_path.read_text(encoding="utf-8")) == {}


def test_strip_malformed_toml_raises_and_leaves_file(tmp_path: Path) -> None:
    """An unparseable config fails loud instead of being rewritten.

    Rewriting a file we failed to parse could destroy the user's config —
    the error tells them what to remove by hand.
    """
    original = 'profile = "ucode\n'  # unterminated string
    config_path = tmp_path / "config.toml"
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(OmnigentError, match="not valid TOML"):
        strip_ucode_codex_config(config_path)
    # The broken file is exactly as we found it.
    assert config_path.read_text(encoding="utf-8") == original


def test_remove_sidecars_deletes_only_existing(tmp_path: Path) -> None:
    """Existing sidecars are deleted and reported; missing ones are skipped."""
    codex_sidecar = tmp_path / "ucode.config.toml"
    codex_sidecar.write_text('model_provider = "ucode-databricks"\n', encoding="utf-8")
    claude_sidecar = tmp_path / "ucode-settings.json"  # never created

    removed = remove_ucode_sidecars([codex_sidecar, claude_sidecar])

    # Only the file that existed is reported — a claude_sidecar entry here
    # would mean we claim to have deleted something that wasn't there.
    assert removed == [codex_sidecar]
    assert not codex_sidecar.exists()


def _ucode_web_search_entry_by_env() -> dict[str, object]:
    """Build ucode's web_search MCP entry as ucode registers it (env marker).

    :returns: The user-scope ``mcpServers.web_search`` value ucode writes
        via ``claude mcp add-json``.
    """
    return {
        "type": "stdio",
        "command": "/some/cache/dir/bin/ucode-binary-elsewhere",
        "args": ["mcp", "web-search"],
        "env": {
            "DATABRICKS_HOST": "https://example.databricks.com",
            "UCODE_WEB_SEARCH_MODEL": "databricks-gpt-5-4",
        },
    }


def _ucode_web_search_entry_by_command() -> dict[str, object]:
    """Build a ucode web_search entry recognizable only by its binary name.

    :returns: An entry whose ``command`` basename is ``ucode`` but whose
        ``env`` lacks the ``UCODE_WEB_SEARCH_MODEL`` marker.
    """
    return {
        "type": "stdio",
        "command": "/usr/local/bin/ucode",
        "args": ["mcp", "web-search"],
        "env": {},
    }


def _user_owned_web_search_entry() -> dict[str, object]:
    """Build a ``web_search`` entry the user registered themselves.

    :returns: An entry with no ucode markers — must never be removed.
    """
    return {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "some-web-search-mcp"],
        "env": {"SEARCH_API_KEY": "sk-user"},
    }


@pytest.mark.parametrize(
    "entry_builder",
    [_ucode_web_search_entry_by_env, _ucode_web_search_entry_by_command],
)
def test_remove_web_search_removes_ucode_owned_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_builder: Callable[[], dict[str, object]],
) -> None:
    """A ucode-owned ``web_search`` entry is detected and removal delegated.

    Both ownership markers are recognized: the ``UCODE_WEB_SEARCH_MODEL``
    env var and a command whose basename is ``ucode``. A False here means
    ucode's stale MCP entry (pointing at a uvx cache path that may no
    longer exist) survives the cleanup.
    """
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"mcpServers": {"web_search": entry_builder()}}), encoding="utf-8"
    )
    cli_calls: list[bool] = []
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_cleanup._remove_web_search_via_claude_cli",
        lambda: cli_calls.append(True) or True,
    )

    assert remove_ucode_web_search_mcp(claude_json) is True
    # Removal went through the claude CLI exactly once (the same interface
    # ucode registered the entry with).
    assert cli_calls == [True]


@pytest.mark.parametrize(
    "claude_json_content",
    [
        # A web_search server the user registered themselves — no ucode markers.
        json.dumps({"mcpServers": {"web_search": _user_owned_web_search_entry()}}),
        # No web_search entry at all.
        json.dumps({"mcpServers": {"other": {"command": "npx"}}}),
        # No mcpServers block.
        json.dumps({"projects": {}}),
        # Corrupt JSON — Claude Code owns this file; we must not guess.
        "{not json",
    ],
)
def test_remove_web_search_never_touches_non_ucode_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_json_content: str
) -> None:
    """The claude CLI is never invoked unless a ucode-owned entry is found.

    Removing a user's own ``web_search`` server (or acting on a file we
    can't parse) would be exactly the kind of invasive edit this module
    exists to undo.
    """
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(claude_json_content, encoding="utf-8")

    def _must_not_run() -> bool:
        raise AssertionError(
            "claude mcp remove was invoked for a web_search entry ucode does not own"
        )

    monkeypatch.setattr(
        "omnigent.onboarding.ucode_cleanup._remove_web_search_via_claude_cli",
        _must_not_run,
    )

    assert remove_ucode_web_search_mcp(claude_json) is False


def test_remove_web_search_missing_file_returns_false(tmp_path: Path) -> None:
    """No ``~/.claude.json`` means nothing to do (fresh machine / no Claude)."""
    assert remove_ucode_web_search_mcp(tmp_path / ".claude.json") is False


def test_remove_ucode_wiring_composes_all_cleanups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The orchestrator cleans a realistically-wired HOME end to end.

    Sets up a home directory exactly as ``ucode configure`` leaves it for a
    legacy-codex user (shared config edited in place + both sidecars) and
    verifies every artifact is gone afterwards. No ``~/.claude.json`` is
    created, so the web_search step reports False without reaching the
    claude CLI.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_LEGACY_UCODE_CONFIG, encoding="utf-8")
    (codex_dir / "ucode.config.toml").write_text(
        'model_provider = "ucode-databricks"\n', encoding="utf-8"
    )
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "ucode-settings.json").write_text("{}", encoding="utf-8")

    result = remove_ucode_wiring()

    assert result == UcodeWiringRemoval(
        codex_config_stripped=True,
        removed_sidecars=[
            codex_dir / "ucode.config.toml",
            claude_dir / "ucode-settings.json",
        ],
        web_search_mcp_removed=False,
    )
    assert result.any_change is True
    # On-disk state matches the report: sidecars gone, shared config kept
    # but stripped of the ucode profile selector.
    assert not (codex_dir / "ucode.config.toml").exists()
    assert not (claude_dir / "ucode-settings.json").exists()
    doc = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
    assert "profile" not in doc


def test_remove_ucode_wiring_clean_machine_reports_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a machine ucode never touched, the cleanup is a pure no-op."""
    monkeypatch.setenv("HOME", str(tmp_path))

    result = remove_ucode_wiring()

    assert result == UcodeWiringRemoval(
        codex_config_stripped=False, removed_sidecars=[], web_search_mcp_removed=False
    )
    assert result.any_change is False
