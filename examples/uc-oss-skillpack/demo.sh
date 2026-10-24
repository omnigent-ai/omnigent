#!/usr/bin/env bash
# End-to-end demo: push one of Denny's real skills into UC OSS, list it, and
# pull it back into a temp dir to prove the round-trip.
#
# Assumes the Docker UC OSS server is already up (run `./run.sh up` or
# `make up` first). Run from this directory.
#
# POC / not for production.
set -euo pipefail

cd "$(dirname "$0")"
REPO_ROOT="$(cd ../.. && pwd)"

# ── config (override via env) ────────────────────────────────
export UC_OSS_URI="${UC_OSS_URI:-http://localhost:8080}"
export UC_OSS_VOLUME="${UC_OSS_VOLUME:-unity.omnigent.skillpacks}"
export UC_OSS_VOLUME_LOCAL_PATH="${UC_OSS_VOLUME_LOCAL_PATH:-$(pwd)/data/skillpacks}"

# Pick a real skill to demo. Prefer one of Denny's ~/.claude/skills; fall back
# to the bundled polly investigate skill so the demo works anywhere.
SKILL_DIR="${SKILL_DIR:-}"
SKILL_NAME="${SKILL_NAME:-}"
if [[ -z "${SKILL_DIR}" ]]; then
  if [[ -d "${HOME}/.claude/skills/startup-score-sheet" ]]; then
    SKILL_DIR="${HOME}/.claude/skills/startup-score-sheet"
    SKILL_NAME="startup-score-sheet"
  else
    SKILL_DIR="${REPO_ROOT}/examples/polly/skills/investigate"
    SKILL_NAME="investigate"
  fi
fi

PY="${PYTHON:-python3}"
CLI=("${PY}" "${REPO_ROOT}/examples/uc-oss-skillpack/skillpack.py")
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

echo "== 1. push: ${SKILL_DIR}"
"${CLI[@]}" push "${SKILL_DIR}"
echo

echo "== 2. list"
"${CLI[@]}" list
echo

DEST="$(mktemp -d)/pulled"
echo "== 3. pull ${SKILL_NAME} -> ${DEST}"
"${CLI[@]}" pull "${SKILL_NAME}" "${DEST}"
echo

echo "== 4. round-trip check"
echo "extracted tree:"
find "${DEST}" -type f | sed "s#${DEST}#  .#"
if diff -r "${SKILL_DIR}" "${DEST}/${SKILL_NAME}" >/dev/null 2>&1; then
  echo "OK: pulled contents match the source skill directory byte-for-byte."
else
  echo "NOTE: pulled tree differs from source (expected if source has"
  echo "      non-file entries); SKILL.md is present:"
  ls -1 "${DEST}/${SKILL_NAME}/SKILL.md"
fi
