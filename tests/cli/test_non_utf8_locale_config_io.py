"""Tests for omnigent.cli's locale-independent config I/O.

omnigent-authored files (bundled agent configs, agent specs, daemon
records, the global config) are UTF-8 on disk. Reading them must not
depend on ``locale.getpreferredencoding()`` — a non-UTF-8 console locale
(e.g. cp936/GBK on Chinese Windows) must not crash the read — and the
"missing/unreadable file" guards must treat undecodable bytes as
unreadable rather than let ``UnicodeDecodeError`` escape.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.cli import (
    _bundled_agent_brain_harness,
    _load_existing_host_id,
    _peek_default_agent_harness,
    _read_daemon_record,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Bytes that are not valid UTF-8 at any offset (0xFF never appears in UTF-8).
_UNDECODABLE_YAML = b"# \xff\xfe broken header\nexecutor:\n  harness: claude-sdk\n"


def test_bundled_and_user_config_reads_survive_non_utf8_locale(tmp_path: Path) -> None:
    """Config reads decode UTF-8 even when the preferred encoding is ASCII.

    Runs a child interpreter forced onto the POSIX locale (ASCII preferred
    encoding, the portable stand-in for a GBK/cp936 Windows console) and
    reads debby's bundled ``config.yaml`` (starts with a UTF-8 em-dash) plus
    a user-style ``--config`` YAML containing an em-dash. Locale-dependent
    reads die here with ``UnicodeDecodeError: 'ascii' codec can't decode
    byte 0xe2``.
    """
    user_config = tmp_path / "user-config.yaml"
    user_config.write_bytes("# em dash — header\nname: café\n".encode())
    home = tmp_path / "home"
    home.mkdir()

    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env.pop("RUNNER_SERVER_URL", None)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["PYTHONUTF8"] = "0"
    env["PYTHONCOERCECLOCALE"] = "0"
    env["PYTHONPATH"] = str(_REPO_ROOT)

    child_code = (
        "import locale, sys\n"
        "enc = locale.getpreferredencoding(False)\n"
        "if 'utf' in enc.replace('-', '').lower():\n"
        "    print('SKIP:' + enc)\n"
        "    sys.exit(0)\n"
        "from omnigent.cli import _bundled_agent_brain_harness, _load_config\n"
        "print('HARNESS:' + str(_bundled_agent_brain_harness('debby')))\n"
        # stdout is ASCII under this locale, so compare in-process and
        # print only ASCII markers.
        "cfg = _load_config(sys.argv[1])\n"
        "assert cfg['name'] == 'caf\\u00e9', repr(cfg)\n"
        "print('NAME:ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", child_code, str(user_config)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.stdout.startswith("SKIP:"):
        pytest.skip(
            f"interpreter forces a UTF-8 preferred encoding "
            f"({result.stdout.strip()}); locale-dependent reads cannot fail here"
        )
    assert result.returncode == 0, (
        "config reads crashed under a non-UTF-8 locale:\n" + result.stderr[-2000:]
    )
    assert "HARNESS:claude-sdk" in result.stdout
    assert "NAME:ok" in result.stdout


def test_bundled_agent_brain_harness_treats_undecodable_config_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undecodable bundled config yields ``None``, not a decode crash."""
    (tmp_path / "config.yaml").write_bytes(_UNDECODABLE_YAML)
    monkeypatch.setattr("omnigent.cli._bundled_example_path", lambda name: str(tmp_path))
    assert _bundled_agent_brain_harness("debby") is None


def test_peek_default_agent_harness_treats_undecodable_spec_as_unreadable(
    tmp_path: Path,
) -> None:
    """An undecodable agent spec yields ``None``, not a decode crash."""
    spec = tmp_path / "agent.yaml"
    spec.write_bytes(_UNDECODABLE_YAML)
    assert _peek_default_agent_harness(str(spec)) is None


def test_read_daemon_record_treats_undecodable_record_as_unreadable(
    tmp_path: Path,
) -> None:
    """An undecodable daemon record yields ``None``, not a decode crash."""
    record = tmp_path / "daemon.json"
    record.write_bytes(b'{"\xff\xfe": 1}')
    assert _read_daemon_record(record) is None


def test_load_existing_host_id_skips_undecodable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An undecodable global config is skipped, not a decode crash."""
    bad_config = tmp_path / "config.yaml"
    bad_config.write_bytes(_UNDECODABLE_YAML)
    monkeypatch.setattr("omnigent.cli._effective_global_config_path", lambda: bad_config)
    monkeypatch.setattr("omnigent.host.identity.CONFIG_PATH", tmp_path / "absent.yaml")
    assert _load_existing_host_id() is None
