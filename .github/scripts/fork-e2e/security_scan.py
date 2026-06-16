#!/usr/bin/env python3
"""
Static security scan of a fork PR's unified diff, used as a gate before the
fork-e2e mirror runs the contributor's code with the test-gateway secret.

It reads diff TEXT only -- it never checks out or executes the fork's code --
so it is safe to run anywhere. It is defense-in-depth + a reviewer aid, NOT a
guarantee: an attacker can obfuscate past regexes, so maintainer approval
remains the primary gate. Its job is to (a) hard-fail on high-confidence
exfiltration shapes in ADDED lines, and (b) surface changes to files that run
during CI bootstrap so the reviewer looks harder.

Findings are two tiers:
  - BLOCKING -> clean=false (mirror is withheld unless a maintainer applies the
    override label): exfil shapes -- a secret-named credential source AND a
    network sink added to the same file; a wholesale ``os.environ`` dump; a
    decode-then-exec; or a raw TCP/reverse-shell sink.
  - INFO -> does not block: edits to CI-bootstrap-executed files (conftest.py,
    setup.py, pyproject build hooks, anything under .github/, pytest plugins).

Usage:   python3 security_scan.py <unified-diff-file>
Outputs (when $GITHUB_OUTPUT is set): ``clean=true|false`` and a one-line
``summary=...``. Always exits 0; callers gate on the ``clean`` output.
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

    :param diff: Full unified-diff text (e.g. from ``gh pr diff --patch``).
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


def scan_diff(diff: str) -> tuple[list[str], list[str]]:
    """
    Classify a unified diff into blocking and info findings.

    :param diff: Full unified-diff text.
    :returns: ``(blocking, info)`` -- two lists of human-readable finding
        strings. ``blocking`` non-empty means the scan is not clean.
    """
    by_file = _changed_files_and_added(diff)
    blocking: list[str] = []
    info: list[str] = []

    for path, added in by_file.items():
        body = "\n".join(added)
        has_net = bool(_NETWORK.search(body))
        has_secret = bool(_SECRET.search(body))
        if has_net and has_secret:
            blocking.append(f"exfil-shape (secret source + network sink) in {path}")
        for ln in added:
            if _STANDALONE.search(ln) and not (
                # a lone base64/decode call is INFO; only block decode+exec
                _DECODE.search(ln) and not _EXEC.search(ln)
            ):
                blocking.append(f"high-risk call in {path}: {ln.strip()[:80]}")
                break
            if _DECODE.search(ln) and _EXEC.search(ln):
                blocking.append(f"decode+exec in {path}: {ln.strip()[:80]}")
                break
        if _HIGH_RISK.search(path):
            info.append(f"touches CI-executed file: {path}")

    return blocking, info


def main(argv: list[str]) -> int:
    """
    Scan the diff file at ``argv[1]`` and emit ``clean``/``summary`` outputs.

    :param argv: Process argv; ``argv[1]`` is the unified-diff file path.
    :returns: Always 0 (callers gate on the ``clean`` GITHUB_OUTPUT value).
    """
    if len(argv) < 2:
        print("usage: security_scan.py <diff-file>", file=sys.stderr)
        return 0
    with open(argv[1], encoding="utf-8", errors="replace") as fh:
        diff = fh.read()
    blocking, info = scan_diff(diff)

    for f in blocking:
        print(f"BLOCKING: {f}")
    for f in info:
        print(f"info: {f}")

    clean = not blocking
    if clean:
        summary = f"clean ({len(info)} CI-file note(s))" if info else "clean"
    else:
        summary = f"{len(blocking)} blocking finding(s): " + "; ".join(blocking)
    summary = summary.replace("\n", " ")[:140]

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"clean={'true' if clean else 'false'}\n")
            fh.write(f"summary={summary}\n")
    print(f"clean={clean} :: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
