"""Interpreter flag for omnigent modules spawned as native-harness hooks.

Native harnesses run omnigent code in child interpreters — command hooks, MCP
``serve-mcp`` servers, policy/usage hooks, the cost popup. Every one of those
children must import the *same* omnigent as its parent, and must not pick up a
different copy from the workspace it happens to run in (a workspace that is
itself an omnigent checkout would otherwise shadow the installed package).

``-P`` is the flag for exactly that: it keeps the script directory and the cwd
off ``sys.path`` and changes nothing else.

Do not use ``-I`` here. Isolated mode implies ``-E`` and ``-s``, so it also
discards ``PYTHONPATH`` and user site-packages — the entries that make omnigent
importable at all for a PYTHONPATH-based layout, a ``pip install --user``, or an
editable install whose finder lives in user site. When those are how the parent
resolved omnigent, every hook exits with ``ModuleNotFoundError`` and the harness
silently loses the features the hooks provide (for claude-native, the transcript
forwarder never starts, so the session's turns never reach the web UI).
"""

from __future__ import annotations

from typing import Final

#: ``python -P``: cwd/script dir stay off ``sys.path``; ``PYTHONPATH`` and user
#: site-packages are preserved. See the module docstring for why not ``-I``.
SAFE_PATH_FLAG: Final[str] = "-P"
