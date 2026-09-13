"""Live model discovery for the native Antigravity (``agy``) harness."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import TypedDict

from omnigent.harnesses.antigravity_native.launch import agy_binary_path


class AntigravityModelOption(TypedDict):
    """One picker row emitted by ``agy models``."""

    id: str
    displayName: str
    isDefault: bool


def parse_agy_model_options(output: str) -> list[AntigravityModelOption]:
    """Parse the tab-separated rows emitted by ``agy models``.

    ``agy`` writes a progress preamble before its data rows.  Ignore every
    non-tabbed line so additions to that preamble do not become picker choices.

    :param output: Combined standard output from ``agy models``.
    :returns: Stable, de-duplicated model picker rows.
    :raises ValueError: If the command returned no valid model rows.
    """
    rows: list[AntigravityModelOption] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        model_id, separator, display_name = raw_line.partition("\t")
        if not separator:
            continue
        model_id = model_id.strip()
        display_name = display_name.strip()
        if not model_id or not display_name or model_id in seen:
            continue
        seen.add(model_id)
        rows.append(
            {
                "id": model_id,
                "displayName": display_name,
                # ``agy models`` advertises no default marker.  Do not infer
                # one from ordering; agy remains the source of that choice.
                "isDefault": False,
            }
        )
    if not rows:
        raise ValueError("agy model list did not contain any valid models")
    return rows


def list_agy_cli_model_options(
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 10.0,
) -> list[AntigravityModelOption]:
    """Discover launchable picker rows from the installed ``agy`` CLI.

    :param env: Optional process environment override for tests and host probes.
    :param timeout_s: Maximum time to wait for the CLI to enumerate models.
    :returns: The models currently advertised by the signed-in ``agy`` CLI.
    :raises FileNotFoundError: If ``agy`` cannot be resolved.
    :raises subprocess.SubprocessError: If the command fails or times out.
    :raises ValueError: If the command emits no valid data rows.
    """
    try:
        executable = agy_binary_path()
    except RuntimeError as exc:
        # ``model_catalog`` treats OSError as an unavailable CLI and reports a
        # retryable, secret-free fallback.  Keep this discovery helper inside
        # that contract instead of exposing the install command to tool output.
        raise FileNotFoundError("agy CLI is unavailable") from exc
    completed = subprocess.run(
        [executable, "models"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=dict(env) if env is not None else None,
    )
    return parse_agy_model_options(completed.stdout)
