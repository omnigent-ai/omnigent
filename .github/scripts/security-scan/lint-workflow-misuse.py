#!/usr/bin/env python3
"""Lint changed GitHub Actions workflows for the two highest-signal CI attacks.

Called by .github/workflows/security-gate.yml. Dependency-free (stdlib +
regex line scanning, no PyYAML) so it never needs a network install to run --
a security check should not depend on fetching anything.

Checks, per changed `.github/workflows/*.yml`:

  1. pull_request_target + PR-head checkout (CRITICAL). The classic OSS
     supply-chain RCE: a `pull_request_target` workflow runs from the base with
     secrets, and if it also checks out / runs the PR head it executes
     attacker code with secrets in scope. We flag any checkout that pulls a
     PR-head ref (github.event.pull_request.head.*, github.head_ref,
     refs/pull/...). A `# leak-scan-allow: pull_request_target` line (the
     repo's existing convention for hand-audited exceptions) downgrades it to
     a warning -- safe here because untrusted authors are independently blocked
     from editing workflows by sensitive-paths.sh.

  2. Unpinned action references (HIGH). `uses: owner/repo@v4` / `@main` lets the
     action's owner change what runs under our token later. Require a 40-hex
     commit SHA. Local (`./`) and `docker://...@sha256:` refs are exempt.

Env in:  CHANGED_FILES (path to a file with one changed path per line).
Exit:    non-zero if any CRITICAL/HIGH finding; 0 otherwise.
"""

from __future__ import annotations

import os
import re
import sys

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"""^\s*-?\s*uses:\s*['"]?([^'"\s#]+)['"]?""")
# PR-head refs that must never be checked out under pull_request_target.
HEAD_REF_RE = re.compile(
    r"github\.event\.pull_request\.head\.(sha|ref)"
    r"|github\.head_ref"
    r"|refs/pull/",
)


def is_pinned(ref: str) -> bool:
    if ref.startswith(("./", "../")):
        return True  # local action, ships with the repo
    if ref.startswith("docker://"):
        return "@sha256:" in ref  # digest-pinned image
    _, _, version = ref.partition("@")
    return bool(SHA_RE.match(version))


def lint_file(path: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        warnings.append(f"::warning file={path}::could not read workflow ({e})")
        return errors, warnings

    lines = text.splitlines()
    allow_prt = "leak-scan-allow: pull_request_target" in text
    has_prt = re.search(r"^\s*pull_request_target\s*:", text, re.MULTILINE) is not None

    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue

        # 1. PR-head checkout under pull_request_target.
        if has_prt and HEAD_REF_RE.search(line):
            msg = (
                f"file={path},line={i}::pull_request_target workflow references a "
                "PR-head ref -- this runs untrusted PR code with secrets. "
                "Check out 'main' only, or read the PR via the API."
            )
            (warnings if allow_prt else errors).append(
                ("::warning " if allow_prt else "::error ") + msg
            )

        # 2. Unpinned action reference.
        m = USES_RE.match(line)
        if m:
            ref = m.group(1)
            if "@" in ref and not is_pinned(ref):
                errors.append(
                    f"::error file={path},line={i}::action '{ref}' is not pinned to a "
                    "full commit SHA; a tag/branch ref can be moved to hostile code."
                )

    return errors, warnings


def main() -> int:
    changed = os.environ.get("CHANGED_FILES")
    if not changed or not os.path.isfile(changed):
        print(f"::error::changed-files list {changed!r} missing")
        return 1

    with open(changed, encoding="utf-8") as fh:
        paths = [p.strip() for p in fh if p.strip()]

    targets = [
        p
        for p in paths
        if p.startswith(".github/workflows/")
        and p.endswith((".yml", ".yaml"))
        and os.path.isfile(p)
    ]
    if not targets:
        print("No changed workflow files to lint.")
        return 0

    all_errors: list[str] = []
    for path in targets:
        errors, warnings = lint_file(path)
        for w in warnings:
            print(w)
        for e in errors:
            print(e)
        all_errors.extend(errors)

    if all_errors:
        print(f"::error::Workflow misuse linter failed with {len(all_errors)} finding(s).")
        return 1
    print(f"Workflow misuse linter passed ({len(targets)} file(s) checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
