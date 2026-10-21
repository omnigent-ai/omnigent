"""Install Pi's shared adapter for the MLflow gateway Chat wire format."""

import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit


def pi_gateway_extension_args(agent_dir: Path, models_config: Mapping[str, object]) -> list[str]:
    """Stage the protocol adapter for generated MLflow Chat providers.

    :param agent_dir: This session's generated Pi configuration directory.
    :param models_config: Generated models.json contents.
    :returns: Explicit extension arguments, or none for other transports.
    """
    configured = models_config.get("providers")
    if not isinstance(configured, dict):
        return []
    providers = [
        name
        for name, provider in configured.items()
        if isinstance(name, str)
        and isinstance(provider, dict)
        and provider.get("api") == "openai-completions"
        and isinstance(base_url := provider.get("baseUrl"), str)
        and urlsplit(base_url).path.rstrip("/").endswith("/ai-gateway/mlflow/v1")
    ]
    if not providers:
        return []
    extension = agent_dir / "omnigent_gateway.js"
    extension.write_text(
        files("omnigent.resources.pi_gateway").joinpath(extension.name).read_text(),
        encoding="utf-8",
    )
    (agent_dir / "omnigent_gateway_providers.json").write_text(
        json.dumps(providers), encoding="utf-8"
    )
    return ["--extension", str(extension)]
