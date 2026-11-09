"""Shared pi gateway configuration for the mock-LLM e2e modules."""

from __future__ import annotations

import json
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


def write_pi_resource_bundle(
    bundle: Path,
    *,
    model: str,
    markers: tuple[str, str, str, str],
    extension_suffix: str,
    sandbox: dict[str, object] | None,
) -> None:
    """Write project resources outside the session workspace for real Pi tests."""
    prompt, extension, skill, context = markers
    bundle.mkdir(parents=True, exist_ok=True)
    spec: dict[str, object] = {
        "spec_version": 1,
        "name": "pi-bundle-probe",
        # Explicit instructions isolate Pi's context loading from spec auto-adoption.
        "prompt": prompt,
        "skills": "all",
        "executor": {"model": model, "config": {"harness": "pi"}},
    }
    if sandbox is not None:
        spec["os_env"] = {"type": "caller_process", "cwd": ".", "sandbox": sandbox}
    files = {
        "config.yaml": yaml.safe_dump(spec),
        f".pi/extensions/marker/index.{extension_suffix}": (
            (
                "module.exports = function (pi) {\n"
                if extension_suffix == "js"
                else "export default function (pi) {\n"
            )
            + '  pi.on("before_agent_start", async event => ({\n'
            f"    systemPrompt: event.systemPrompt + {json.dumps(chr(10) + chr(10) + extension)}\n"
            "  }));\n}\n"
        ),
        ".pi/skills/grilling/SKILL.md": (
            f"---\nname: grilling\ndescription: {skill} grilling tips\n---\n# Grilling\n"
        ),
        "AGENTS.md": context,
    }
    for name, content in files.items():
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
