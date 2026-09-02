from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "serve-gbrain.sh"


def _fake_gbrain(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "gbrain-calls"
    executable = bin_dir / "gbrain"
    executable.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$GBRAIN_TEST_LOG"\n'
        'if [ "$1" = "--version" ]; then\n'
        '  printf "%s\\n" "${GBRAIN_TEST_VERSION:-gbrain 0.46.30.0}"\n'
        "fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, log_path


def _env(tmp_path: Path, bin_dir: Path, log_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GBRAIN_HOME": str(tmp_path / "state"),
        "GBRAIN_PUBLIC_URL": "https://brain.example.com",
        "GBRAIN_ADMIN_BOOTSTRAP_TOKEN": "bootstrap-token",
        "GBRAIN_NO_EMBEDDING": "1",
        "GBRAIN_TEST_LOG": str(log_path),
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(_SCRIPT)],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_entrypoint_requires_dedicated_postgres_before_running_gbrain(
    tmp_path: Path,
) -> None:
    bin_dir, log_path = _fake_gbrain(tmp_path)

    result = _run(_env(tmp_path, bin_dir, log_path))

    assert result.returncode != 0
    assert "GBRAIN_DATABASE_URL" in result.stderr
    assert not log_path.exists()


def test_entrypoint_rejects_an_unpinned_gbrain_binary(tmp_path: Path) -> None:
    bin_dir, log_path = _fake_gbrain(tmp_path)
    env = _env(tmp_path, bin_dir, log_path)
    env["GBRAIN_DATABASE_URL"] = "postgresql://gbrain:secret@db/gbrain"
    env["GBRAIN_TEST_VERSION"] = "gbrain 0.47.0"

    result = _run(env)

    assert result.returncode != 0
    assert "expected gbrain 0.46.30.0" in result.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == ["--version"]


@pytest.mark.parametrize("initialized", [False, True])
def test_entrypoint_initializes_checks_and_serves_postgres(
    tmp_path: Path,
    initialized: bool,
) -> None:
    bin_dir, log_path = _fake_gbrain(tmp_path)
    env = _env(tmp_path, bin_dir, log_path)
    database_url = "postgresql://gbrain:secret@db/gbrain"
    env["GBRAIN_DATABASE_URL"] = database_url
    state_dir = Path(env["GBRAIN_HOME"])
    if initialized:
        (state_dir / ".gbrain").mkdir(parents=True)
        (state_dir / ".gbrain" / "config.json").write_text(
            json.dumps(
                {
                    "engine": "postgres",
                    "database_url": database_url,
                    "embedding_disabled": True,
                }
            ),
            encoding="utf-8",
        )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    calls = log_path.read_text(encoding="utf-8").splitlines()
    assert calls[0] == "--version"
    if initialized:
        assert not any(call.startswith("init ") for call in calls)
    else:
        assert f"init --url {database_url} --non-interactive --no-embedding" in calls
    assert "doctor" in calls
    assert calls[-1] == (
        "serve --http --bind 0.0.0.0 --port 3131 "
        "--public-url https://brain.example.com --surface starter "
        "--suppress-bootstrap-token"
    )


def test_entrypoint_rejects_persisted_database_drift(tmp_path: Path) -> None:
    bin_dir, log_path = _fake_gbrain(tmp_path)
    env = _env(tmp_path, bin_dir, log_path)
    env["GBRAIN_DATABASE_URL"] = "postgresql://gbrain:secret@db/gbrain"
    config_dir = Path(env["GBRAIN_HOME"]) / ".gbrain"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "engine": "postgres",
                "database_url": "postgresql://gbrain:old-secret@db/gbrain",
                "embedding_disabled": True,
            }
        ),
        encoding="utf-8",
    )

    result = _run(env)

    assert result.returncode != 0
    assert "differs from this deployment" in result.stderr
    assert "old-secret" not in result.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == ["--version"]
