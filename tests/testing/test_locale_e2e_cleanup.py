"""Locale CLI regressions must not leave detached runtimes for the next test."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests.e2e import test_bundled_agent_nonutf8_locale_e2e as locale_e2e


@pytest.mark.parametrize("outcome", ["eof", "timeout", "decode_error"])
def test_locale_launch_reaps_only_its_detached_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Exercise the launch test's real teardown with owned and unrelated processes."""
    owned: list[subprocess.Popen[bytes]] = []
    foreign = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)", "omnigent-foreign-runtime"],
        env={**os.environ, "OMNIGENT_DATA_DIR": str(tmp_path / "another-test")},
        start_new_session=True,
    )
    close = Mock()

    def spawn(*_args: Any, env: dict[str, str], **_kwargs: Any) -> SimpleNamespace:
        runtime = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; time.sleep(120)",
                "omnigent.host._daemon_entry",
                "--local",
            ],
            env=env,
            start_new_session=True,
        )
        owned.append(runtime)
        return SimpleNamespace(
            expect=lambda *_a, **_k: {"decode_error": 0, "eof": 1, "timeout": 2}[outcome],
            before="",
            after="UnicodeDecodeError",
            close=close,
        )

    monkeypatch.setattr(locale_e2e, "_preferred_encoding", lambda _env: "ascii")
    monkeypatch.setattr(locale_e2e.pexpect, "spawn", spawn)
    try:
        expected = (
            pytest.raises(pytest.fail.Exception, match="crashed with UnicodeDecodeError")
            if outcome == "decode_error"
            else contextlib.nullcontext()
        )
        with expected:
            locale_e2e.test_bundled_agent_launch_survives_non_utf8_locale("debby", tmp_path)
        close.assert_called_once_with(force=True)
        assert len(owned) == 1
        assert owned[0].poll() is not None, "Locale test left its detached runtime alive"
        assert foreign.poll() is None, "Locale cleanup stopped another test's runtime"
    finally:
        for process in [*owned, foreign]:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
