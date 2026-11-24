"""Live model discovery for the native Antigravity (``agy``) harness."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from typing import TypedDict

from omnigent.errors import OmnigentError
from omnigent.harnesses.antigravity_native.launch import agy_binary_path

# Discovery through the Databricks gateway also resolves a token and the
# workspace's Gemini services before agy lists its models.
_GATEWAY_DISCOVERY_TIMEOUT_S = 30.0

# Orchestrator slugs carry a label (e.g. "Orchestrator (Carbon)") that does not
# identify the planner model they run, so they are left out of the catalog.
_ORCHESTRATOR_MODEL_PREFIX = "gemini-pro-orchestrator"


def _is_orchestrator_model(model_id: str) -> bool:
    return model_id == _ORCHESTRATOR_MODEL_PREFIX or model_id.startswith(
        f"{_ORCHESTRATOR_MODEL_PREFIX}-"
    )


class AntigravityModelOption(TypedDict):
    """One picker row emitted by ``agy models``."""

    id: str
    displayName: str
    isDefault: bool


def parse_agy_model_options(output: str) -> list[AntigravityModelOption]:
    """Parse the tab-separated rows emitted by ``agy models``.

    ``agy`` writes a progress preamble before its data rows.  Ignore every
    non-tabbed line so additions to that preamble do not become picker choices.
    ``gemini-pro-orchestrator*`` rows are excluded because their labels do not
    name the model they run.

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
        if _is_orchestrator_model(model_id):
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


def _launch_credential_environment() -> dict[str, str]:
    """Return the credential environment a native agy launch would inject.

    :returns: Gemini key/endpoint (and Databricks profile) overrides, or ``{}``
        when agy runs on its own login.
    :raises ValueError: If the saved configuration needs repair through setup.
    """
    from omnigent.harnesses.antigravity_native.credentials import (
        resolve_antigravity_credentials,
    )

    try:
        credentials = resolve_antigravity_credentials()
    except OmnigentError:
        # The message is secret-free, but a fixed one keeps tool output stable.
        raise ValueError(
            "Antigravity credentials need repair; run omni setup on the host"
        ) from None
    return credentials.environment() if credentials is not None else {}


def list_agy_cli_model_options(
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 10.0,
) -> list[AntigravityModelOption]:
    """Discover launchable picker rows from the installed ``agy`` CLI.

    Without *env*, discovery uses the same configured Gemini key, endpoint or
    Databricks gateway as a native launch, so the catalog matches what agy runs.

    :param env: Complete process environment override for tests and host probes.
    :param timeout_s: Maximum time to wait for the CLI to enumerate models.
    :returns: The models currently advertised by the ``agy`` CLI.
    :raises FileNotFoundError: If ``agy`` cannot be resolved.
    :raises subprocess.SubprocessError: If the command fails or times out.
    :raises ValueError: If the command emits no valid data rows, or saved
        credentials need repair.
    """
    try:
        executable = agy_binary_path()
    except RuntimeError as exc:
        # ``model_catalog`` treats OSError as an unavailable CLI and reports a
        # retryable, secret-free fallback.  Keep this discovery helper inside
        # that contract instead of exposing the install command to tool output.
        raise FileNotFoundError("agy CLI is unavailable") from exc
    argv = [executable, "models"]
    if env is None:
        overrides = _launch_credential_environment()
        if overrides:
            from omnigent.harnesses.antigravity_native.gateway import wrap_agy_gateway_launch

            wrapped = wrap_agy_gateway_launch(argv, overrides)
            if wrapped != argv:
                timeout_s = max(timeout_s, _GATEWAY_DISCOVERY_TIMEOUT_S)
            argv = wrapped
        run_env: dict[str, str] | None = {**os.environ, **overrides} if overrides else None
    else:
        run_env = dict(env)
    completed = subprocess.run(
        argv,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=run_env,
    )
    return parse_agy_model_options(completed.stdout)
