"""Normalize the package registry in ``ap-web/package-lock.json`` to public npm.

Local ``npm`` runs resolve against whatever registry is configured on the
developer's machine (e.g. the Databricks npm proxy
``npm-proxy.cloud.databricks.com``), and ``npm`` bakes that host into every
``"resolved"`` tarball URL it writes to ``package-lock.json``. Crucially,
``npm ci`` then fetches each package from its locked ``resolved`` URL —
**ignoring** ``NPM_CONFIG_REGISTRY`` — so a single committed proxy URL makes
every frontend CI job (pre-commit's ap-web install, ``npm test``, the UI-snapshot
build, the E2E-UI shards) fail at install with ``ETIMEDOUT`` against an internal
proxy the public OSS runners can't reach. For this OSS repo the committed
lockfile must always resolve from public npm (``https://registry.npmjs.org``).

This is the npm analog of :mod:`scripts.normalize_uv_lock_registry`. As a
pre-commit *fixer* it rewrites each registry-tarball ``resolved`` host to
npmjs.org and exits non-zero when it changed anything, so the commit aborts and
the developer re-stages the normalized lockfile. Pass ``--check`` to validate
without writing (CI runs this against the committed file *before* any ``npm``
command — a later ``npm ci``/``install`` would re-resolve and mask a committed
proxy URL otherwise). Only registry tarball URLs (``.../-/<name>-<ver>.tgz``) are
touched; ``git+``/``file:``/workspace ``resolved`` entries are left alone.

Usage::

    python scripts/normalize_npm_lock_registry.py ap-web/package-lock.json
    python scripts/normalize_npm_lock_registry.py --check ap-web/package-lock.json
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The canonical public registry host the committed lockfile must always use.
_CANONICAL_HOST = "registry.npmjs.org"

# Matches a registry-tarball ``"resolved"`` URL, capturing the ``"resolved": "``
# prefix and the ``/<path>/-/<file>.tgz"`` suffix so only the scheme+host between
# them is rewritten. The ``/-/ … .tgz`` shape is unique to registry tarballs, so
# ``git+https://`` / ``file:`` / workspace ``resolved`` entries never match.
_RESOLVED_TARBALL_RE = re.compile(r'("resolved":\s*")https://[^/"]+(/[^"]*?/-/[^"]+\.tgz")')

# Same shape, capturing just the host — used by the ``--check`` reporter.
_RESOLVED_HOST_RE = re.compile(r'"resolved":\s*"https://([^/"]+)/[^"]*?/-/[^"]+\.tgz"')


def non_canonical_hosts(text: str) -> list[str]:
    """Return registry-tarball ``resolved`` hosts that are not public npm.

    :param text: Full contents of a ``package-lock.json`` file.
    :returns: Each non-canonical tarball host, in order, with duplicates
        preserved (one per offending ``resolved`` entry).
    """
    return [h for h in _RESOLVED_HOST_RE.findall(text) if h != _CANONICAL_HOST]


def normalize_text(text: str) -> str:
    """Return *text* with every registry-tarball ``resolved`` host set to npmjs.

    :param text: Full contents of a ``package-lock.json`` file.
    :returns: The same text with each registry-tarball URL's scheme+host
        replaced by ``https://registry.npmjs.org`` (path preserved).
    """
    return _RESOLVED_TARBALL_RE.sub(rf"\g<1>https://{_CANONICAL_HOST}\g<2>", text)


def main(argv: list[str]) -> int:
    """Normalize (or, with ``--check``, validate) each given lockfile.

    :param argv: Filenames to process, optionally with the ``--check`` flag
        (passed by pre-commit or CI).
    :returns: In fix mode, ``1`` when a file was modified (so the commit aborts
        and the change is re-staged) else ``0``. In ``--check`` mode, ``1`` when
        any file has a non-canonical tarball host (printing the offenders) else
        ``0``; no file is written.
    """
    check = "--check" in argv
    files = [a for a in argv if a != "--check"]

    if check:
        ok = True
        for name in files:
            offenders = non_canonical_hosts(Path(name).read_text())
            if offenders:
                ok = False
                unique = sorted(set(offenders))
                print(
                    f"{name}: {len(offenders)} non-canonical registry tarball "
                    f"host(s) (expected {_CANONICAL_HOST}): {', '.join(unique)}"
                )
                print(
                    "Fix with: python scripts/normalize_npm_lock_registry.py "
                    f"{name} && git add {name}"
                )
        return 0 if ok else 1

    changed = False
    for name in files:
        path = Path(name)
        original = path.read_text()
        normalized = normalize_text(original)
        if normalized != original:
            path.write_text(normalized)
            print(f"{name}: normalized registry tarball hosts to {_CANONICAL_HOST}")
            changed = True
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
