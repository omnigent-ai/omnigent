#!/usr/bin/env bash
# Flags PR changes to security-sensitive paths. Called by
# .github/workflows/security-scan.yml after the trust gate opens.
#
# Two tiers:
#   FAIL  -- paths that let a PR escalate privilege or rewrite the trust model:
#            CI workflows, the maintainer list, code owners. An untrusted
#            author has no business editing these; a real need is unblocked by
#            a maintainer reviewing and merging the change anyway.
#   WARN  -- build/test hooks that execute code at install or collection time
#            (setup.py, pyproject build backends, conftest.py) and the lockfile.
#            Not auto-failed (legit PRs touch them), but surfaced as annotations
#            so a reviewer looks closely. semgrep + the secret scan still run on
#            their contents.
#
# Env in:  CHANGED_FILES (path to a file with one changed path per line).
# Exit:    non-zero if any FAIL-tier path changed; 0 otherwise.

set -euo pipefail

CHANGED="${CHANGED_FILES:?CHANGED_FILES not set}"
[[ -f "$CHANGED" ]] || { echo "::error::changed-files list $CHANGED missing"; exit 1; }

fail=0

while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  case "$path" in
    .github/workflows/*)
      echo "::error file=$path::Untrusted PR edits a CI workflow. Workflow changes can exfiltrate secrets or weaken gates; a maintainer must review."
      fail=1
      ;;
    .github/MAINTAINER)
      echo "::error file=$path::Untrusted PR edits .github/MAINTAINER (the maintainer allowlist). Self-granting maintainership is blocked."
      fail=1
      ;;
    .github/CODEOWNERS | CODEOWNERS | docs/CODEOWNERS)
      echo "::error file=$path::Untrusted PR edits CODEOWNERS. Review-routing changes must be made by a maintainer."
      fail=1
      ;;
    .github/scripts/*)
      echo "::error file=$path::Untrusted PR edits a CI helper script under .github/scripts. These run in privileged workflows; a maintainer must review."
      fail=1
      ;;
    setup.py | */setup.py | pyproject.toml | */pyproject.toml | conftest.py | */conftest.py)
      echo "::warning file=$path::PR edits a build/test hook that runs code at install or collection time. Review for code execution side effects."
      ;;
    uv.lock | */uv.lock | package-lock.json | */package-lock.json | yarn.lock | */yarn.lock)
      echo "::warning file=$path::PR edits a dependency lockfile. Review for dependency-confusion / typosquat / repointed sources."
      ;;
  esac
done < "$CHANGED"

if [[ "$fail" -ne 0 ]]; then
  echo "::error::Sensitive-path guard failed: this PR modifies privileged repo configuration."
  exit 1
fi
echo "Sensitive-path guard passed."
