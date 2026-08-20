"""Decide whether the pushed tag is the max version overall and/or the max
final release, using PEP 440 ordering (1.2.3rc1 < 1.2.3 — which `sort -V` gets
wrong). Inputs via env: CUR (the pushed tag, e.g. "v0.1.1") and ALL_TAGS (the
repo's tag names, newline-separated). Prints "<is_max_rc> <is_max_release>" as
true/false. Used by .github/workflows/oss-publish-images.yml to gate the
:latest-rc (max release-or-rc) and :latest (max final release) image tags.
"""

import os

from packaging.version import InvalidVersion, Version


def parse(name):
    try:
        return Version(name.strip().removeprefix("v"))
    except InvalidVersion:
        return None


def main():
    cur = parse(os.environ["CUR"])
    if cur is None:
        print("false false")
        return

    versions = [v for v in (parse(t) for t in os.environ.get("ALL_TAGS", "").splitlines()) if v]
    versions.append(cur)  # guard against a tag listing that lags the just-pushed tag

    max_all = max(versions)
    finals = [v for v in versions if not v.is_prerelease]
    max_final = max(finals) if finals else None

    is_max_rc = cur == max_all
    is_max_release = (not cur.is_prerelease) and max_final is not None and cur == max_final
    print(f"{'true' if is_max_rc else 'false'} {'true' if is_max_release else 'false'}")


if __name__ == "__main__":
    main()
