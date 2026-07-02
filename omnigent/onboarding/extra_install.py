"""Shared helper for installing optional pip extras (cursor, antigravity, copilot).

Each SDK harness ships as an optional extra (``omnigent[cursor]``, etc.).  The
install command depends on *how* omnigent itself was installed:

* **``uv tool``** — the package lives in an isolated tool environment that
  ``uv pip install`` cannot reach.  A registry install reinstalls with the
  extra via ``uv tool install --with "omnigent[extra]" omnigent --force``; a
  git-source install (``uv tool install git+…``) instead reinstalls from that
  same source (``uv tool install --force "omnigent[extra] @ git+…"``) so the
  tool is not silently re-pulled from PyPI.
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

    .. note::

       Misses custom layouts set via ``UV_TOOL_DIR`` / ``XDG_DATA_HOME``
       (falls through to ``uv pip install``, same accepted gap as the pipx
       heuristic).
    """
    return "uv/tools/" in sys.prefix.replace(os.sep, "/")


def _installed_vcs_url() -> str | None:
    """Return the VCS URL omnigent was installed from, or ``None``.

    Reads the distribution's ``direct_url.json`` (PEP 610) via
    :func:`omnigent.update_check._read_installed_wheel_info`, which normalizes
    the URL to the ``git+…`` form pip/uv accept back as an install target (and
    repairs an SSH user the installer redacted to ``****``). Returns ``None``
    for plain registry installs, so callers fall back to a bare ``omnigent``
    target.
    """
    from omnigent.update_check import _read_installed_wheel_info

    info = _read_installed_wheel_info()
    return info.vcs_url if info is not None else None


def extra_install_command(extra: str) -> list[str]:
    """Return the argv that installs *extra* into the running environment.

    Detects the install method and picks the right tool:

    1. ``uv tool`` install, git source → ``uv tool install --force
       "omnigent[extra] @ git+…"`` (keeps the tool on its original source)
    2. ``uv tool`` install, registry → ``uv tool install --with ...
       omnigent --force``
    3. ``uv`` on PATH (non-tool) → ``uv pip install "omnigent[extra]"``
    4. fallback → ``<sys.executable> -m pip install "omnigent[extra]"``

    :param extra: The pip extra name, e.g. ``"cursor"``.
    :returns: The install argv.
    """
    target = f"omnigent[{extra}]"

    if _is_uv_tool_install():
        vcs_url = _installed_vcs_url()
        if vcs_url is not None:
            # Git-source tool install: reinstall from the same source with the
            # extra attached, else a bare ``omnigent`` re-pulls it from PyPI.
            return ["uv", "tool", "install", "--force", f"{target} @ {vcs_url}"]
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
