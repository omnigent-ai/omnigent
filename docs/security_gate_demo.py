"""Deliberately malicious sample for testing the Security Scan (test PR #289).

This file is NOT real code -- it exists only to prove the deterministic
detectors fire. Delete it (and revert the security-scan.yml test edits) before
merge.
"""

import base64

# 1. Committed secret -> secret-scan.py (AWS access-key-id shape; this is the
#    well-known AWS documentation EXAMPLE key, not a live credential).
aws_access_key_id = "AKIAIOSFODNN7EXAMPLE"

# 2. Obfuscated code execution -> semgrep rule `exec-on-decoded-payload`.
exec(base64.b64decode("cHJpbnQoJ3B3bmVkJyk="))
