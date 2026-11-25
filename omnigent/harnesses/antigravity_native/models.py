"""Live model discovery for the native Antigravity (``agy``) harness."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping
from typing import TypedDict

from omnigent.errors import OmnigentError
from omnigent.harnesses.antigravity_native.launch import agy_binary_path

# Discovery must finish inside the host model-options request deadline (15s),
# including the termination grace below, or the result is lost to a 504.
_DISCOVERY_TIMEOUT_S = 10.0
# Time a timed-out discovery tree gets to exit after SIGTERM before SIGKILL.
_TERMINATE_GRACE_S = 2.0

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


def _signal_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal *process* and everything it spawned, falling back to the process."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        else:
            process.send_signal(sig)
    except ProcessLookupError:
        pass


def _run_discovery(
    argv: list[str], *, env: dict[str, str] | None, timeout_s: float
) -> subprocess.CompletedProcess[str]:
    """Run a discovery command, removing its whole process tree on timeout.

    The Databricks route runs agy under a gateway wrapper; killing only the
    wrapper would leave agy running, so the command gets its own session.

    :param argv: Command to run.
    :param env: Complete environment, or ``None`` to inherit.
    :param timeout_s: Maximum time to wait for the command.
    :returns: The completed process with captured text output.
    :raises subprocess.TimeoutExpired: If the command outlives *timeout_s*.
    :raises subprocess.CalledProcessError: If the command exits non-zero.
    """
    with subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _signal_process_group(process, signal.SIGTERM)
            try:
                process.communicate(timeout=_TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                _signal_process_group(process, signal.SIGKILL)
                process.communicate()
            # Reap anything the wrapper left behind in the group.
            _signal_process_group(process, signal.SIGKILL)
            raise subprocess.TimeoutExpired(argv, timeout_s) from None
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, argv, stdout, stderr)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def list_agy_cli_model_options(
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = _DISCOVERY_TIMEOUT_S,
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

            argv = wrap_agy_gateway_launch(argv, overrides)
        run_env: dict[str, str] | None = {**os.environ, **overrides} if overrides else None
    else:
        run_env = dict(env)
    completed = _run_discovery(argv, env=run_env, timeout_s=timeout_s)
    return parse_agy_model_options(completed.stdout)
