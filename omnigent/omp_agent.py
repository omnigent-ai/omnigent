"""Built-in omp (Oh My Pi) agent spec.

omp is driven over the Agent Client Protocol (``omp acp``) through the ``omp``
harness (see :mod:`omnigent.inner.acp_harness` +
``omnigent.runtime.workflow._build_omp_spawn_env``, which pins the command).
This module materializes the thin launcher spec the server seeds as a built-in
so "Oh My Pi" appears in the New Chat picker with no user ``acp:`` config.

omp owns its own file/shell tools and authenticates with its own ``~/.omp``
credentials, so the spec declares neither an ``os_env`` block nor
``executor.auth`` — mirroring the other self-authenticating ACP agents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# The built-in agent name (the New Chat picker label and the stable builtin id).
OMP_AGENT_NAME = "omp"

_PROMPT = (
    "You are Oh My Pi (omp), running over ACP inside Omnigent. Help the user "
    "with software engineering and system-debugging tasks — read and edit "
    "files, run shell commands, and reach local network resources when asked. "
    "Keep responses concise and prefer showing diffs and commands over prose."
)


def _omp_agent_spec() -> dict[str, Any]:
    """Return the built-in omp agent spec as a plain dict."""
    return {
        "name": OMP_AGENT_NAME,
        "description": (
            "Oh My Pi (omp) coding agent, driven over the Agent Client Protocol "
            "via `omp acp`. Runs its own file/shell tools and authenticates with "
            "its own ~/.omp credentials (e.g. Ollama Cloud)."
        ),
        "executor": {"harness": "omp"},
        "prompt": _PROMPT,
    }


def materialize_omp_agent_spec(tmpdir: Path) -> Path:
    """
    Write the built-in omp agent spec YAML into *tmpdir*.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to the generated ``omp.yaml`` spec.
    """
    yaml_path = tmpdir / "omp.yaml"
    yaml_path.write_text(yaml.safe_dump(_omp_agent_spec(), sort_keys=False), encoding="utf-8")
    return yaml_path
