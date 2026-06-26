"""Shared helper for installing optional pip extras (cursor, antigravity, copilot).

Each SDK harness ships as an optional extra (``omnigent[cursor]``, etc.).  The
install command depends on *how* omnigent itself was installed:

* **``uv tool``** — the package lives in an isolated tool environment that
  ``uv pip install`` cannot reach.  The correct command is
  ``uv tool install --with "omnigent[extra]" omnigent --force``.
* **``uv`` (non-tool)** — ``uv pip install "omnigent[extra]"`` targets the
  active virtualenv.
* **``pip`` / fallback** — ``<sys.executable> -m pip install "omnigent[extra]"``
  pins to the running interpreter.
"""

from __future__ import annotations

import os
import shutil
import sys


def _is_uv_tool_install() -> bool:
    """Return whether the running interpreter lives inside a ``uv tool`` venv.

    ``uv tool install`` creates per-tool environments under a platform-specific
    ``uv/tools/<package>/`` directory.  Checking ``sys.prefix`` for the
    ``uv/tools/`` segment mirrors the ``pipx/venvs`` heuristic in
    :func:`omnigent.update_check._looks_like_pipx_install`.
    """
    return "uv/tools/" in sys.prefix.replace(os.sep, "/")


def extra_install_command(extra: str) -> list[str]:
    """Return the argv that installs *extra* into the running environment.

    Detects the install method and picks the right tool:

    1. ``uv tool`` install → ``uv tool install --with ... omnigent --force``
    2. ``uv`` on PATH (non-tool) → ``uv pip install "omnigent[extra]"``
    3. fallback → ``<sys.executable> -m pip install "omnigent[extra]"``

    :param extra: The pip extra name, e.g. ``"cursor"``.
    :returns: The install argv.
    """
    target = f"omnigent[{extra}]"

    if _is_uv_tool_install():
        return ["uv", "tool", "install", "--with", target, "omnigent", "--force"]

    if shutil.which("uv") is not None:
        return ["uv", "pip", "install", target]

    return [sys.executable, "-m", "pip", "install", target]


def extra_install_display(extra: str) -> str:
    """Return a human-readable command string for installing *extra*.

    Derived from :func:`extra_install_command` so the displayed text always
    matches what actually runs.

    :param extra: The pip extra name, e.g. ``"cursor"``.
    :returns: A shell-style command string.
    """
    import shlex

    return " ".join(shlex.quote(tok) for tok in extra_install_command(extra))
