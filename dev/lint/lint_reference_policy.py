"""Check added text and paths against the repository reference policy."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from bisect import bisect_right
from pathlib import Path

POLICY_FILES = frozenset({"dev/lint/lint_reference_policy.py"})
PROHIBITED_NAMES = frozenset(
    {
        "arca",
        "arcas",
        "arclet",
        "arclets",
        "isaac",
        "dbcert",
        "dbcerts",
        "lakebox",
        "ucode",
        "dbexec",
        "spog",
    }
)
PHRASES = re.compile(
    r"databricks[\s_-]*(?:internal|corp(?:orate)?|employees?|network|only)"
    r"|(?:internal|corp(?:orate)?)[\s_-]*databricks"
    r"|databricks-eng(?:/|\b)"
    r"|\binternal[-_]beta\b"
    r"|\be2[-_]dogfood\b",
    re.IGNORECASE,
)
HOST_SUFFIXES = ("databricks.com", "databricksapps.com", "azuredatabricks.net")
PUBLIC_HOSTS = frozenset(
    {
        "www.databricks.com",
        "docs.databricks.com",
        "accounts.cloud.databricks.com",
        "login.databricks.com",
    }
)


def host_pattern(suffixes: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(suffix) for suffix in suffixes)
    return re.compile(
        r"(?<![\w.-])(?:[a-z0-9-]+\.)+(?:" + alternatives + r")(?![\w-]|\.[\w-])",
        re.IGNORECASE,
    )


HOST = host_pattern(HOST_SUFFIXES)


def _violation_lines(text: str) -> set[int]:
    """Return LF-based locations, including phrases that wrap across lines."""
    words = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", words)
    lines = set()
    original_lines = text.split("\n")
    for line_number, normalized in enumerate(words.split("\n"), 1):
        if PROHIBITED_NAMES.intersection(re.findall(r"[a-z]+", normalized.lower())) or any(
            match.group().lower() not in PUBLIC_HOSTS
            for match in HOST.finditer(original_lines[line_number - 1])
        ):
            lines.add(line_number)
    line_starts = [0, *(match.end() for match in re.finditer("\n", words))]
    position = 0
    while match := PHRASES.search(words, position):
        first_line = bisect_right(line_starts, match.start())
        for offset, segment in enumerate(match.group().split("\n")):
            if segment.strip():
                lines.add(first_line + offset)
        position = match.start() + 1
    return lines


def violates(text: str) -> bool:
    """Match names in prose, snake_case, camelCase, and filenames."""
    return bool(_violation_lines(text))


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    try:
        git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return False
        raise
    return True


def _default_upstream(root: Path) -> tuple[str, str] | None:
    try:
        reference = (
            git(root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").decode().strip()
        )
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        try:
            git(root, "show-ref", "--verify", "--quiet", "refs/remotes/origin/HEAD")
        except subprocess.CalledProcessError as missing:
            if missing.returncode != 1:
                raise
        else:
            return None
        reference = "refs/remotes/origin/main"
    prefix = "refs/remotes/origin/"
    if not reference.startswith(prefix) or reference == prefix:
        return None
    branch = "refs/heads/" + reference.removeprefix(prefix)
    try:
        tip = git(root, "rev-parse", "--verify", reference + "^{commit}").decode().strip()
    except subprocess.CalledProcessError:
        return None
    return branch, tip


def _has_unique_merge_base(root: Path, first: str, second: str) -> bool:
    try:
        ancestors = git(root, "merge-base", "--all", first, second).splitlines()
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return False
        raise
    return len(ancestors) == 1


def _staged_upstream_update(root: Path) -> str | None:
    """Recognize a current upstream-tip merge into a named feature branch."""
    merge_path = Path(git(root, "rev-parse", "--git-path", "MERGE_HEAD").decode().strip())
    if not merge_path.is_absolute():
        merge_path = root / merge_path
    if not merge_path.exists():
        return None
    incoming = merge_path.read_text().splitlines()
    if len(incoming) != 1 or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", incoming[0]):
        return None
    try:
        branch = git(root, "symbolic-ref", "--quiet", "HEAD").decode().strip()
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return None
        raise
    default = _default_upstream(root)
    if default is None:
        return None
    default_branch, upstream = default
    if branch == default_branch:
        return None
    if incoming[0] != upstream:
        return None
    if _is_ancestor(root, upstream, "HEAD") or not _has_unique_merge_base(root, "HEAD", upstream):
        return None
    return upstream


def _pushed_upstream_update(
    root: Path, base: str, head: str, destination: str | None
) -> str | None:
    """Recognize a verified upstream update published to a feature branch."""
    if destination is None or not destination.startswith("refs/heads/"):
        return None
    try:
        git(root, "check-ref-format", destination)
    except subprocess.CalledProcessError:
        return None
    if not _is_ancestor(root, base, head):
        return None
    merges = git(root, "rev-list", "--ancestry-path", "--merges", "--parents", f"{base}..{head}")
    if not merges.strip():
        return None
    try:
        branch = git(root, "symbolic-ref", "--quiet", "HEAD").decode().strip()
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return None
        raise
    default = _default_upstream(root)
    if default is None:
        return None
    default_branch, upstream = default
    if branch == default_branch or destination == default_branch:
        return None
    if git(root, "rev-parse", head).strip() != git(root, "rev-parse", "HEAD").strip():
        return None
    if _is_ancestor(root, upstream, base):
        return None
    candidates = []
    for merge in merges.decode().splitlines():
        parents = merge.split()[1:]
        if len(parents) != 2:
            return None
        if parents[1] == upstream and _is_ancestor(root, base, parents[0]):
            candidates.append(parents[0])
    if not candidates:
        return None
    if (
        len(candidates) != 1
        or not _is_ancestor(root, upstream, head)
        or not _has_unique_merge_base(root, candidates[0], upstream)
    ):
        return None
    return upstream


def check(
    root: Path,
    base: str | None = None,
    head: str | None = None,
    *,
    pre_push: bool = False,
    destination: str | None = None,
) -> list[str]:
    """Check staged changes, or the branch diff from a merge base."""
    if head and not base:
        raise ValueError("--head requires --base")
    revisions = ["--cached"]
    if base:
        target = head or "HEAD"
        ancestor = git(root, "merge-base", base, target).decode().strip()
        if pre_push:
            ancestor = _pushed_upstream_update(root, base, target, destination) or ancestor
        revisions = [ancestor, target]
    else:
        upstream = _staged_upstream_update(root)
        if upstream is not None:
            revisions.append(upstream)
    options = ["--no-ext-diff", "--no-textconv", "--no-color"]
    entries = iter(
        git(
            root,
            "diff",
            *options,
            "--find-renames",
            *revisions,
            "--raw",
            "--no-abbrev",
            "-z",
            "--diff-filter=AMRT",
            "--",
        ).split(b"\0")
    )
    findings = []
    for metadata in entries:
        if not metadata:
            break
        old_mode, mode, old_object_id, object_id, status = metadata[1:].split()
        old_path = os.fsdecode(next(entries))
        renamed = status.startswith(b"R")
        path = os.fsdecode(next(entries)) if renamed else old_path
        if path in POLICY_FILES:
            continue
        if (status == b"A" or renamed) and violates(path):
            findings.append(f"{path}:1: prohibited reference in new path")
        if mode == b"160000":
            continue
        contents = git(root, "cat-file", "blob", object_id.decode())
        if b"\0" in contents[:8000]:
            continue
        violation_lines = _violation_lines(contents.decode("utf-8", errors="replace"))
        if not violation_lines:
            continue
        if status == b"A" or old_mode == b"160000" or old_path in POLICY_FILES:
            added_lines = violation_lines
        else:
            patch = git(
                root,
                "diff",
                *options,
                "--text",
                "--unified=0",
                "--inter-hunk-context=0",
                old_object_id.decode(),
                object_id.decode(),
                "--",
            ).decode("utf-8", errors="replace")
            added_lines = set()
            line_number = None
            for line in patch.split("\n"):
                hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if hunk:
                    line_number = int(hunk[1])
                elif line_number is not None and line.startswith("+"):
                    added_lines.add(line_number)
                    line_number += 1
                elif line_number is not None and line.startswith(" "):
                    line_number += 1
        findings.extend(
            f"{path}:{line_number}: prohibited reference in added text"
            for line_number in sorted(violation_lines & added_lines)
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("PRE_COMMIT_FROM_REF"))
    parser.add_argument("--head", default=os.environ.get("PRE_COMMIT_TO_REF"))
    parser.add_argument(
        "--destination",
        default=os.environ.get("PRE_COMMIT_REMOTE_BRANCH"),
        help="Full destination branch ref for a pre-push check.",
    )
    parser.add_argument(
        "--pre-push",
        action="store_true",
        default="PRE_COMMIT_REMOTE_NAME" in os.environ,
        help="Require both push revisions.",
    )
    args = parser.parse_args()
    try:
        if args.pre_push and not (args.base and args.head):
            raise ValueError("--pre-push requires both --base and --head")
        findings = check(
            Path.cwd(),
            args.base,
            args.head,
            pre_push=args.pre_push,
            destination=args.destination,
        )
    except (subprocess.CalledProcessError, ValueError):
        print(
            "reference-policy: cannot determine changes; check the base/head revisions",
            file=sys.stderr,
        )
        return 2
    for finding in findings:
        print(finding)
    if findings:
        print("Use generic names and synthetic examples. Do not add inline suppressions.")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
