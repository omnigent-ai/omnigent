#!/usr/bin/env python3
"""Scan a PR's *added* lines for committed secrets.

Called by .github/workflows/security-gate.yml. Dependency-free (stdlib only)
so it runs without a network install. Operates on a unified diff and inspects
only added (`+`) lines, so it flags secrets the PR introduces, not pre-existing
ones -- and reports them at the right file/line for inline annotations.

Detection is two-pronged:
  * High-confidence provider token shapes (AWS, GitHub, Slack, Google, private
    keys) -- low false-positive, reported as errors.
  * Generic high-entropy assignments to secret-looking names
    (token/secret/password/api_key=...) -- reported as errors when the value is
    long and high-entropy.

This is intentionally a curated, hermetic baseline, not a replacement for
gitleaks/trufflehog; those can be layered in later once an org license / pinned
action SHA is settled (see plan).

Env in:  DIFF_FILE (path to a `git diff base...head` unified diff).
Exit:    non-zero if any secret is found; 0 otherwise.
"""

from __future__ import annotations

import math
import os
import re
import sys

HIGH_CONFIDENCE = [
    ("AWS access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    (
        "private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    ("Stripe secret key", re.compile(r"\b(sk|rk)_live_[0-9A-Za-z]{24,}\b")),
]

# name = "value"  /  name: value  /  name=value  for secret-ish names.
ASSIGN_RE = re.compile(
    r"""(?ix)
    \b(?P<name>[a-z0-9_\-\.]*(?:secret|token|passwd|password|api[_\-]?key|access[_\-]?key|private[_\-]?key)[a-z0-9_\-\.]*)
    \s*[:=]\s*
    ['"]?(?P<value>[A-Za-z0-9+/_\-\.=]{20,})['"]?
    """
)
# Values that look like references/placeholders, not real secrets.
PLACEHOLDER_RE = re.compile(
    r"(?i)\$\{|\$\(|secrets\.|env\.|vars\.|os\.environ|getenv|process\.env"
    r"|example|placeholder|changeme|your[_\-]?|xxx|<.*>|\*{4,}|redacted|dummy|fake|todo"
)


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def scan_value(value: str) -> bool:
    """Generic heuristic: long, high-entropy, not an obvious placeholder."""
    if PLACEHOLDER_RE.search(value):
        return False
    if len(value) < 20:
        return False
    return shannon_entropy(value) >= 4.0


def main() -> int:
    diff_path = os.environ.get("DIFF_FILE")
    if not diff_path or not os.path.isfile(diff_path):
        print(f"::error::diff file {diff_path!r} missing")
        return 1

    findings: list[str] = []
    cur_file = "?"
    new_lineno = 0

    with open(diff_path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if line.startswith("+++ "):
                cur_file = line[6:] if line.startswith("+++ b/") else line[4:]
                continue
            if line.startswith("@@"):
                m = re.search(r"\+(\d+)", line)
                new_lineno = int(m.group(1)) if m else 0
                continue
            if line.startswith("+") and not line.startswith("+++"):
                added = line[1:]
                for label, rx in HIGH_CONFIDENCE:
                    if rx.search(added):
                        findings.append(
                            f"::error file={cur_file},line={new_lineno}::"
                            f"possible committed secret ({label})."
                        )
                        break
                else:
                    m = ASSIGN_RE.search(added)
                    if m and scan_value(m.group("value")):
                        findings.append(
                            f"::error file={cur_file},line={new_lineno}::"
                            f"possible hardcoded secret assigned to '{m.group('name')}' "
                            "(long, high-entropy value)."
                        )
                new_lineno += 1
            elif not line.startswith("-"):
                # context line advances the new-file counter too
                new_lineno += 1

    for f in findings:
        print(f)
    if findings:
        print(f"::error::Secret scan found {len(findings)} candidate secret(s) in added lines.")
        return 1
    print("Secret scan passed (no secrets in added lines).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
