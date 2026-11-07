"""Update the direct-download GitHub CLI pins that Dependabot cannot discover."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

DOCKERFILES = ("deploy/docker/Dockerfile", "deploy/docker/Dockerfile.ubi")
ARCHITECTURES = ("amd64", "arm64")
RELEASE_URL = "https://github.com/cli/cli/releases/tag/v"


def version_tuple(version: str) -> tuple[int, ...]:
    """Accept stable three-part versions only."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"Not a stable GitHub CLI version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def read_pins(text: str) -> dict[str, str]:
    """Fail closed if the pin block is missing, duplicated or malformed."""
    pins = {}
    for key in ("GH_VERSION", "GH_SHA256_AMD64", "GH_SHA256_ARM64"):
        declarations = re.findall(rf"^[ \t]*ARG[ \t]+{key}(?:[= \t].*)?$", text, re.MULTILINE)
        if len(declarations) != 1:
            raise ValueError(f"Expected exactly one ARG {key}")
        match = re.fullmatch(rf"ARG {key}=(\S+)", declarations[0])
        if match is None:
            raise ValueError(f"Malformed ARG {key}")
        value = match[1]
        if key == "GH_VERSION":
            version_tuple(value)
        elif not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"Invalid SHA-256 for {key}")
        pins[key] = value
    return pins


def select_release(
    releases: list[dict], current: str, *, now: datetime, min_age_days: int
) -> dict | None:
    """Choose the highest newer stable release outside the cooldown."""
    if min_age_days < 0:
        raise ValueError("min_age_days must be nonnegative")
    candidates = []
    for release in releases:
        tag = release.get("tag_name", "")
        if release.get("draft") or release.get("prerelease"):
            continue
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
            continue
        if version_tuple(tag[1:]) <= version_tuple(current):
            continue
        published = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
        if published > now - timedelta(days=min_age_days):
            continue
        candidates.append(release)
    return max(
        candidates, key=lambda release: version_tuple(release["tag_name"][1:]), default=None
    )


def verified_digests(release: dict) -> dict[str, str]:
    """Hash both official archives and require agreement with GitHub asset metadata."""
    version = release["tag_name"].removeprefix("v")
    version_tuple(version)
    digests = {}
    for arch in ARCHITECTURES:
        name = f"gh_{version}_linux_{arch}.tar.gz"
        assets = [asset for asset in release["assets"] if asset["name"] == name]
        if len(assets) != 1:
            raise ValueError(f"Expected exactly one official release asset: {name}")
        asset = assets[0]
        digest = asset.get("digest") or ""
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Missing or invalid GitHub asset digest: {name}")
        url = f"https://github.com/cli/cli/releases/download/v{version}/{name}"
        if asset["browser_download_url"] != url:
            raise ValueError(f"Unexpected release asset URL: {name}")
        hasher = hashlib.sha256()
        size = 0
        with urllib.request.urlopen(url, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                hasher.update(chunk)
                size += len(chunk)
        if size != asset["size"] or f"sha256:{hasher.hexdigest()}" != digest:
            raise ValueError(f"Release archive does not match GitHub metadata: {name}")
        digests[arch] = hasher.hexdigest()
    return digests


def updated_files(root: Path, release: dict, digests: dict[str, str]) -> dict[Path, str]:
    """Validate both Dockerfiles before preparing either edit; never downgrade."""
    originals = {root / name: (root / name).read_text() for name in DOCKERFILES}
    pins = [read_pins(text) for text in originals.values()]
    if pins[0] != pins[1]:
        raise ValueError("GitHub CLI pins disagree between Dockerfiles")
    version = release["tag_name"].removeprefix("v")
    if version_tuple(version) <= version_tuple(pins[0]["GH_VERSION"]):
        raise ValueError("Release must be newer than the current pin")
    replacements = {"GH_VERSION": version}
    for arch in ARCHITECTURES:
        if not re.fullmatch(r"[0-9a-f]{64}", digests[arch]):
            raise ValueError(f"Invalid verified digest for {arch}")
        replacements[f"GH_SHA256_{arch.upper()}"] = digests[arch]
    edits = {}
    for path, text in originals.items():
        for key, value in replacements.items():
            text = re.sub(rf"^ARG {key}=\S+$", f"ARG {key}={value}", text, flags=re.MULTILINE)
        edits[path] = text
    return edits


def pr_body(root: Path, version: str) -> str:
    """Keep the generated dependency PR aligned with the repository PR template."""
    body = (root / ".github/pull_request_template.md").read_text().split("## Changelog")[0]
    body = re.sub(r"<!--.*?-->\s*", "", body, flags=re.DOTALL).replace("Closes #\n", "")
    sections = {
        "Related issue": "None required — automated Test / CI dependency maintenance.",
        "Summary": (
            "Dependabot tracks container base images, but not this downloaded CLI. "
            f"Update GitHub CLI to {version} in both managed-host Dockerfiles.\n\n"
            f"Official release: {RELEASE_URL}{version}\n\n"
            "Both Linux archives were downloaded and SHA-256 hashed; the hashes match "
            "GitHub's release-asset digests. No binaries were executed."
        ),
        "Test Plan": (
            "- The updater verified both archive sizes and SHA-256 digests before editing.\n"
            "- Normal Docker build, lint and test checks must pass before merge.\n"
            "- Review the upstream release notes and the six pin changes."
        ),
        "Coverage notes": (
            "Automated artifact verification ran in the updater workflow. "
            "Image installation/version checks run in normal PR CI."
        ),
    }
    for heading, content in sections.items():
        body = body.replace(f"## {heading}\n\n", f"## {heading}\n\n{content}\n\n")
    for label in (
        "Non-visual evidence provided below or in Test Plan",
        "Test / CI",
        "Existing tests cover this change",
        "No — no noteworthy user-facing change.",
    ):
        body = body.replace(f"- [ ] {label}", f"- [x] {label}")
    return body.strip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--min-age-days", type=int, default=7)
    parser.add_argument("--write", action="store_true", help="Apply pins; default is a dry run")
    parser.add_argument("--body-file", type=Path)
    args = parser.parse_args()
    pins = [read_pins((args.root / path).read_text()) for path in DOCKERFILES]
    if pins[0] != pins[1]:
        raise ValueError("GitHub CLI pins disagree between Dockerfiles")
    result = subprocess.run(
        ["gh", "api", "repos/cli/cli/releases?per_page=100", "--paginate", "--slurp"],
        check=True,
        capture_output=True,
        text=True,
    )
    releases = [release for page in json.loads(result.stdout) for release in page]
    release = select_release(
        releases, pins[0]["GH_VERSION"], now=datetime.now(UTC), min_age_days=args.min_age_days
    )
    if release is None:
        print("No newer stable GitHub CLI release outside the cooldown.")
        return
    digests = verified_digests(release)
    edits = updated_files(args.root, release, digests)
    version = release["tag_name"][1:]
    print(f"Verified GitHub CLI {pins[0]['GH_VERSION']} -> {version}: {json.dumps(digests)}")
    if args.write:
        body = pr_body(args.root, version) if args.body_file else None
        for path, text in edits.items():
            path.write_text(text)
        if args.body_file and body is not None:
            args.body_file.write_text(body)
    else:
        print("Dry run: no files changed. Pass --write to apply.")


if __name__ == "__main__":
    main()
