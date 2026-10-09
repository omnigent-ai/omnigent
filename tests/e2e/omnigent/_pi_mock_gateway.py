"""Shared pi gateway configuration for the mock-LLM e2e modules."""

from __future__ import annotations

from pathlib import Path

import yaml


def write_pi_gateway_config(config_home: Path, *, mock_url: str, model: str) -> None:
    """Point pi at the mock LLM via an OpenAI-key provider (gateway mode)."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "auth": {"type": "api_key"},
                "providers": {
                    "mock-oai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "models": {"default": model},
                        },
                    },
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
