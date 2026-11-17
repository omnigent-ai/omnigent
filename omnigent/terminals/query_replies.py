"""Which terminal queries a tmux server answers itself.

A program in a pane asks its terminal questions (cursor position, device
attributes, colours, mode reports) by writing escape sequences and reading the
reply as input. tmux, the pane's real terminal, answers some of them. Control
mode also forwards the raw query to the browser's xterm.js, which answers every
one it knows, so where tmux has already answered, the browser's reply reaches
the program as typed input.

The browser therefore drops its reply only for the queries tmux answers. Which
those are depends on the tmux release (3.3 answers 6 of the queries xterm.js
does, 3.7 answers far more) and on how the binary was built, so this asks the
installed tmux directly instead of keying a table on ``tmux -V``.

The probe runs a throwaway server with a control-mode client attached, as the
bridge's is. A ``sh`` program in its pane writes each query followed by a
primary device attributes query (DA1), which every supported tmux answers, and
records what comes back. tmux answers in order, so whatever arrives between
two DA1 replies is tmux's answer to the query between them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

_logger = logging.getLogger(__name__)

# DEC private modes xterm.js reports on, plus 2031, which tmux 3.6 reports on
# by name. xterm.js answers a request for any mode number, so these cover the
# modes a tmux that answers only named modes is known to report.
_DEC_MODES = (
    1, 3, 6, 7, 8, 9, 12, 25, 45, 47, 66, 67, 1000, 1002, 1003, 1004, 1005,
    1006, 1015, 1016, 1047, 1048, 1049, 2004, 2026, 2031,
)  # fmt: skip
# ANSI modes xterm.js reports on.
_ANSI_MODES = (2, 4, 12, 20)
# Status strings (DECRQSS) xterm.js reports on.
_STATUS_STRINGS = ('"q', '"p', "r", "m", " q")
# A mode number and a status string no terminal implements. tmux answering
# either means it answers every request of that kind (tmux 3.7 reports every
# mode, recognised or not; 3.6 answers every status string).
_UNKNOWN_MODE = 9999
_UNKNOWN_STATUS = "zz"


def _csi(body: str) -> bytes:
    return f"\x1b[{body}".encode()


# Every query xterm.js answers, keyed by the identifier the web client matches
# on (web/src/components/blocks/TerminalQueryReplies.ts). A ``*`` identifier
# stands for every request of its kind.
QUERIES: Mapping[str, bytes] = {
    "da2": _csi(">c"),
    "dsr:5": _csi("5n"),
    "dsr:6": _csi("6n"),
    **{f"dsr:?{code}": _csi(f"?{code}n") for code in (6, 15, 25, 26, 53)},
    **{f"decrqm:{mode}": _csi(f"{mode}$p") for mode in _ANSI_MODES},
    "decrqm:*": _csi(f"{_UNKNOWN_MODE}$p"),
    **{f"decrqm:?{mode}": _csi(f"?{mode}$p") for mode in _DEC_MODES},
    "decrqm:?*": _csi(f"?{_UNKNOWN_MODE}$p"),
    **{f"decrqss:{name}": f"\x1bP$q{name}\x1b\\".encode() for name in _STATUS_STRINGS},
    "decrqss:*": f"\x1bP$q{_UNKNOWN_STATUS}\x1b\\".encode(),
    "osc:4": b"\x1b]4;1;?\x07",
    **{f"osc:{code}": f"\x1b]{code};?\x07".encode() for code in (10, 11, 12)},
}

# The query that separates the others, and the reply that marks each boundary.
_SENTINEL = _csi("c")
_SENTINEL_REPLY = re.compile(rb"\x1b\[\?[0-9;]*c")
# DA1 itself is answered exactly when the sentinels are.
_SENTINEL_ID = "da1"

# A probe takes well under 0.1s; past this its result is treated as unknown.
_PROBE_TIMEOUT_S = 2.0
# Cleanup after a timeout has its own bound, so a hung tmux can't hold the attach.
_CLEANUP_TIMEOUT_S = 1.0

_cache: dict[tuple[str, int, int], frozenset[str] | None] = {}
_cache_lock = asyncio.Lock()


def answered_queries(replies: bytes, query_ids: list[str]) -> frozenset[str] | None:
    """Work out which queries tmux answered from the bytes its pane read back.

    :param replies: Everything the probe pane read, e.g.
        ``b"\\x1b[?1;2c\\x1b[0n\\x1b[?1;2c"`` for ``["da2", "dsr:5"]`` on a tmux
        that answers DSR but not DA2.
    :param query_ids: The identifiers of the queries written, in order; each
        was followed by a DA1 sentinel.
    :returns: The identifiers tmux answered, including ``"da1"``, or ``None``
        when fewer sentinel replies arrived than queries were written, so the
        boundaries can't be trusted.
    """
    boundaries = list(_SENTINEL_REPLY.finditer(replies))
    if len(boundaries) < len(query_ids):
        return None
    answered = {_SENTINEL_ID}
    start = 0
    for query_id, boundary in zip(query_ids, boundaries, strict=False):
        if boundary.start() > start:
            answered.add(query_id)
        start = boundary.end()
    return frozenset(answered)


async def _tmux(tmux: str, socket: str, *args: str, capture: bool = False) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        tmux,
        "-S",
        socket,
        "-f",
        os.devnull,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await proc.communicate()
        return await proc.wait(), out or b""
    finally:
        # A cancelled probe must not leave the command running.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


async def _probe(tmux: str, client_env: Mapping[str, str]) -> frozenset[str] | None:
    # Unix socket paths are capped near 104 bytes, so keep the directory short.
    workdir = Path(tempfile.mkdtemp(prefix="omnigent-tmux-replies-"))
    socket = str(workdir / "s")
    queries_path = workdir / "queries"
    replies_path = workdir / "replies"
    query_ids = list(QUERIES)
    queries_path.write_bytes(b"".join(QUERIES[query_id] + _SENTINEL for query_id in query_ids))
    # Raw mode keeps the replies from being echoed or line-buffered. The pane
    # waits for the control client to attach first: some answers (OSC 10/11
    # since tmux 3.4) depend on an attached client, as they do for the bridge.
    pane_command = "; ".join(
        (
            "stty raw -echo",
            f"{shlex.quote(tmux)} wait-for go",
            f"cat {shlex.quote(str(queries_path))}",
            f"exec cat > {shlex.quote(str(replies_path))}",
        )
    )
    client: asyncio.subprocess.Process | None = None
    try:
        # Run sh directly: tmux would run a single-string command through the
        # user's $SHELL, which may not be POSIX (nushell, PowerShell).
        sh_command = ("--", "/bin/sh", "-c", pane_command)
        code, _ = await _tmux(
            tmux, socket, "new-session", "-d", "-x", "80", "-y", "24", *sh_command
        )
        if code:
            return None
        client = await asyncio.create_subprocess_exec(
            tmux,
            "-S",
            socket,
            "-f",
            os.devnull,
            "-C",
            "attach",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **client_env},
        )
        # The control client registers asynchronously; wait for it to show up
        # before releasing the pane.
        while True:
            _, clients = await _tmux(tmux, socket, "list-clients", capture=True)
            if clients.strip():
                break
            await asyncio.sleep(0.02)
        code, _ = await _tmux(tmux, socket, "wait-for", "-S", "go")
        if code:
            return None
        while True:
            replies = replies_path.read_bytes() if replies_path.exists() else b""
            if len(_SENTINEL_REPLY.findall(replies)) >= len(query_ids):
                return answered_queries(replies, query_ids)
            await asyncio.sleep(0.02)
    finally:
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(_tmux(tmux, socket, "kill-server"), _CLEANUP_TIMEOUT_S)
        if client is not None and client.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                client.kill()
            await client.wait()
        shutil.rmtree(workdir, ignore_errors=True)


async def tmux_answered_queries(tmux: str, client_env: Mapping[str, str]) -> frozenset[str] | None:
    """Return the queries this tmux binary answers itself, probing it once.

    :param tmux: Path to the tmux binary, e.g. ``"/usr/bin/tmux"``.
    :param client_env: Environment overrides for the probe's control client,
        matching the bridge's (e.g. ``{"TERM": "xterm-256color"}``).
    :returns: Query identifiers from :data:`QUERIES`, plus ``"da1"``, or
        ``None`` when the probe failed. ``None`` means the browser should drop
        none of its replies, which is how terminals behaved before.
    """
    try:
        stat = os.stat(tmux)
    except OSError:
        return None
    key = (tmux, stat.st_mtime_ns, stat.st_size)
    async with _cache_lock:
        if key not in _cache:
            try:
                _cache[key] = await asyncio.wait_for(_probe(tmux, client_env), _PROBE_TIMEOUT_S)
            except (TimeoutError, OSError) as exc:
                _logger.warning("tmux query-reply probe failed for %s: %r", tmux, exc)
                _cache[key] = None
        return _cache[key]
