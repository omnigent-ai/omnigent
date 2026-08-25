"""Normalize ``uv.lock`` to the public PyPI index and canonical file metadata.

Local ``uv`` runs resolve against whatever index is configured on the
developer's machine (e.g. the Databricks PyPI proxy via ``UV_INDEX_URL``
or ``~/.config/uv``), and ``uv`` rewrites every
``source = { registry = "<url>" }`` entry in ``uv.lock`` to that index.
For this OSS repo the committed lockfile must always point at public
PyPI (``https://pypi.org/simple``) so the lock is reproducible for
contributors who don't have the proxy — CI already pins
``UV_INDEX_URL: https://pypi.org/simple`` for the same reason.

The index also leaks through file metadata: pypi.org serves a size for
every wheel/sdist while proxy indexes may not, so each re-lock adds or
strips ``size = N`` across thousands of lines depending on which index
resolved it. The canonical form omits ``size`` (the hash is the
integrity check), so re-locks from either side stop ping-ponging.

This is a pre-commit *fixer*: it rewrites the lockfile in place and
exits non-zero when it changed anything, so the commit aborts and the
developer re-stages the normalized lockfile (mirroring
``end-of-file-fixer`` and friends). Only ``registry`` sources are
touched; ``git`` / ``path`` / ``editable`` sources are left alone.

Pass ``--check`` to validate without writing: it exits non-zero (and
names the offending entries) when a file is *not* already canonical, but
leaves it untouched. CI runs this mode against the committed lockfile
*before* any ``uv`` command — a plain ``uv run pre-commit`` can't catch a
committed proxy URL, because ``uv`` re-syncs the working tree to CI's
own index (``pypi.org``) first and masks it.

Usage::

    python scripts/normalize_uv_lock_registry.py uv.lock           # fix
    python scripts/normalize_uv_lock_registry.py --check uv.lock   # verify
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# The canonical public index the committed lockfile must always use.
_CANONICAL_INDEX = "https://pypi.org/simple"

# The canonical file host for wheel/sdist direct URLs.
_CANONICAL_FILES_HOST = "https://files.pythonhosted.org"

# Matches a uv.lock registry source, capturing the surrounding literal so
# only the URL between the quotes is replaced, e.g.
#   source = { registry = "https://pypi-proxy.example.com/simple" }
_REGISTRY_RE = re.compile(r'(registry = ")[^"]*(")')

# Matches any non-pypi.org host in a direct wheel/sdist url = "..." entry so
# proxy-resolved URLs (e.g. pypi-proxy.cloud.databricks.com) can be rewritten
# to files.pythonhosted.org.  The path component (/packages/…) is identical
# between the proxy and the canonical host.
_DIRECT_URL_RE = re.compile(
    r'(url = ")(https?://(?!files\.pythonhosted\.org)[^"]+?/packages/)([^"]*")'
)

# Matches the optional file-size field after a wheel/sdist hash, e.g.
#   hash = "sha256:...", size = 116543, upload-time = "..."
# pypi.org's index serves sizes, proxy indexes may not; canonical form omits them.
_SIZE_RE = re.compile(r'(hash = "[^"]+"), size = \d+')


def non_canonical_entries(text: str) -> list[str]:
    """Return the non-canonical registry URLs, direct-URL hosts, and size fields in *text*.

    :param text: Full contents of a ``uv.lock`` file.
    :returns: Each non-canonical URL, in order, with duplicates preserved,
        plus one summary entry when ``size`` fields are present.
    """
    bad: list[str] = [
        m.group(1)
        for m in re.finditer(r'registry = "([^"]*)"', text)
        if m.group(1) != _CANONICAL_INDEX
    ]
    bad += [m.group(2) for m in _DIRECT_URL_RE.finditer(text)]
    sizes = len(_SIZE_RE.findall(text))
    if sizes:
        bad.append(f"size field on {sizes} file entr{'y' if sizes == 1 else 'ies'}")
    return bad


def normalize_text(text: str) -> str:
    """Return *text* with URLs rewritten to canonical hosts and ``size`` fields dropped.

    :param text: Full contents of a ``uv.lock`` file.
    :returns: The normalized text.
    """
    text = _REGISTRY_RE.sub(rf"\g<1>{_CANONICAL_INDEX}\g<2>", text)
    text = _DIRECT_URL_RE.sub(rf"\g<1>{_CANONICAL_FILES_HOST}/packages/\g<3>", text)
    return _SIZE_RE.sub(r"\1", text)


def main(argv: list[str]) -> int:
    """Normalize (or, with ``--check``, validate) each given lockfile.

    :param argv: Filenames to process, optionally preceded/followed by the
        ``--check`` flag (passed by pre-commit or CI).
    :returns: In fix mode, ``1`` when a file was modified (so the commit
        aborts and the change is re-staged) else ``0``. In ``--check``
        mode, ``1`` when any file is not already canonical (printing the
        offenders) else ``0``; no file is written.
    """
    check = "--check" in argv
    files = [a for a in argv if a != "--check"]

    if check:
        ok = True
        for name in files:
            offenders = non_canonical_entries(Path(name).read_text())
            if offenders:
                ok = False
                unique = sorted(set(offenders))
                print(
                    f"{name}: {len(offenders)} non-canonical entry(s) "
                    f"(expected {_CANONICAL_INDEX}, no size fields): {', '.join(unique)}"
                )
                print(
                    "Fix with: python scripts/normalize_uv_lock_registry.py "
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
            print(f"{name}: normalized to canonical form ({_CANONICAL_INDEX}, no size fields)")
            changed = True
    return 1 if changed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
