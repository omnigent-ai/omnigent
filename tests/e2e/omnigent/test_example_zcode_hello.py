"""Run the ZCode example through the real CLI with a deterministic vendor stub."""

from __future__ import annotations

import json
import os
from pathlib import Path

from tests.e2e.omnigent._example_helpers import assert_completed_one_shot, run_one_shot
from tests.inner.test_zcode import _write_fake_zcode


def test_zcode_hello_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    tmp_path: Path,
) -> None:
    binary = tmp_path / "zcode"
    log = tmp_path / "argv.jsonl"
    _write_fake_zcode(binary)
    credentials = tmp_path / ".zcode" / "v2" / "credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("test-credential-store")
    env = {
        **mock_credentials_env,
        "PATH": f"{tmp_path}{os.pathsep}{mock_credentials_env.get('PATH', '')}",
        "OMNIGENT_ZCODE_PATH": str(binary),
        "ZCODE_DATA_BASE_DIR": str(tmp_path),
        "ZCODE_TEST_LOG": str(log),
    }
    result = run_one_shot(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=env,
        example_name="zcode_hello",
        prompt="say pong",
        harness=None,
        model=None,
    )
    assert_completed_one_shot(result, "zcode_hello")
    assert "pong" in result.stdout
    args = json.loads(log.read_text().splitlines()[0])["argv"]
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--mode") + 1] == "yolo"
