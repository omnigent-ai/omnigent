"""Tests for ``omnigent.onboarding.claude_profiles`` (issue #503).

Covers the config-block loader and the name→``config_dir`` resolver that
the runner / spawn-env builder relies on. No subprocess, no CLI, no
credentials — the module owns no secrets and neither do these tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.onboarding.claude_profiles import (
    ClaudeProfile,
    active_default_claude_profile,
    claude_profile_by_name,
    claude_profiles_list,
    load_claude_fanout_pool,
    load_claude_profiles,
    resolve_claude_profile_config_dir,
)


def _config(
    *,
    profiles: list[dict[str, str]] | None = None,
    active_default: str | None = None,
    fanout_pool: list[str] | None = None,
) -> dict[str, object]:
    """Build a ``claude_profiles`` config block inline."""
    block: dict[str, object] = {"profiles": profiles or []}
    if active_default is not None:
        block["active_default"] = active_default
    if fanout_pool is not None:
        block["fanout_pool"] = fanout_pool
    return {"claude_profiles": block}


# ── load_claude_profiles ────────────────────────────────────────────


def test_load_returns_profiles_in_declared_order() -> None:
    """Profiles load in YAML order with expanded config_dir paths."""
    cfg = _config(
        profiles=[
            {"name": "work", "config_dir": "/var/claude/work", "display": "Work"},
            {"name": "personal", "config_dir": "~/claude/personal", "display": "Personal"},
        ]
    )
    profiles = load_claude_profiles(cfg)
    assert [p.name for p in profiles] == ["work", "personal"]
    assert profiles[0].config_dir == "/var/claude/work"
    # ``~`` is expanded to the home directory.
    assert profiles[1].config_dir == str(Path("~/claude/personal").expanduser())
    assert profiles[1].display == "Personal"


def test_load_empty_when_block_absent() -> None:
    """No ``claude_profiles`` block → empty list (harness falls back to ~/.claude)."""
    assert load_claude_profiles({}) == []


def test_load_empty_when_block_not_a_dict() -> None:
    """A malformed block (e.g. a bare string) is ignored, not fatal."""
    assert load_claude_profiles({"claude_profiles": "oops"}) == []


def test_load_empty_when_profiles_not_a_list() -> None:
    assert load_claude_profiles({"claude_profiles": {"profiles": "not-a-list"}}) == []


def test_load_skips_entries_missing_name_or_config_dir() -> None:
    """An entry without a name or config_dir is dropped; valid siblings survive."""
    cfg = _config(
        profiles=[
            {"name": "good", "config_dir": "/tmp/good"},
            {"name": "", "config_dir": "/tmp/no-name"},  # empty name dropped
            {"name": "no-dir"},  # missing config_dir dropped
            {"config_dir": "/tmp/no-name-field"},  # missing name dropped
            "not-a-dict",  # non-dict dropped
        ]
    )
    profiles = load_claude_profiles(cfg)
    assert [p.name for p in profiles] == ["good"]


def test_load_non_string_display_falls_back_to_none() -> None:
    """A non-string ``display`` is coerced to None (then to name at the endpoint)."""
    cfg = _config(profiles=[{"name": "work", "config_dir": "/tmp/work", "display": 123}])
    profiles = load_claude_profiles(cfg)
    assert profiles[0].display is None


# ── active_default / by_name / resolve ──────────────────────────────


def test_active_default_returns_configured_name() -> None:
    cfg = _config(
        profiles=[{"name": "work", "config_dir": "/tmp/work"}],
        active_default="work",
    )
    assert active_default_claude_profile(cfg) == "work"


def test_active_default_none_when_unset() -> None:
    cfg = _config(profiles=[{"name": "work", "config_dir": "/tmp/work"}])
    assert active_default_claude_profile(cfg) is None


def test_active_default_none_when_block_absent() -> None:
    assert active_default_claude_profile({}) is None


def test_profile_by_name_returns_match() -> None:
    cfg = _config(profiles=[{"name": "work", "config_dir": "/tmp/work"}])
    assert claude_profile_by_name("work", cfg) == ClaudeProfile(
        name="work", config_dir="/tmp/work", display=None
    )


def test_profile_by_name_unknown_returns_none() -> None:
    cfg = _config(profiles=[{"name": "work", "config_dir": "/tmp/work"}])
    assert claude_profile_by_name("missing", cfg) is None


def test_resolve_explicit_name_to_config_dir() -> None:
    cfg = _config(profiles=[{"name": "personal", "config_dir": "~/claude/personal"}])
    assert resolve_claude_profile_config_dir("personal", cfg) == str(
        Path("~/claude/personal").expanduser()
    )


def test_resolve_unknown_name_returns_none() -> None:
    """Unknown name → None → caller leaves CLAUDE_CONFIG_DIR unset (default ~/.claude)."""
    cfg = _config(profiles=[{"name": "work", "config_dir": "/tmp/work"}])
    assert resolve_claude_profile_config_dir("ghost", cfg) is None


def test_resolve_none_name_uses_active_default() -> None:
    """``None`` name falls back to ``active_default`` (the per-agent spec default path)."""
    cfg = _config(
        profiles=[{"name": "work", "config_dir": "/tmp/work"}],
        active_default="work",
    )
    assert resolve_claude_profile_config_dir(None, cfg) == "/tmp/work"


def test_resolve_none_name_with_no_default_returns_none() -> None:
    cfg = _config(profiles=[{"name": "work", "config_dir": "/tmp/work"}])
    assert resolve_claude_profile_config_dir(None, cfg) is None


# ── claude_profiles_list (secret-free) ──────────────────────────────


def test_list_returns_only_name_and_display() -> None:
    """The server endpoint shape: name + display only, never config_dir."""
    cfg = _config(
        profiles=[
            {"name": "work", "config_dir": "/secret/work", "display": "Work"},
            {"name": "personal", "config_dir": "/secret/personal"},  # no display
        ]
    )
    items = claude_profiles_list(cfg)
    assert items == [
        {"name": "work", "display": "Work"},
        {"name": "personal", "display": "personal"},  # display falls back to name
    ]
    # No item leaks the config_dir path.
    assert all("config_dir" not in item for item in items)


def test_list_empty_when_no_profiles() -> None:
    assert claude_profiles_list({}) == []


# ── global config isolation ($OMNIGENT_CONFIG_HOME) ─────────────────


def test_load_reads_global_config_respecting_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``load_claude_profiles()`` with no arg reads ``~/.omnigent/config.yaml``
    via ``$OMNIGENT_CONFIG_HOME`` — the same isolation path the spawn-env
    tests use, so the runner's resolver works in tests without touching the
    developer's real config.
    """
    cfg_home = tmp_path / "omnigent"
    cfg_home.mkdir()
    (cfg_home / "config.yaml").write_text(
        "claude_profiles:\n"
        "  active_default: work\n"
        "  profiles:\n"
        "    - name: work\n"
        "      config_dir: /tmp/work\n"
        "      display: Work\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(cfg_home))
    profiles = load_claude_profiles()
    assert [p.name for p in profiles] == ["work"]
    assert active_default_claude_profile() == "work"
    assert resolve_claude_profile_config_dir("work") == "/tmp/work"


# ── load_claude_fanout_pool (issue #692) ─────────────────────────────


def test_fanout_pool_returns_declared_order() -> None:
    """Pool names load in declared order so round-robin is deterministic."""
    cfg = _config(
        profiles=[
            {"name": "work", "config_dir": "/tmp/work"},
            {"name": "personal", "config_dir": "/tmp/personal"},
            {"name": "clientb", "config_dir": "/tmp/clientb"},
        ],
        fanout_pool=["work", "personal", "clientb"],
    )
    assert load_claude_fanout_pool(cfg) == ["work", "personal", "clientb"]


def test_fanout_pool_drops_unknown_names() -> None:
    """A name not backed by a configured profile is silently dropped
    (a partial pool is still useful; a typo must not break every spawn)."""
    cfg = _config(
        profiles=[
            {"name": "work", "config_dir": "/tmp/work"},
            {"name": "personal", "config_dir": "/tmp/personal"},
        ],
        fanout_pool=["work", "ghost", "personal"],
    )
    assert load_claude_fanout_pool(cfg) == ["work", "personal"]


def test_fanout_pool_dedupes_repeated_names() -> None:
    """A repeated name appears once (round-robin must not double-assign)."""
    cfg = _config(
        profiles=[
            {"name": "work", "config_dir": "/tmp/work"},
            {"name": "personal", "config_dir": "/tmp/personal"},
        ],
        fanout_pool=["work", "work", "personal"],
    )
    assert load_claude_fanout_pool(cfg) == ["work", "personal"]


def test_fanout_pool_empty_when_block_absent() -> None:
    """No ``claude_profiles`` block → empty pool → fan-out disabled (today's behavior)."""
    assert load_claude_fanout_pool({}) == []


def test_fanout_pool_empty_when_field_absent() -> None:
    """Block present but no ``fanout_pool:`` → empty pool (fan-out off)."""
    cfg = _config(profiles=[{"name": "work", "config_dir": "/tmp/work"}])
    assert load_claude_fanout_pool(cfg) == []


def test_fanout_pool_empty_when_not_a_list() -> None:
    """A malformed ``fanout_pool`` (e.g. a bare string) is ignored, not fatal."""
    block: dict[str, object] = {
        "profiles": [{"name": "work", "config_dir": "/tmp/work"}],
        "fanout_pool": "work",  # not a list — ignored
    }
    assert load_claude_fanout_pool({"claude_profiles": block}) == []


def test_fanout_pool_drops_non_string_entries() -> None:
    """Non-string entries (numbers, nulls) are dropped; valid siblings survive."""
    block: dict[str, object] = {
        "profiles": [{"name": "work", "config_dir": "/tmp/work"}],
        "fanout_pool": ["work", 123, None, ""],  # only "work" is a valid name
    }
    assert load_claude_fanout_pool({"claude_profiles": block}) == ["work"]


def test_fanout_pool_empty_when_all_names_unknown() -> None:
    """A pool of only unknown names → empty (fan-out disabled, not an error)."""
    cfg = _config(
        profiles=[{"name": "work", "config_dir": "/tmp/work"}],
        fanout_pool=["ghost1", "ghost2"],
    )
    assert load_claude_fanout_pool(cfg) == []
