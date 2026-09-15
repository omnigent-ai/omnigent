"""Parser tests for host-side ACP CLI model discovery (fixtures from real output)."""

from __future__ import annotations

import json

from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.harnesses.acp_cli_models import (
    _parse_bullet_text,
    _parse_devin_json,
    discover_acp_cli_models,
)

# Trimmed from a live `devin models list --format json`.
_DEVIN_JSON = json.dumps(
    {
        "families": [
            {
                "family_label": "Claude Opus 5",
                "family_uid": "claude-opus-5",
                "aliases": ["opus"],
                "variants": [
                    {
                        "model_uid": "claude-opus-5-high",
                        "label": "Claude Opus 5 High",
                        "max_context_tokens": 1000000,
                        "max_output_tokens": 128000,
                        "cost_tier": "High cost",
                    },
                    {"model_uid": "claude-opus-5-low", "label": "Claude Opus 5 Low"},
                ],
            },
            {"family_label": "Broken", "variants": [{"label": "no uid"}, "not-a-dict"]},
        ]
    }
)

# Trimmed from a live `grok models`.
_GROK_TEXT = """You are logged in with grok.com.

Default model: grok-4.6

Available models:
  * grok-4.6 (default)
  - grok-4.5
"""


def test_devin_json_maps_variants_to_rows() -> None:
    rows = _parse_devin_json(_DEVIN_JSON)
    assert [r["id"] for r in rows] == ["claude-opus-5-high", "claude-opus-5-low"]
    high = rows[0]
    assert high["displayName"] == "Claude Opus 5 High"
    assert high["group"] == "Claude Opus 5"
    assert high["context_window"] == 1000000
    assert high["max_output_tokens"] == 128000
    assert high["cost_tier"] == "High cost"
    # A variant without a model_uid, and a non-dict entry, are skipped, not fatal.
    assert all("id" in r and r["id"] for r in rows)


def test_grok_bullet_text_extracts_ids_without_default_suffix() -> None:
    rows = _parse_bullet_text(_GROK_TEXT)
    # `(default)` is dropped; the "Default model:" and header lines are ignored.
    assert [r["id"] for r in rows] == ["grok-4.6", "grok-4.5"]
    assert all(r["displayName"] == r["id"] for r in rows)


def test_bullet_text_dedupes() -> None:
    assert _parse_bullet_text("* a\n- a\n* b\n") == [
        {"id": "a", "displayName": "a"},
        {"id": "b", "displayName": "b"},
    ]


def test_discover_returns_empty_when_no_command_declared() -> None:
    # The generic behavior: a row with no models_argv discovers nothing (no crash).
    class _Row:
        models_argv = ()
        models_format = "none"
        binary = "nope"

    assert discover_acp_cli_models("x", _Row()) == []  # type: ignore[arg-type]


def test_catalog_rows_declare_known_formats() -> None:
    # devin/grok must declare a format the parser table knows; jcode may stay bare.
    from omnigent.harnesses.acp_cli_models import _PARSERS

    for name in ("devin", "grok"):
        row = ACP_CLI_HARNESSES[name]
        assert row.models_argv, f"{name} should declare a models command"
        assert row.models_format in _PARSERS, f"{name} format {row.models_format!r} has no parser"


def test_discover_never_raises_on_unexpected_error(monkeypatch) -> None:
    # The module contract is "never raises → []". A CLI emitting undecodable bytes
    # would raise UnicodeDecodeError (a ValueError) from the decode; the broad catch
    # (plus errors="replace") must degrade to [] rather than escape to the caller.
    import omnigent.harnesses.acp_cli_models as mod

    def boom(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert discover_acp_cli_models("grok", ACP_CLI_HARNESSES["grok"]) == []
