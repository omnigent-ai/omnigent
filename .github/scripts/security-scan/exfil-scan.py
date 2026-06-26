#!/usr/bin/env python3
"""Scan a PR's *added* lines for secret-exfiltration and obfuscated-exec shapes.

Part of the single contributor Security Scan (.github/workflows/security-scan.yml),
the companion to secret-scan.py: that one flags secrets a PR *commits*, this one
flags code a PR adds to *steal* the CI secrets it runs with (the test-gateway
token, GITHUB_TOKEN) -- an env-secret read piped to the network is the shape that
matters.

It reads diff TEXT only -- it never checks out or executes the PR's code -- so it
is safe on any event. It is defense-in-depth + a reviewer aid, NOT a guarantee:
an attacker can obfuscate past regexes, so maintainer review remains the primary
gate. Its job is to (a) hard-fail on high-confidence exfiltration shapes in ADDED
lines, and (b) surface changes to files that run during CI bootstrap so the
reviewer looks harder.

Findings are two tiers:
  - BLOCKING -> non-zero exit: exfil shapes -- a secret-named credential source
    AND a network sink added to the same file; a wholesale ``os.environ`` dump; a
    decode-then-exec; or a raw TCP / reverse-shell sink.
  - INFO -> ``::warning`` only: edits to CI-bootstrap-executed files (conftest.py,
    setup.py, pyproject build hooks, anything under .github/, pytest plugins).

Env in:  DIFF_FILE (path to a ``git diff base...head`` / ``gh pr diff`` unified diff).
Exit:    non-zero if any BLOCKING finding; 0 otherwise.
"""

from __future__ import annotations

import os
import re
import sys

# Network / exfil sinks.
_NETWORK = re.compile(
    r"requests\.(get|post|put|patch|request|Session)"
    r"|urllib\.request|urlopen|httpx\.|aiohttp|http\.client"
    r"|socket\.(socket|create_connection)|telnetlib|smtplib|ftplib"
    r"|\bcurl\b|\bwget\b|\bnc\b|fetch\(|XMLHttpRequest|axios",
    re.IGNORECASE,
)

# Secret-NAMED credential sources (deliberately narrow: generic os.environ /
# LLM_API_KEY use is normal in tests, so it is INFO-only, not blocking).
_SECRET = re.compile(
    r"DATABRICKS_(CLIENT_ID|CLIENT_SECRET|TOKEN|BEARER)"
    r"|FORK_E2E_APP_PRIVATE_KEY|PRIVATE_KEY|[A-Z0-9]+_SECRET\b"
    # No bare ACCESS_TOKEN: case-insensitively it matches common `access_token`
    # OAuth/JSON fields and would block legit PRs. The specific secret names
    # above stay; generic-token exfil is left to the reviewer + LLM advisory.
    r"|GITHUB_TOKEN|\bGH_TOKEN\b|\.databrickscfg",
    re.IGNORECASE,
)

# Always-blocking single-line shapes (independent of co-occurrence).
_STANDALONE = re.compile(
    r"/dev/tcp/"  # bash reverse shell
    # Wholesale environ dump only -- a bare `os.environ)` matched benign
    # `helper(os.environ)` and is dropped to avoid false positives.
    r"|(json\.dumps|dict|str|repr)\(\s*os\.environ"  # dump the whole environ
    r"|\beval\s*\(|\bexec\s*\(|__import__\s*\("  # dynamic exec
    r"|pickle\.loads|marshal\.loads"  # deserialization exec
    r"|base64\.(b64decode|decodebytes)|codecs\.decode",  # decode (paired below)
    re.IGNORECASE,
)
_DECODE = re.compile(r"base64|b64decode|decodebytes|fromhex|codecs\.decode", re.IGNORECASE)
_EXEC = re.compile(
    r"\beval\s*\(|\bexec\s*\(|__import__\s*\(|subprocess|os\.system|popen", re.IGNORECASE
)

# Files that execute during `uv sync` / pytest collection -- INFO, so the
# reviewer scrutinizes them even when no exfil pattern is present.
_HIGH_RISK = re.compile(
    r"(^|/)conftest\.py$|(^|/)setup\.py$|(^|/)pyproject\.toml$"
    r"|^\.github/|(^|/)sitecustomize\.py$|\.pth$"
    r"|(^|/)_token_usage\.py$|(^|/)noxfile\.py$|(^|/)tox\.ini$|(^|/)Makefile$",
)


def _changed_files_and_added(diff: str) -> dict[str, list[str]]:
    """
    Group a unified diff's ADDED lines by destination file.

    :param diff: Full unified-diff text (e.g. from ``gh pr diff``).
    :returns: Mapping of file path (e.g. ``"tests/conftest.py"``) to the list of
        added line bodies (without the leading ``+``); diff headers excluded.
    """
    by_file: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            by_file.setdefault(current, [])
        elif line.startswith(("+++ ", "diff --git")):
            current = None
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            by_file[current].append(line[1:])
    return by_file


def scan_diff(diff: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    Classify a unified diff into blocking and info findings.

    :param diff: Full unified-diff text.
    :returns: ``(blocking, info)`` -- two lists of ``(path, message)`` tuples.
        ``blocking`` non-empty means the scan is not clean.
    """
    by_file = _changed_files_and_added(diff)
    blocking: list[tuple[str, str]] = []
    info: list[tuple[str, str]] = []

    for path, added in by_file.items():
        body = "\n".join(added)
        has_net = bool(_NETWORK.search(body))
        has_secret = bool(_SECRET.search(body))
        if has_net and has_secret:
            blocking.append((path, "exfil shape: secret-named source + network sink in one file"))
        for ln in added:
            if _STANDALONE.search(ln) and not (
                # a lone base64/decode call is INFO; only block decode+exec
                _DECODE.search(ln) and not _EXEC.search(ln)
            ):
                blocking.append((path, f"high-risk call: {ln.strip()[:80]}"))
                break
            if _DECODE.search(ln) and _EXEC.search(ln):
                blocking.append((path, f"decode+exec: {ln.strip()[:80]}"))
                break
        if _HIGH_RISK.search(path):
            info.append((path, "touches a file that runs during CI bootstrap; review closely"))

    return blocking, info


def main() -> int:
    """
    Scan the diff at ``$DIFF_FILE`` and report exfil / obfuscated-exec findings.

    :returns: 1 if any blocking finding, else 0.
    """
    diff_path = os.environ.get("DIFF_FILE")
    if not diff_path or not os.path.isfile(diff_path):
        print(f"::error::diff file {diff_path!r} missing")
        return 1

    with open(diff_path, encoding="utf-8", errors="replace") as fh:
        diff = fh.read()
    blocking, info = scan_diff(diff)

    for path, msg in info:
        print(f"::warning file={path}::{msg}")
    for path, msg in blocking:
        print(f"::error file={path}::{msg}")

    if blocking:
        print(f"::error::Exfil scan found {len(blocking)} blocking finding(s) in added lines.")
        return 1
    print(f"Exfil scan passed ({len(info)} CI-file note(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
