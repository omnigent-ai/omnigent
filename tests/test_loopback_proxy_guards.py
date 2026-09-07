"""Guard: server-directed HTTP clients must opt out of environment proxies.

An HTTP proxy resolves ``127.0.0.1`` against itself, so it can never reach
this machine's local Omnigent server. httpx trusts the environment by
default — and on Windows ``getproxies()`` also reads the system registry —
so any client built without ``trust_env`` fails against a perfectly healthy
local server with ``ConnectError: All connection attempts failed``.

The construction below is copied verbatim into every new native harness, so
this guard is a static check rather than a per-harness test: adding harness
number twelve by copy-paste must not silently reintroduce the bug.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules whose httpx clients are bound to the Omnigent server's base URL.
# Deliberately not repo-wide: runner tool/transport clients talk to harnesses
# and MCP endpoints, where the environment's proxy is the right thing to use.
_SERVER_CLIENT_MODULES: list[Path] = [
    *sorted((_REPO_ROOT / "omnigent").glob("*_native.py")),
    _REPO_ROOT / "omnigent" / "chat.py",
    _REPO_ROOT / "omnigent" / "cli.py",
    _REPO_ROOT / "omnigent" / "runner" / "_entry.py",
    _REPO_ROOT / "omnigent" / "runner" / "app.py",
]


def _clients_missing_trust_env(path: Path) -> list[int]:
    """
    Return the line numbers of server-bound httpx clients lacking ``trust_env``.

    Only calls that pass ``base_url`` are considered: a client with no base
    URL is not pinned to the Omnigent server, so the proxy question is the
    caller's to answer at the request site.

    :param path: Module to scan, e.g. ``omnigent/claude_native.py``.
    :returns: Line numbers of offending ``httpx.Client``/``AsyncClient`` calls.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in {"Client", "AsyncClient"}:
            continue
        keywords = {kw.arg for kw in node.keywords}
        if "base_url" in keywords and "trust_env" not in keywords:
            offenders.append(node.lineno)
    return offenders


@pytest.mark.parametrize(
    "module_path",
    _SERVER_CLIENT_MODULES,
    ids=lambda p: str(p.relative_to(_REPO_ROOT)),
)
def test_server_clients_opt_out_of_env_proxies(module_path: Path) -> None:
    """Every server-bound httpx client must decide ``trust_env`` explicitly.

    Without it, a user whose machine has any proxy configured cannot reach
    their own local server, and the failure surfaces as a crash rather than
    as a connectivity problem.
    """
    offenders = _clients_missing_trust_env(module_path)

    assert not offenders, (
        f"{module_path.relative_to(_REPO_ROOT)} builds an httpx client bound to a "
        f"server base_url without trust_env at line(s) {offenders}. "
        f"Pass trust_env=not is_loopback_url(base_url) so loopback targets "
        f"bypass the environment's proxy settings."
    )
