#!/usr/bin/env bash
# TEMPORARY pre-merge verification harness for the Security Gate detectors.
#
# Runs each detector against crafted malicious + benign fixtures and asserts the
# exit codes, so CI proves the scan blocks/permits correctly BEFORE the scanner
# lands on main (where the gate otherwise fail-opens as bootstrap and never
# exercises the detectors). Driven by .github/workflows/security-gate-selftest.yml.
#
# REMOVE this file and that workflow once the gate is validated and merged.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SS="$ROOT/.github/scripts/security-scan"
RULES="$ROOT/.github/security/semgrep-rules.yml"
WORK="$(mktemp -d)"
fail=0

pass() { echo "  PASS: $1"; }
bad() {
  echo "  FAIL: $1"
  fail=1
}

echo "== secret-scan =="
# Malicious: an AWS-key-shaped token. Built from parts so THIS source file holds
# no contiguous token (and so secret-scan never flags the harness itself).
KEY="AKIA""IOSFODNN7EXAMPLE"
printf '+++ b/config.py\n@@ -0,0 +1,2 @@\n+aws_key = "%s"\n+greeting = "hello world plain text value"\n' "$KEY" >"$WORK/bad.diff"
if DIFF_FILE="$WORK/bad.diff" python3 "$SS/secret-scan.py" >/dev/null 2>&1; then bad "should flag committed AWS key"; else pass "flagged committed AWS key"; fi
# Benign: a secrets.* reference (placeholder, not a real secret).
printf '+++ b/ci.yml\n@@ -0,0 +1,1 @@\n+  token: from-secrets-context-placeholder-${secrets.MY_TOKEN}\n' >"$WORK/ok.diff"
if DIFF_FILE="$WORK/ok.diff" python3 "$SS/secret-scan.py" >/dev/null 2>&1; then pass "allowed secrets.* reference"; else bad "false-positive on secrets.* reference"; fi

echo "== sensitive-paths =="
printf '.github/workflows/evil.yml\nsrc/app.py\n' >"$WORK/changed_bad.txt"
if CHANGED_FILES="$WORK/changed_bad.txt" bash "$SS/sensitive-paths.sh" >/dev/null 2>&1; then bad "should fail on workflow edit"; else pass "failed on workflow edit"; fi
printf 'src/app.py\nREADME.md\n' >"$WORK/changed_ok.txt"
if CHANGED_FILES="$WORK/changed_ok.txt" bash "$SS/sensitive-paths.sh" >/dev/null 2>&1; then pass "allowed benign paths"; else bad "false-positive on benign paths"; fi

echo "== lint-workflow-misuse =="
# Malicious workflow: pull_request_target + PR-head checkout + unpinned action.
# The literal `${{` is assembled at runtime so it is not interpolated anywhere.
mkdir -p "$WORK/pr/.github/workflows"
{
  echo 'on:'
  echo '  pull_request_target:'
  echo 'jobs:'
  echo '  x:'
  echo '    steps:'
  echo '      - uses: actions/checkout@v4'
  echo '        with:'
  printf '          ref: %s{{ github.event.pull_request.head.sha }}\n' '$'
} >"$WORK/pr/.github/workflows/evil.yml"
printf '.github/workflows/evil.yml\n' >"$WORK/wf_bad.txt"
if (cd "$WORK/pr" && CHANGED_FILES="$WORK/wf_bad.txt" python3 "$SS/lint-workflow-misuse.py" >/dev/null 2>&1); then bad "should fail on prt+head / unpinned action"; else pass "failed on prt+head / unpinned action"; fi
# Benign workflow: pull_request + SHA-pinned action.
mkdir -p "$WORK/pr2/.github/workflows"
{
  echo 'on:'
  echo '  pull_request:'
  echo 'jobs:'
  echo '  x:'
  echo '    steps:'
  echo '      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd  # v6.0.2'
} >"$WORK/pr2/.github/workflows/good.yml"
printf '.github/workflows/good.yml\n' >"$WORK/wf_ok.txt"
if (cd "$WORK/pr2" && CHANGED_FILES="$WORK/wf_ok.txt" python3 "$SS/lint-workflow-misuse.py" >/dev/null 2>&1); then pass "allowed clean pinned workflow"; else bad "false-positive on clean pinned workflow"; fi

echo "== semgrep (local rules) =="
cat >"$WORK/evil.py" <<'PY'
import base64
exec(base64.b64decode("cHJpbnQoMSk="))
PY
cat >"$WORK/ok.py" <<'PY'
def add(a, b):
    return a + b
PY
if uvx semgrep scan --config "$RULES" --severity=ERROR --error --metrics=off --quiet "$WORK/evil.py" >/dev/null 2>&1; then bad "semgrep should flag exec(base64...)"; else pass "semgrep flagged exec(base64...)"; fi
if uvx semgrep scan --config "$RULES" --severity=ERROR --error --metrics=off --quiet "$WORK/ok.py" >/dev/null 2>&1; then pass "semgrep clean on benign file"; else bad "semgrep false-positive on benign file"; fi

rm -rf "$WORK"
if [ "$fail" -ne 0 ]; then
  echo "SELFTEST FAILED"
  exit 1
fi
echo "SELFTEST PASSED"
