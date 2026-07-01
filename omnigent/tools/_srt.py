"""Shared ``srt`` sandbox wrap for subprocess-based tool execution.

Callers that spawn subprocesses and want OS-level sandboxing use
:func:`wrap_with_srt` to prepend ``srt`` when it's installed and
sandboxing is enabled, or pass the command through unchanged
otherwise.

One in-tree consumer today:

- :class:`~omnigent.tools.local.LocalPythonTool` — wraps its
  ``python _runner.py`` spawn. Stateful tools pass a per-call
  ``settings_file`` path that whitelists their ToolState dir for
  writes.

Stdio MCP servers (``omnigent/tools/mcp.py``) used to go
through this helper too, but the wrap was removed in step 7 of
the harness contract migration — srt's default policy blocks
outbound network, which broke every useful MCP server (Glean,
Slack, GitHub, UC, etc. all need outbound HTTPS). Stdio MCPs
now spawn unsandboxed, matching the legacy inner stack at
``omnigent/inner/mcp_tools.py`` (which has never sandboxed
stdio MCPs). Future per-MCP sandboxing — if reintroduced —
should flow through the ``omnigent/environments/`` primitive
with explicit outbound-host allowlists, not srt-defaults.

The PTY-mode wrap used by :class:`~omnigent.terminals.shell.Shell`
is a different shape (srt's Node library API + the ``_srt_shell.mjs``
wrapper, not the ``srt`` CLI) and is not covered here — that path
needs a PTY-compatible entry, which the ``srt -c`` CLI doesn't
provide.
"""

from __future__ import annotations

import shlex
import shutil


class SandboxUnavailableError(RuntimeError):
    """
    Raised when sandboxing is *required* but the ``srt`` runtime is missing.

    This is the fail-closed signal: a deployment that demands OS-level
    sandboxing (``sandbox_enabled=True`` AND ``sandbox_required=True``)
    must never silently downgrade to a plain, full-privilege subprocess
    when ``srt`` is absent from ``PATH``. :func:`wrap_with_srt` raises
    this instead of returning the unwrapped command so the caller
    refuses to run untrusted tool code rather than running it
    unsandboxed.

    Callers that spawn spec-author code (e.g.
    :class:`~omnigent.tools.local.LocalPythonTool`) translate this into
    a clear tool-level error so the refusal is visible to the agent and
    operators rather than crashing the whole run.
    """


def is_srt_available() -> bool:
    """
    Return ``True`` when the ``srt`` CLI is on ``PATH``.

    Separate from :func:`wrap_with_srt` so callers can probe once
    at construction time (cheap on module import) and cache the
    result — the wrap itself runs on every subprocess spawn and
    shouldn't hit ``shutil.which`` each time.

    :returns: Whether ``srt`` resolves via the current ``PATH``.
    """
    return shutil.which("srt") is not None


def wrap_with_srt(
    cmd: list[str],
    *,
    sandbox_enabled: bool,
    srt_available: bool,
    settings_file: str | None = None,
    sandbox_required: bool = False,
) -> list[str]:
    """
    Prepend ``srt`` to *cmd* when sandboxing is enabled AND available.

    Behaviour by (``sandbox_enabled``, ``srt_available``,
    ``sandbox_required``):

    - ``sandbox_enabled=False`` → return *cmd* unchanged (explicit
      opt-out; never wrap, never raise).
    - enabled, ``srt_available=True`` → wrap with ``srt``.
    - enabled, ``srt_available=False``, ``sandbox_required=False`` →
      return *cmd* unchanged. This is the **documented dev escape
      hatch**: dev boxes / CI without ``srt`` installed still run the
      plain subprocess so they remain usable.
    - enabled, ``srt_available=False``, ``sandbox_required=True`` →
      raise :class:`SandboxUnavailableError` (**fail closed**). A
      deployment that demands sandboxing must never silently downgrade
      to a full-privilege subprocess just because ``srt`` is missing or
      renamed on ``PATH``.

    Historically this helper failed *open* in the missing-``srt`` case
    regardless of intent, so a deployment that believed it was
    sandboxing could silently run untrusted spec-author code with full
    host privileges. ``sandbox_required`` closes that hole while
    keeping the dev fallback opt-in.

    When wrapping, the returned form is ``srt [-s <settings>] -c
    <quoted>``. The ``-c`` flag takes a single quoted command string
    (like ``bash -c``), so *cmd* is joined with :func:`shlex.join`
    to preserve word boundaries / embedded spaces.

    :param cmd: The unwrapped command argv, e.g.
        ``["python", "/path/to/_runner.py"]`` or
        ``["npx", "some-mcp-server", "--flag"]``.
    :param sandbox_enabled: Operator-level opt-in for sandboxing
        this caller. The spec-level knob
        (:attr:`SandboxConfig.enabled`, etc.) flows into this arg.
    :param srt_available: Whether ``srt`` is on ``PATH``. Typically
        cached from :func:`is_srt_available` at construction time.
    :param settings_file: Absolute path to a per-call srt settings
        JSON file, or ``None`` for the default srt sandbox.
        Settings files are used when a caller needs a writable path
        that srt's defaults deny (e.g. :class:`LocalPythonTool`'s
        per-call ToolState directory). MCP stdio callers usually
        pass ``None`` — the MCP server lives inside its own
        filesystem expectations and srt's permissive read defaults
        + the caller's ``cwd`` cover the common case.
    :param sandbox_required: When ``True``, a missing ``srt`` while
        sandboxing is enabled is fatal — the function raises
        :class:`SandboxUnavailableError` instead of returning *cmd*
        unwrapped. Defaults to ``False`` to preserve the dev
        fail-open fallback.
    :returns: The wrapped command argv, or *cmd* unchanged when
        not wrapping.
    :raises SandboxUnavailableError: If sandboxing is enabled and
        required but ``srt`` is not available.
    """
    if not sandbox_enabled:
        return cmd
    if not srt_available:
        if sandbox_required:
            raise SandboxUnavailableError(
                "Sandboxing is required (sandbox_enabled=True, "
                "sandbox_required=True) but the 'srt' sandbox runtime is not "
                "available on PATH. Refusing to run the command unsandboxed. "
                "Install 'srt' (or restore it to PATH); set "
                "sandbox_required=False only for trusted dev environments to "
                "allow the unsandboxed fallback."
            )
        return cmd
    base = ["srt"]
    if settings_file is not None:
        base += ["-s", settings_file]
    return [*base, "-c", shlex.join(cmd)]
