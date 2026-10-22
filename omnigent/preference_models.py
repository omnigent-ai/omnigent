"""Native launch selection for sessions that adopted portable preferences."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from omnigent.config import load_global_config, load_local_config
from omnigent.preference_startup import inherited_launch


def adopted_model(
    harness: str,
    workspace: str | Path,
    *,
    session_model: str | None = None,
    spec_model: str | None = None,
    args: Sequence[str] = (),
) -> str | None:
    """Explicit session/spec choices precede frozen machine/shared defaults."""
    launch = inherited_launch()
    if launch is None:
        return session_model or spec_model
    explicit = None
    for index, arg in enumerate(args):
        if arg.startswith("--model="):
            explicit = arg.partition("=")[2]
        elif arg in ("--model", "-m") and index + 1 < len(args):
            explicit = args[index + 1]
    if explicit or session_model or spec_model:
        return explicit or session_model or spec_model
    project = load_local_config(Path(workspace) / ".omnigent/config.yaml")
    config = launch.resolve(load_global_config(), project).config
    value = (
        config.get("opencode_model") or config.get("model")
        if harness == "opencode-native"
        else config.get("model")
    )
    return value if isinstance(value, str) and value else None


def reject_model_substitution(harness: str, model: str) -> None:
    """Keep adopted intent intact when a native provider cannot serve it."""
    if inherited_launch() is not None:
        raise RuntimeError(
            f"{harness}: selected model {model!r} is unavailable on this machine/provider. "
            "Your saved choice is unchanged. Choose another model for this session "
            "or fix this machine's provider configuration, then retry."
        )


def adopted_model_args(
    harness: str,
    workspace: str | Path,
    args: Sequence[str],
    *,
    session_model: str | None = None,
) -> list[str]:
    """Supply a portable default only to native CLIs with a --model option."""
    result = list(args)
    if inherited_launch() is None:
        return result
    model = adopted_model(harness, workspace, session_model=session_model, args=args)
    if model and not any(arg in ("--model", "-m") or arg.startswith("--model=") for arg in args):
        result.extend(("--model", model))
    return result
