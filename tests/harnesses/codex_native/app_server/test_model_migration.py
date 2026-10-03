"""Model migration tests for Codex app server."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]


def test_codex_model_upgrade_target_reads_catalog_migration() -> None:
    """The runner records the exact old-to-new mapping Codex advertises."""
    from omnigent.harnesses.codex_native.app_server import _codex_model_upgrade_target

    catalog = {
        "models": [
            {"slug": "gpt-5.4", "upgrade": {"model": "gpt-5.6-terra"}},
            {"slug": "current", "upgrade": None},
        ]
    }

    assert _codex_model_upgrade_target(catalog, "gpt-5.4") == "gpt-5.6-terra"
    assert _codex_model_upgrade_target(catalog, "current") is None
    assert _codex_model_upgrade_target(catalog, "missing") is None


def test_codex_model_upgrade_target_reads_gateway_model_list_migration() -> None:
    """Gateway-aware ``model/list`` rows carry the same migration target."""
    from omnigent.harnesses.codex_native.app_server import _codex_model_upgrade_target

    rows = [
        {
            "id": "databricks-gpt-5-4",
            "model": "gpt-5.4",
            "upgrade": "gpt-5.6-terra",
            "upgradeInfo": {"model": "gpt-5.6-terra"},
        }
    ]

    assert _codex_model_upgrade_target(rows, "gpt-5.4") == "gpt-5.6-terra"


def test_acknowledge_codex_model_migration_updates_private_config(tmp_path: Path) -> None:
    """Acknowledgement preserves user notices while suppressing one prompt."""
    from omnigent.harnesses.codex_native.app_server import _acknowledge_codex_model_migration

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text("[notice]\nhide_rate_limit_model_nudge = true\n", encoding="utf-8")

    _acknowledge_codex_model_migration(codex_home, "gpt-5.4", "gpt-5.6-terra")

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["notice"]["hide_rate_limit_model_nudge"] is True
    assert config["notice"]["model_migrations"] == {"gpt-5.4": "gpt-5.6-terra"}
