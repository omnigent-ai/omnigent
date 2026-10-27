"""Host console failures must not change the process's final exit status."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.host import connect as host_connect

_CHILD = """
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.host import connect as hc
from omnigent.host.identity import HostIdentity

stream_name, mode, code, directory = sys.argv[1:]
directory = Path(directory)
rows = directory / "events.jsonl"

class Capture(logging.Handler):
    def emit(self, record):
        with rows.open("a") as output:
            output.write(json.dumps({
                "event": getattr(record, "event_name", None),
                "attributes": getattr(record, "attributes", {}),
                "message": record.getMessage(),
            }) + "\\n")

root = logging.getLogger()
root.setLevel(logging.INFO)
root.handlers = [logging.StreamHandler(getattr(sys, stream_name)), Capture()]

if mode == "status":
    hc._print_host_status("test status", stderr=stream_name == "stderr")
    hc._print_host_status("next status", stderr=stream_name == "stderr")
    hc._logger.info("file logging remains available")
    raise SystemExit(int(code))

with (
    patch.object(hc, "publish_origin_identity"),
    patch.object(hc, "configure_process_logging", return_value=directory / "host.log"),
    patch.object(hc, "load_or_create_host_identity",
                 return_value=HostIdentity(host_id="test", name="test")),
    patch.object(hc, "_generate_ucode_configs"),
    patch("omnigent.host.identity.export_configured_slice_key"),
    patch("omnigent.runtime.telemetry.init"),
    patch("omnigent.git_credential_github.configure_host_git"),
    patch("omnigent.git_credential_github.configure_host_gh"),
    patch("omnigent.git_credential_github.start_host_gh_refresh"),
    patch("omnigent.host.databricks_credential.configure_host_databricks"),
    patch.object(hc.HostProcess, "run",
                 new=AsyncMock(side_effect=hc.HostConnectError("HTTP 403: refused"))),
):
    hc.run_host_process("https://host.example", directory / "config.yaml",
                        interactive_shells=["bash"])
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe descriptors")
@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
@pytest.mark.parametrize(
    "mode,code", [("status", 0), ("status", HOST_FATAL_EXIT_CODE), ("fatal", HOST_FATAL_EXIT_CODE)]
)
def test_closed_console_preserves_process_exit(
    tmp_path: Path, stream_name: str, mode: str, code: int
) -> None:
    """Exercise Python's final flush, including a log handler sharing the failed pipe."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)
    env["OMNIGENT_CONFIG_HOME"] = str(tmp_path / "config")
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "data")
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, stream_name, mode, str(code), str(tmp_path)],
            stdout=write_fd if stream_name == "stdout" else subprocess.PIPE,
            stderr=write_fd if stream_name == "stderr" else subprocess.PIPE,
            env=env,
            timeout=30,
        )
    finally:
        os.close(write_fd)

    assert result.returncode == code, result.stderr
    assert b"Exception ignored" not in (result.stderr or b"")
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert any(row["event"] == "host_console_write_failed" for row in rows)
    if mode == "status":
        assert any(row["message"] == "file logging remains available" for row in rows)
    else:
        exits = [row for row in rows if row["event"] == "host_exiting"]
        assert len(exits) == 1
        assert exits[0]["attributes"]["reason"] == "fatal_connect"
        assert exits[0]["attributes"]["exit_code"] == HOST_FATAL_EXIT_CODE
        assert "HTTP 403" in exits[0]["attributes"]["error"]


@pytest.mark.parametrize("console_error", [BrokenPipeError(), OSError(errno.EIO, "I/O error")])
def test_mocked_print_does_not_redirect_a_healthy_console(
    monkeypatch: pytest.MonkeyPatch, console_error: OSError
) -> None:
    """A print stub raising an error says nothing about the actual descriptor."""
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    monkeypatch.setattr(host_connect, "print", Mock(side_effect=console_error), raising=False)
    redirect = Mock()
    monkeypatch.setattr(host_connect.os, "dup2", redirect)
    host_connect._print_host_status("test status")
    redirect.assert_not_called()


def test_replacement_stream_is_not_redirected(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = Mock()
    stream.write.side_effect = BrokenPipeError()
    monkeypatch.setattr(sys, "stdout", stream)
    redirect = Mock()
    monkeypatch.setattr(host_connect.os, "dup2", redirect)
    host_connect._print_host_status("test status")
    redirect.assert_not_called()
    stream.fileno.assert_not_called()
