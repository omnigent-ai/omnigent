"""Normalize the registry in npm ``package-lock.json`` files to the public npm registry.

Local ``npm install`` runs resolve against whatever registry is configured
on the developer's machine (e.g. the Databricks npm proxy via a global
``~/.npmrc``), and npm rewrites every ``"resolved"`` URL in
``package-lock.json`` to that registry.  For this OSS repo the committed
lockfile must always point at the public npm registry
(``https://registry.npmjs.org``) so the lock is reproducible for
contributors who don't have the proxy — CI runners can't reach the
internal proxy, so ``npm ci`` would time out fetching from it.

This is a pre-commit *fixer*: it rewrites the registry host in every
``"resolved"`` URL in place and exits non-zero when it changed anything,
so the commit aborts and the developer re-stages the normalized lockfile
(mirroring ``end-of-file-fixer`` and friends).  Only the ``resolved``
host is touched; the path and ``integrity`` fields are identical between
the proxy and the canonical host, so the tarball content is unchanged.

Pass ``--check`` to validate without writing: it exits non-zero (and
names the offending URLs) when a file is *not* already canonical, but
leaves it untouched.  CI can run this mode against the committed
lockfile before ``npm ci`` to fail fast instead of timing out mid-install.

Usage::

    python scripts/normalize_package_lock_registry.py web/electron/package-lock.json
    python scripts/normalize_package_lock_registry.py --check web/electron/package-lock.json

Multiple files may be passed; pre-commit passes the changed files.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# The canonical public registry the committed lockfile must always use.
_CANONICAL_REGISTRY = "https://registry.npmjs.org"

# Matches a "resolved" URL with a non-canonical host, capturing the path
# suffix so only the host is replaced, e.g.
#   "resolved": "https://npm-proxy.cloud.databricks.com/electron-updater/-/electron-updater-6.8.9.tgz"
# becomes
#   "resolved": "https://registry.npmjs.org/electron-updater/-/electron-updater-6.8.9.tgz"
_NON_CANONICAL_RESOLVED_RE = re.compile(
    r'("resolved":\s*")(https?://(?!registry\.npmjs\.org)[^"]+)(")'
)


def non_canonical_urls(text: str) -> list[str]:
    """Return the ``resolved`` URLs in *text* that are not on the canonical registry.

    :param text: Full contents of a ``package-lock.json`` file.
    :returns: Each non-canonical URL, in order, with duplicates preserved.
    """
    return [m.group(2) for m in _NON_CANONICAL_RESOLVED_RE.finditer(text)]


def normalize_text(text: str) -> str:
    """Return *text* with every non-canonical ``resolved`` URL rewritten to the canonical registry.

    The path component (everything after the host) is identical between a
    proxy and the canonical registry, so only the host is swapped.

    :param text: Full contents of a ``package-lock.json`` file.
    :returns: The normalized text.
    """
    return _NON_CANONICAL_RESOLVED_RE.sub(rf"\g<1>{_CANONICAL_REGISTRY}\g<3>", text)


def main(argv: list[str]) -> int:
    """Normalize (or, with ``--check``, validate) each given lockfile.

    :param argv: Filenames to process, optionally preceded/followed by the
        ``--check`` flag (passed by pre-commit or CI).
    :returns: In fix mode, ``1`` when a file was modified (so the commit
        aborts and the change is re-staged) else ``0``.  In ``--check``
        mode, ``1`` when any file is not already canonical (printing the
        offending URLs) else ``0``; no file is written.
    """
    check = "--check" in argv
    files = [a for a in argv if a != "--check"]

    if check:
        ok = True
        for name in files:
            offenders = non_canonical_urls(Path(name).read_text())
            if offenders:
                ok = False
                unique = sorted(set(offenders))
                print(
                    f"{name}: {len(offenders)} non-canonical resolved URL(s) "
                    f"(expected {_CANONICAL_REGISTRY}):"
                )
                for url in unique:
                    print(f"  {url}")
                print(
                    "Fix with: python scripts/normalize_package_lock_registry.py "
                    f"{' '.join(files)} && git add {' '.join(files)}"
                )
        return 0 if ok else 1

    changed = False
    for name in files:
        path = Path(name)
        original = path.read_text()
        normalized = normalize_text(original)
        if normalized != original:
            # Validate that the result is still valid JSON before writing.
            json.loads(normalized)
            path.write_text(normalized)
            count = len(non_canonical_urls(original))
            print(f"{name}: normalized {count} resolved URL(s) to {_CANONICAL_REGISTRY}")
            changed = True
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
