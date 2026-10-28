"""The node program behind a Windows npm or pnpm ``.cmd`` shim.

npm and pnpm install a CLI such as ``pi`` on Windows as a ``.cmd`` shim that
runs a JavaScript script with node. Windows runs a ``.cmd`` through cmd.exe,
which ends the command line at the first newline and expands ``%`` in
arguments, and a Windows tmux refuses a ``.cmd`` pane program. Callers run the
shim's node and script directly instead.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
from contextlib import suppress
from importlib.resources import as_file, files
from pathlib import Path

from omnigent._platform import IS_WINDOWS
from omnigent.inner._proc import kill_tree

_PROBE_SENTINEL = "omnigent-node-cmd-shim-probe:"
_PROBE_TIMEOUT_S = 5.0
_PROBE_REAP_TIMEOUT_S = 2.0
_NAMES_NODE = re.compile(rb"\bnode\b", re.IGNORECASE)


def node_cmd_shim_argv(path: str) -> list[str] | None:
    """Return the node executable and script that a Windows ``.cmd`` shim runs.

    Node itself reports them: the shim runs once, without arguments, under a
    ``--require`` probe that prints them and exits before the script starts.

    :param path: The resolved program, e.g. ``C:\\...\\pnpm\\pi.CMD``.
    :returns: ``[node, script]``, e.g. ``["C:\\...\\node.exe", "C:\\...\\cli.js"]``,
        or ``None`` off Windows, for another program, or when the probe fails.
    """
    if not IS_WINDOWS or not path.lower().endswith(".cmd"):
        return None
    try:
        shim = Path(path).resolve(strict=True)
        shim_stat = shim.stat()
    except OSError:
        return None
    argv = _probe(str(shim), shim_stat.st_mtime_ns, shim_stat.st_size)
    return None if argv is None else list(argv)


@functools.lru_cache(maxsize=32)
def _probe(shim: str, mtime_ns: int, size: int) -> tuple[str, str] | None:
    """Run *shim* under the node probe; *mtime_ns* and *size* key the cache."""
    del mtime_ns, size
    probe = files("omnigent.inner").joinpath("node_cmd_shim_probe.cjs")
    try:
        # The probe stops node only, so a shim that names no node never runs.
        if not _NAMES_NODE.search(Path(shim).read_bytes()):
            return None
        with as_file(probe) as probe_path:
            # Node reads a backslash in NODE_OPTIONS as an escape.
            require = f'--require "{probe_path.as_posix()}"'
            node_options = f"{os.environ.get('NODE_OPTIONS', '')} {require}".lstrip()
            process = subprocess.Popen(
                [shim],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "NODE_OPTIONS": node_options},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                stdout, _ = process.communicate(timeout=_PROBE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                # Killing cmd.exe alone leaves node holding the pipe; never wait on it.
                kill_tree(process)
                with suppress(subprocess.TimeoutExpired):
                    process.communicate(timeout=_PROBE_REAP_TIMEOUT_S)
                return None
        output = stdout.decode("utf-8", errors="replace")
        _, found, reported = output.partition(_PROBE_SENTINEL)
        if not found:
            return None
        node, script = json.loads(reported.splitlines()[0])
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, IndexError):
        return None
    if isinstance(node, str) and isinstance(script, str):
        if os.path.isfile(node) and os.path.isfile(script):
            return node, script
    return None
