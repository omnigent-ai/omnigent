"""Pick which version tag each floating release tag should point at, using PEP
440 ordering. Reads ALL_TAGS (the repo's tag names, newline-separated) from the
environment and prints one line: "<rc_tag> <latest_tag>" where

  rc_tag     = max(release, rc)   -> the image :latest-rc should reference
  latest_tag = max(final release) -> the image :latest    should reference

Either field is "-" when no qualifying tag exists. The original tag string
(e.g. "v0.1.1") is preserved so the caller can reference the matching image
tag. Used by the reconcile-floating job in
.github/workflows/oss-publish-images.yml to retag :latest / :latest-rc onto the
correct existing images without a rebuild.
"""

import os

from packaging.version import InvalidVersion, Version


def parse(name):
    try:
        return Version(name.strip().removeprefix("v"))
    except InvalidVersion:
        return None


def main():
    pairs = [
        (v, t.strip()) for t in os.environ.get("ALL_TAGS", "").splitlines() if (v := parse(t))
    ]
    if not pairs:
        print("- -")
        return

    # Tie-break on the raw tag string so the choice is deterministic.
    _, rc_tag = max(pairs, key=lambda p: (p[0], p[1]))
    finals = [p for p in pairs if not p[0].is_prerelease]
    latest_tag = max(finals, key=lambda p: (p[0], p[1]))[1] if finals else "-"
    print(f"{rc_tag} {latest_tag}")


if __name__ == "__main__":
    main()
