"""Shared helper for installing optional pip extras (cursor, antigravity, copilot).

Each SDK harness ships as an optional extra (``omnigent[cursor]``, etc.).  The
install command depends on *how* omnigent itself was installed:

* **``uv tool``** — the package lives in an isolated tool environment that
  ``uv pip install`` cannot reach, and ``uv tool install --force`` replaces the
  tool's recorded requirements instead of adding to them.  So the reinstall
  names the receipt's extras together with the new one (``uv tool install
  --force "omnigent[copilot,databricks]"``); a git-source install (``uv tool
  install git+…``) reinstalls from that same source (``uv tool install --force
  "omnigent[extra] @ git+…"``) so the tool is not silently re-pulled from PyPI.
* **``uv`` (non-tool)** — ``uv pip install --python <sys.executable>
  "omnigent[extra]"`` pins the install to the running interpreter.  The bare
  form instead resolves the *active* virtualenv, which fails outright on a
  Homebrew/pipx/system install that merely has ``uv`` on ``PATH`` ("No virtual
  environment found") and can silently target an unrelated venv when one is
  active.
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


def _recorded_uv_tool_extras() -> list[str]:
    """Return the extras the ``uv tool`` receipt records for omnigent.

    Reads ``uv-receipt.toml`` via
    :func:`omnigent.update_check._read_uv_tool_extras`. Empty when no readable
    receipt exists or no extras were requested.
    """
    from omnigent.update_check import _read_uv_tool_extras

    return _read_uv_tool_extras() or []


def extra_install_command(extra: str) -> list[str]:
    """Return the argv that installs *extra* into the running environment.

    Detects the install method and picks the right tool:

    1. ``uv tool`` install, git source → ``uv tool install --force
       "omnigent[extras] @ git+…"`` (keeps the tool on its original source)
    2. ``uv tool`` install, registry → ``uv tool install --force
       "omnigent[extras]"``
    3. ``uv`` on PATH (non-tool) → ``uv pip install --python <sys.executable>
       "omnigent[extra]"``
    4. fallback → ``<sys.executable> -m pip install "omnigent[extra]"``

    Both ``uv tool`` forms name the receipt's recorded extras alongside *extra*:
    ``--force`` replaces the tool's requirements, so an ``omnigent[databricks]``
    install would otherwise lose ``databricks`` while gaining *extra*. Other
    ``--with`` requirements the receipt records are not re-applied.

    :param extra: The pip extra name, e.g. ``"cursor"``.
    :returns: The install argv.
    """
    if _is_uv_tool_install():
        target = f"omnigent[{','.join(sorted({*_recorded_uv_tool_extras(), extra}))}]"
        vcs_url = _installed_vcs_url()
        if vcs_url is not None:
            # Git-source tool install: reinstall from the same source, else a
            # bare ``omnigent`` re-pulls it from PyPI.
            return ["uv", "tool", "install", "--force", f"{target} @ {vcs_url}"]
        return ["uv", "tool", "install", "--force", target]

    target = f"omnigent[{extra}]"
    if shutil.which("uv") is not None:
        # ``--python`` pins the install to the interpreter that will import the
        # extra.  Without it ``uv pip install`` requires an active virtualenv
        # and errors out on Homebrew/pipx/system installs that merely have uv
        # on PATH.
        return ["uv", "pip", "install", "--python", sys.executable, target]

    return [sys.executable, "-P", "-m", "pip", "install", target]


def extra_install_display(extra: str) -> str:
    """Return a human-readable command string for installing *extra*.

    Derived from :func:`extra_install_command` so the displayed text always
    matches what actually runs.

    :param extra: The pip extra name, e.g. ``"cursor"``.
    :returns: A shell-style command string.
    """
    import shlex

    return " ".join(shlex.quote(tok) for tok in extra_install_command(extra))
