"""Carry an exact Codex model choice separately from an unresolved alias."""

from __future__ import annotations

from omnigent.models.model_override import validate_model_override


class ExactCodexModel(str):
    """A caller-selected provider ID to use verbatim, without model discovery."""

    def __new__(cls, value: str) -> ExactCodexModel:
        return super().__new__(cls, validate_model_override(value))
