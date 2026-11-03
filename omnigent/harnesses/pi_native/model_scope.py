"""Resolve picker curation with the installed Pi CLI's own model resolver."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from omnigent._platform import resolve_cli_binary
from omnigent.harness_startup_config import resolve_harness_path

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopableModel:
    """One available model, without credentials or provider configuration."""

    provider: str
    model_id: str
    name: str | None = None

    @property
    def reference(self) -> str:
        """The canonical provider/model reference."""
        return f"{self.provider}/{self.model_id}"


def scope_models(patterns: Iterable[str], models: Sequence[ScopableModel]) -> list[ScopableModel]:
    """Use Pi's resolver without loading user extensions or accessing providers.

    :param patterns: Pi's enabledModels settings.
    :param models: Available catalog entries, in Pi's discovery order.
    :returns: Selected entries, or an empty scope when unmatched/unavailable.
        Callers retain Pi's full-catalog fallback for an empty scope.
    """
    if not models:
        return []
    executable = resolve_cli_binary(resolve_harness_path("pi-native") or "pi")
    node = shutil.which("node")
    if executable is None or node is None:
        _LOGGER.warning("Pi model curation unavailable: Pi and Node must be installed")
        return []
    cli = Path(executable).resolve()
    dist = next((parent for parent in cli.parents if parent.name == "dist"), cli.parent)
    resolver = dist / "core" / "model-resolver.js"
    try:
        indices = _scoped_indices(
            node,
            str(resolver),
            resolver.stat().st_mtime_ns,
            tuple(patterns),
            tuple(models),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        # Never include subprocess output: CLI diagnostics can contain local config.
        _LOGGER.warning("Pi model curation unavailable; using the unscoped catalog")
        return []
    return [models[index] for index in indices]


@lru_cache(maxsize=128)
def _scoped_indices(
    node: str,
    resolver: str,
    _resolver_mtime_ns: int,
    patterns: tuple[str, ...],
    models: tuple[ScopableModel, ...],
) -> tuple[int, ...]:
    """Cache successful resolutions by CLI revision, settings and catalog.

    :param node: Node executable.
    :param resolver: Installed Pi resolver module.
    :param _resolver_mtime_ns: Invalidates cached results after an in-place CLI upgrade.
    :param patterns: Model selection patterns.
    :param models: Credential-free catalog entries.
    :returns: Validated indices into the supplied catalog.
    :raises ValueError: If the helper's output does not identify catalog entries.
    """
    helper = files("omnigent.resources.pi_native").joinpath("resolve_model_scope.cjs")
    result = subprocess.run(
        [node, str(helper), resolver],
        input=json.dumps(
            {
                "patterns": patterns,
                "models": [
                    {
                        "provider": model.provider,
                        "id": model.model_id,
                        "name": model.name or model.model_id,
                    }
                    for model in models
                ],
            }
        ),
        capture_output=True,
        text=True,
        timeout=3,
        check=True,
    )
    indices = json.loads(result.stdout)
    if not isinstance(indices, list) or any(
        type(index) is not int or not 0 <= index < len(models) for index in indices
    ):
        raise ValueError("Pi resolver returned invalid catalog indices")
    return tuple(dict.fromkeys(indices))
