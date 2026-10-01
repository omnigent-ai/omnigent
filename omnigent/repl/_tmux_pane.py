"""
Tmux pane integration for the REPL.

When the REPL boots inside a tmux pane (``$TMUX`` set), this module:

1. Marks the pane with custom options so other tooling can identify
   it as an omnigent pane and recover the launch args.
2. Discovers the user's existing prefix-table ``split-window`` /
   ``new-window`` bindings and rewrites each one with an
   ``if-shell -F`` wrapper that:

   - When the focused pane has the ``@omnigent-conv-id`` option set
     (i.e. it's an omnigent pane), runs ``omnigent pane-split``
     to launch the chooser in a new pane.
   - Otherwise, runs the user's exact original command unchanged.

The wrap-not-replace mechanism preserves bit-identical behavior in
non-omnigent panes (the else branch is the user's untouched
command), so the global mutation of the ``prefix`` table is
behaviorally invisible everywhere except inside an omnigent pane.

See ``designs/REPL_TMUX_PANE_SPLIT.md`` for the full design.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Master kill-switch for the tmux pane integration. When ``False``,
# :func:`register_pane` is a no-op — no pane options written, no
# split-window bindings wrapped — and the REPL behaves as if it
# weren't running inside tmux. Flip to ``True`` to re-enable.
#
# The feature ships disabled while the chooser UX is still being
# iterated on (see ``designs/REPL_TMUX_PANE_SPLIT.md``); turning
# it on globally requires un-toggling this constant. Existing
# wrappers in a running tmux server are not removed by this flag —
# they survive until the user restarts tmux or runs
# ``tmux unbind-key -T prefix '"'`` (etc.) by hand. That's fine
# for the disabled-default state because the wrappers' false
# branch is the user's exact original command, so non-omnigent
# panes keep working.
PANE_INTEGRATION_ENABLED = False

# Pane-option keys we set on the omnigent pane. Each is read by
# ``omnigent pane-picker`` to reconstruct the launch context for
# the new pane.
OPT_CONV_ID = "@omnigent-conv-id"
OPT_AGENT_NAME = "@omnigent-agent-name"
OPT_AGENT_YAML = "@omnigent-agent-yaml"
OPT_LAUNCH_ARGV = "@omnigent-launch-argv"
OPT_SERVER_URL = "@omnigent-server-url"

# Sentinel value for ``OPT_CONV_ID`` before the first conversation
# id is known. The wrapper's ``#{?#{@omnigent-conv-id},...}``
# truthiness check fires for any non-empty string, so this
# preserves "yes, this is an omnigent pane" even when there's no
# real conv id yet.
_PENDING_CONV_ID = "pending"

# Minimum tmux version required for reliable ``set-option -p``
# pane scope and the format-string evaluation primitives we use.
# 3.2 introduced pane-scoped hooks; 3.0 introduced ``-F`` mode for
# ``if-shell``. We pick 3.2 as the floor because the design also
# uses pane-scoped hooks elsewhere (future phases) and a single
# version gate is simpler than feature-by-feature.
_MIN_TMUX_VERSION = (3, 2)


@dataclass(frozen=True)
class SplitBinding:
    """
    One user-bound prefix-table binding that maps to a
    ``split-window`` or ``new-window`` invocation.

    :param key: The tmux key spec, e.g. ``'"'`` or ``'|'`` or ``'c'``.
    :param direction: One of ``'v'`` (vertical split), ``'h'``
        (horizontal split), or ``'w'`` (new window/tab) — the flag
        value passed to ``omnigent pane-split``.
    :param original_command: The full command string from the
        user's existing binding, e.g. ``'split-window -c
        "#{pane_current_path}"'`` — re-emitted verbatim in the
        wrapper's else branch so non-omnigent panes get
        bit-identical behavior.
    """

    key: str
    direction: str
    original_command: str


def _is_in_tmux() -> bool:
    """True when the REPL is running inside a tmux client."""
    return os.environ.get("TMUX") is not None


def _tmux_pane_id() -> str | None:
    """Return ``$TMUX_PANE`` (e.g. ``%0``), or ``None`` if unset."""
    return os.environ.get("TMUX_PANE")


def _tmux_version_ok() -> bool:
    """
    Check that the running tmux is recent enough for our integration.

    Parses ``tmux -V`` output (e.g. ``"tmux 3.4"`` or ``"tmux 3.2a"``)
    and compares the leading two integers against
    :data:`_MIN_TMUX_VERSION`. Suffix letters (``"a"`` in ``"3.2a"``)
    are ignored so a stable 3.2.x release counts as 3.2.
    """
    try:
        out = subprocess.run(
            ["tmux", "-V"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    parts = out.split()
    if len(parts) != 2:
        return False
    nums: list[int] = []
    for piece in parts[1].replace("-", ".").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if digits:
            nums.append(int(digits))
        if len(nums) >= 2:
            break
    return tuple(nums[:2]) >= _MIN_TMUX_VERSION


def _resolve_omnigent_argv() -> list[str]:
    """
    Resolve a callable argv prefix for invoking the running
    omnigent installation from a context that doesn't share our
    PATH (tmux's ``run-shell`` and ``split-window`` inherit the
    tmux server's environment, not the calling user's shell).

    Resolution order:

    1. ``sys.argv[0]`` is path-shaped (contains ``/``) — abspath
       it. Covers ``./.venv/bin/omnigent …`` and absolute-path
       invocations.
    2. ``sys.argv[0]`` is a bare name like ``"omnigent"`` — try
       :func:`shutil.which` against the running process's PATH.
       The Python process inherits PATH from the user's shell at
       launch, so an activated venv typically resolves correctly.
    3. Fallback — ``[sys.executable, "-m", "omnigent.cli.commands"]``.
       Always works because if Python is running this code, the
       ``omnigent.cli.commands`` module is importable. Trades aesthetics
       (the binding shows ``python -m omnigent.cli.commands``) for
       reliability across exotic launch shapes.

    :returns: A list of one or two argv elements. Length-1 means
        a single executable; length-3 means
        ``[python, "-m", "omnigent.cli.commands"]``. The caller appends
        the subcommand and its args (e.g. ``["pane-split", "-v",
        ...]``) to this prefix.
    """
    argv0 = sys.argv[0] if sys.argv else ""
    # When the process was launched via ``python -m omnigent.cli.commands``
    # (the chooser's fallback path), ``sys.argv[0]`` is the path to
    # ``cli.py`` itself — not a directly callable executable. Don't
    # try to exec it as a binary; round-trip through python -m again
    # so the recursive case (split → picker → new REPL → which
    # itself wants to install wrappers in tmux) keeps working.
    if argv0.endswith(".py"):
        return [sys.executable, "-m", "omnigent.cli.commands"]
    if argv0 and ("/" in argv0 or os.sep in argv0):
        # Path-shaped — make absolute. ``abspath`` handles relative
        # paths against the current working directory.
        return [os.path.abspath(argv0)]
    if argv0:
        resolved = shutil.which(argv0)
        if resolved:
            return [os.path.abspath(resolved)]
    # Last resort: invoke via the running interpreter. The
    # ``omnigent.cli.commands`` module is importable since we're running
    # inside it, so this exec form always succeeds.
    return [sys.executable, "-m", "omnigent.cli.commands"]


# User-facing click subcommand names that mark the start of the
# user's args inside a ``launch_argv``. Anything BEFORE the first
# match is the launcher prefix (the omnigent binary, ``python
# -m omnigent.cli.commands``, etc.) and gets stripped during
# normalization. Keep in sync with ``cli.py:_CLICK_SUBCOMMANDS``;
# duplicating the set here avoids importing ``cli`` from this
# module (which would create an import cycle through ``run_repl``).
_USER_FACING_SUBCOMMANDS = frozenset({"attach", "deploy", "run", "server", "version"})


def _user_args_after_launcher(launch_argv: list[str]) -> list[str]:
    """
    Slice *launch_argv* to drop everything before the user's
    first subcommand, returning just the user-args portion.

    The launcher prefix can take many forms across invocations:

    - ``[<omnigent-binary>, run, …]`` (PATH-resolved console script)
    - ``[<python>, -m, omnigent.cli.commands, run, …]`` (python-m fallback)
    - ``[<python>, -m, omnigent.cli.commands, -m, omnigent.cli.commands, run, …]``
      (doubled prefix from an earlier buggy register_pane)

    This walker doesn't try to recognize all of them
    individually; it just scans forward until it finds the first
    token in :data:`_USER_FACING_SUBCOMMANDS` and returns from
    there.

    :param launch_argv: ``sys.argv`` (or stored ``launch_argv``)
        of the running ``omnigent`` invocation.
    :returns: The user's portion of the argv. Empty list when
        no recognized subcommand is found (degenerate input —
        the caller writes the empty list to the pane option,
        and the picker errors clearly when it tries to exec).
    """
    for i, token in enumerate(launch_argv):
        if token in _USER_FACING_SUBCOMMANDS:
            return list(launch_argv[i:])
    return []


def _set_pane_option(pane_id: str, name: str, value: str) -> None:
    """Issue ``tmux set-option -p -t <pane_id> <name> <value>``."""
    subprocess.run(
        ["tmux", "set-option", "-p", "-t", pane_id, name, value],
        check=False,
        capture_output=True,
    )


def _unset_pane_options(pane_id: str) -> None:
    """
    Remove every ``@omnigent-*`` pane option this module sets.

    Used by :func:`register_pane`'s kill-switch path to un-mark a
    pane that may still have leftover options from a prior
    flag-on run. Once unmarked, any still-installed
    ``if-shell -F '#{?#{@omnigent-conv-id},...}'`` wrapper falls
    through to its else branch (the user's untouched original
    command), so split keys behave as if the integration never
    ran here.

    :param pane_id: Target pane, e.g. ``"%0"``.
    """
    for opt in (
        OPT_CONV_ID,
        OPT_AGENT_NAME,
        OPT_AGENT_YAML,
        OPT_LAUNCH_ARGV,
        OPT_SERVER_URL,
    ):
        subprocess.run(
            # ``-u`` unsets the option on the targeted pane.
            ["tmux", "set-option", "-p", "-t", pane_id, "-u", opt],
            check=False,
            capture_output=True,
        )


def _list_prefix_keys() -> list[str]:
    """Return raw lines from ``tmux list-keys -T prefix``, or ``[]``."""
    try:
        result = subprocess.run(
            ["tmux", "list-keys", "-T", "prefix"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return []
    return result.stdout.splitlines()


def _parse_bind_line(line: str) -> tuple[str, list[str]] | None:
    """
    Parse a tmux ``bind-key …`` line into ``(key, command-tokens)``.

    Tokenization uses :mod:`shlex` because tmux's output quotes
    keys like ``"`` and ``%`` with backslash escapes that shlex
    decodes correctly. Lines that fail to tokenize (lambda blocks
    with embedded braces, etc.) return ``None`` and are skipped
    by the caller.

    :param line: One line of output from ``tmux list-keys -T prefix``.
    :returns: ``(key, [cmd, *args])`` tuple, or ``None`` when the
        line isn't a parseable ``bind-key`` invocation.
    """
    try:
        toks = shlex.split(line)
    except ValueError:
        return None
    if not toks or toks[0] != "bind-key":
        return None
    i = 1
    # Skip flags. ``-T <table>`` and ``-N <note>`` are two-arg;
    # treat anything else starting with ``-`` as a one-arg flag
    # defensively (we don't expect them, but a future tmux release
    # might add new ones).
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] in ("-T", "-N"):
            i += 2
        else:
            i += 1
    if i >= len(toks):
        return None
    return toks[i], toks[i + 1 :]


def _classify(cmd_tokens: list[str]) -> str | None:
    """
    Classify a binding's command tokens as ``'v'`` / ``'h'`` /
    ``'w'``, or ``None`` if the binding doesn't map to a top-level
    ``split-window`` / ``new-window`` invocation.

    Only top-level bindings qualify — lambda blocks, chained
    commands, custom shell wrappers all return ``None`` and are
    left untouched. The user's exotic split commands keep their
    original behavior in all panes; they just don't get chooser
    routing in omnigent panes. Acceptable v1 limitation.

    :param cmd_tokens: The command portion of a bind-key line,
        e.g. ``['split-window', '-h', '-c', '#{pane_current_path}']``.
    :returns: Direction code consumed by ``omnigent pane-split``,
        or ``None``.
    """
    if not cmd_tokens:
        return None
    cmd = cmd_tokens[0]
    if cmd == "new-window":
        return "w"
    if cmd != "split-window":
        return None
    # ``-h`` flag → horizontal split (panes side by side).
    # Default and ``-v`` → vertical split (panes stacked). Match
    # tmux's flag semantics, not human-intuition "horizontal divider".
    if "-h" in cmd_tokens[1:]:
        return "h"
    return "v"


# Marker our wrapper's ``if-shell -F`` always emits as its
# format-string condition. Used by :func:`_unwrap_existing_wrapper`
# to recognize a binding we already installed and recover the
# user's original command from the false branch — without this,
# repeat ``register_pane`` calls would see the wrapper itself
# (command starts with ``if-shell``, not ``split-window``) and
# skip re-installation, leaving an outdated wrapper in place.
_WRAPPER_MARKER_FORMAT = "#{?#{@omnigent-conv-id},1,0}"


def _unwrap_existing_wrapper(cmd_tokens: list[str]) -> list[str] | None:
    """
    Peel off an existing omnigent wrapper to recover the user's
    original command tokens.

    Detection: ``cmd_tokens`` starts with ``if-shell -F
    <our-marker>`` and has the canonical 5-token shape
    ``[if-shell, -F, <fmt>, <true-cmd>, <false-cmd>]``. The false
    branch is the user's original (untouched) command — we
    re-tokenize it via shlex so the caller can classify it like
    any unwrapped binding.

    :param cmd_tokens: The command portion of a bind-key line, as
        returned by :func:`_parse_bind_line`.
    :returns: The unwrapped original command tokens, or ``None``
        when *cmd_tokens* isn't one of our wrappers.
    """
    if len(cmd_tokens) != 5:
        return None
    if cmd_tokens[0] != "if-shell" or cmd_tokens[1] != "-F":
        return None
    if cmd_tokens[2] != _WRAPPER_MARKER_FORMAT:
        return None
    # cmd_tokens[3] is the true branch (our chooser); cmd_tokens[4]
    # is the user's original.
    try:
        return shlex.split(cmd_tokens[4])
    except ValueError:
        return None


def _discover_split_bindings() -> list[SplitBinding]:
    """
    Walk ``tmux list-keys -T prefix`` and yield user bindings that
    map to ``split-window`` / ``new-window``. Mirroring the user's
    existing keys preserves their muscle memory inside the
    omnigent pane.

    Bindings whose command can't be tokenized (lambda blocks),
    isn't a top-level ``split-window`` / ``new-window``, or has
    an unfamiliar flag set, are silently skipped. Non-omnigent
    panes still see those bindings unchanged (they're never
    rewritten); omnigent panes just don't get chooser routing
    on those keys.
    """
    out: list[SplitBinding] = []
    for line in _list_prefix_keys():
        parsed = _parse_bind_line(line)
        if parsed is None:
            continue
        key, cmd_tokens = parsed
        # If this binding is already one of our wrappers (from a
        # prior ``register_pane`` invocation), peel it off so we
        # see the user's ORIGINAL command and can re-classify
        # against the latest resolver. Without this, repeat
        # registration silently leaves an outdated wrapper in
        # place — the live regression that prompted this code path.
        unwrapped = _unwrap_existing_wrapper(cmd_tokens)
        if unwrapped is not None:
            cmd_tokens = unwrapped
        direction = _classify(cmd_tokens)
        if direction is None:
            continue
        out.append(
            SplitBinding(
                key=key,
                direction=direction,
                original_command=shlex.join(cmd_tokens),
            )
        )
    return out


def _wrap_binding(binding: SplitBinding, omnigent_argv: list[str]) -> None:
    """
    Replace one of the user's prefix-table bindings with an
    ``if-shell -F`` wrapper.

    True branch (focused pane is an omnigent — the
    ``@omnigent-conv-id`` option is set and non-empty): run
    the resolved ``omnigent pane-split`` invocation via
    ``run-shell``. False branch: the user's exact original
    command, unchanged.

    Using ``if-shell -F`` makes the conditional a pure tmux
    format-string evaluation — no shell process is spawned for
    the dispatch, only for the chooser's ``run-shell`` itself
    when the omnigent branch fires.

    :param binding: One of the user's prefix-table split-window /
        new-window bindings.
    :param omnigent_argv: Argv prefix for invoking the running
        omnigent installation, as returned by
        :func:`_resolve_omnigent_argv`. Length 1 (e.g.
        ``["/venv/bin/omnigent"]``) for a direct binary
        invocation, or length 3 (``[sys.executable, "-m",
        "omnigent.cli.commands"]``) for the python-m fallback. Either
        works because the entire prefix is shlex-quoted into
        the chooser shell command. The absolute path bypasses
        tmux's restricted PATH (the tmux server's environment
        usually doesn't include the venv ``bin/``), avoiding
        exit-code 127 ("command not found").
    """
    inner_argv = [
        *omnigent_argv,
        "pane-split",
        f"-{binding.direction}",
        "-p",
        # ``#{pane_id}`` is a tmux format placeholder, NOT a
        # shell token — tmux substitutes it before the shell
        # sees it. We leave it bare (un-quoted) so tmux's
        # interpolator finds it intact in the resulting string.
        "#{pane_id}",
    ]
    chooser_inner = " ".join(shlex.quote(p) if p != "#{pane_id}" else p for p in inner_argv)
    chooser_cmd = f"run-shell {shlex.quote(chooser_inner)}"
    subprocess.run(
        [
            "tmux",
            "bind-key",
            "-T",
            "prefix",
            binding.key,
            "if-shell",
            "-F",
            "#{?#{@omnigent-conv-id},1,0}",
            chooser_cmd,
            binding.original_command,
        ],
        check=False,
        capture_output=True,
    )


def register_pane(
    *,
    conv_id: str | None,
    agent_name: str,
    agent_yaml: Path | None,
    launch_argv: list[str],
    server_url: str | None,
) -> None:
    """
    Mark the current tmux pane as an omnigent pane and wrap the
    user's prefix-table split bindings.

    No-op when:

    - The REPL isn't running inside tmux (``$TMUX`` unset).
    - ``$TMUX_PANE`` is unset (degenerate environment).
    - tmux version is older than :data:`_MIN_TMUX_VERSION`.

    Subprocess failures from individual ``tmux`` invocations are
    swallowed (logged at warning level): the REPL continues to
    work, it just doesn't get the split-pane integration.

    :param conv_id: Initial conversation id to advertise. When
        ``None``, a placeholder is used so the wrapper's truthy
        check still fires; the placeholder gets replaced once the
        first conversation is actually created. See
        :func:`update_conv_id`.
    :param agent_name: Display name of the active agent, e.g.
        ``"coding-supervisor"``. Surfaced in the chooser dialog.
    :param agent_yaml: Path to the agent spec, or ``None`` when
        running against a remote URL where the spec lives on the
        server. Forwarded to chooser-launched siblings via
        ``OPT_AGENT_YAML``.
    :param launch_argv: ``sys.argv`` of the running ``omnigent
        run`` process. Re-played verbatim by the chooser when the
        user picks "new conversation with same agent".
    :param server_url: Omnigent server base URL (e.g.
        ``"http://127.0.0.1:8023"``), or ``None`` for the legacy
        non-AP path. Used by the chooser to enumerate
        sub-agent conversations.
    """
    if not PANE_INTEGRATION_ENABLED:
        # Master kill-switch: feature is disabled by default while
        # the chooser UX is still being iterated on. Flip
        # ``PANE_INTEGRATION_ENABLED`` to True at the top of this
        # module to re-enable.
        #
        # Even with the flag off, actively un-mark this pane so any
        # wrapper bindings still installed in the user's running
        # tmux server (left over from a previous flag-on run)
        # become inert here: the wrapper's
        # ``#{?#{@omnigent-conv-id},1,0}`` check returns 0, so the
        # else branch (the user's exact original split-window
        # command) runs. Without this active cleanup, "I disabled
        # the feature but my split key still opens a chooser" is
        # the live regression we're guarding against.
        if _is_in_tmux():
            pane_id = _tmux_pane_id()
            if pane_id is not None:
                _unset_pane_options(pane_id)
        return
    if not _is_in_tmux():
        return
    pane_id = _tmux_pane_id()
    if pane_id is None:
        _LOGGER.warning("$TMUX is set but $TMUX_PANE is not — skipping omnigent pane integration")
        return
    if not _tmux_version_ok():
        _LOGGER.warning(
            "tmux >= %s required for omnigent pane integration; skipping",
            ".".join(str(n) for n in _MIN_TMUX_VERSION),
        )
        return

    # Resolve a callable ``omnigent`` argv prefix. tmux's
    # ``run-shell`` (used by the wrapper) and ``split-window``
    # (used by ``pane-split``) inherit the tmux server's PATH,
    # which typically doesn't include the venv ``bin/`` where
    # ``omnigent`` lives — so a bare ``omnigent`` exits 127.
    # ``sys.argv[0]`` is the bare name when the user invoked us
    # via ``$PATH`` (e.g. typed ``omnigent`` after activating
    # a venv), so we can't just pass through ``launch_argv[0]``.
    # See :func:`_resolve_omnigent_argv` for the resolution
    # ladder (abspath → ``shutil.which`` → ``python -m`` fallback).
    omnigent_argv = _resolve_omnigent_argv()

    # Normalize the launcher prefix in *launch_argv* — replacing
    # whatever launcher tokens preceded the user's first
    # subcommand with the freshly resolved
    # ``omnigent_argv``. The user's subcommand and args
    # (``run agent.yaml …``) survive untouched.
    #
    # Idempotency under repeat calls: ``_user_args_after_launcher``
    # walks past leading launcher tokens (the omnigent binary,
    # ``python``, ``-m``, ``omnigent.cli.commands`` — including doubled
    # forms left by an earlier buggy register_pane) and returns
    # everything from the first user-facing click subcommand
    # onward. A regression where launch_argv accumulates extra
    # ``-m omnigent.cli.commands`` markers each time is exactly what this
    # repairs.
    user_args = _user_args_after_launcher(launch_argv)
    normalized_launch_argv = list(omnigent_argv) + user_args

    # Advertise context. The conv-id placeholder is enough to flip
    # the wrapper's truthy check on; the real conv id is set once
    # the first conversation is created.
    _set_pane_option(pane_id, OPT_CONV_ID, conv_id or _PENDING_CONV_ID)
    _set_pane_option(pane_id, OPT_AGENT_NAME, agent_name)
    if agent_yaml is not None:
        _set_pane_option(pane_id, OPT_AGENT_YAML, str(agent_yaml))
    _set_pane_option(pane_id, OPT_LAUNCH_ARGV, json.dumps(normalized_launch_argv))
    if server_url is not None:
        _set_pane_option(pane_id, OPT_SERVER_URL, server_url)

    # Discover and wrap. Any failure here only affects this REPL's
    # split-key UX; the user's normal prefix bindings remain
    # functional.
    for binding in _discover_split_bindings():
        _wrap_binding(binding, omnigent_argv)


def update_conv_id(conv_id: str) -> None:
    """
    Update ``OPT_CONV_ID`` on the current pane, e.g. after the
    first conversation is created or the user runs
    ``/switch <conv-id>``.

    Safe to call when not in tmux — degenerates to a no-op.

    :param conv_id: The new current conversation id.
    """
    if not _is_in_tmux():
        return
    pane_id = _tmux_pane_id()
    if pane_id is None:
        return
    _set_pane_option(pane_id, OPT_CONV_ID, conv_id)


def read_pane_option(pane_id: str, name: str) -> str | None:
    """
    Read one custom option from a tmux pane.

    Returns ``None`` when the option isn't set or when the tmux
    invocation fails. Used by ``omnigent pane-picker`` to
    recover the parent pane's launch context.

    :param pane_id: Target pane, e.g. ``"%0"``.
    :param name: Option name including leading ``@``, e.g.
        ``"@omnigent-launch-argv"``.
    """
    try:
        result = subprocess.run(
            ["tmux", "show-options", "-p", "-q", "-v", "-t", pane_id, name],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    out = result.stdout.strip()
    return out or None
