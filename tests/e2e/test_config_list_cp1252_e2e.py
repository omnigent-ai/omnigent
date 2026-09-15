"""``omnigent config list`` must survive a legacy non-UTF-8 stdio encoding.

On Windows with the default legacy ANSI codepage (cp1252),
``omnigent config list`` crashes with ``UnicodeEncodeError`` because the
provider listing prints per-kind emoji glyphs (``_KIND_GLYPH`` in
``omnigent/onboarding/configure_models.py``) through a plain rich
``Console()`` whose underlying stream encodes with the locale codepage,
which cannot represent the emoji (e.g. ``U+1F511``).

The test drives the real user journey end-to-end: seed one configured
provider in an isolated ``OMNIGENT_CONFIG_HOME``, then run the actual
``python -m omnigent config list`` subprocess with its stdio encoding
forced to cp1252 via ``PYTHONIOENCODING`` — the cross-platform equivalent
of Windows resolving stdio to the legacy ANSI codepage. Before the fix the
command aborts with ``UnicodeEncodeError`` (exit 1, listing truncated);
after the fix it must exit 0 and still show the configured provider.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml


def _seed_provider_config(config_home: Path) -> None:
    """Write a minimal ``config.yaml`` with one key-kind provider.

    Any configured provider triggers the glyph rendering (every
    ``_KIND_GLYPH`` entry is a non-cp1252 emoji), so one ``key`` entry is
    enough to walk the crashing path.

    :param config_home: Directory used as ``OMNIGENT_CONFIG_HOME``.
    """
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "env:CP1252_E2E_DUMMY_KEY",
                            "models": {"default": "claude-sonnet-4-6"},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def test_config_list_survives_cp1252_stdio(tmp_path: Path) -> None:
    """The provider listing renders (no crash) when stdio is cp1252.

    Runs the real CLI subprocess with ``PYTHONIOENCODING=cp1252`` —
    the same stdio text encoding a default-locale Windows install
    resolves — and asserts the command completes and lists the
    configured provider instead of aborting with ``UnicodeEncodeError``.
    """
    config_home = tmp_path / "confighome"
    _seed_provider_config(config_home)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(tmp_path / "omnigent-data"),
            "CP1252_E2E_DUMMY_KEY": "sk-test-dummy",
            # Force the stdio text encoding to the Windows legacy ANSI
            # codepage. On a real cp1252-locale Windows box Python picks
            # this up from locale.getpreferredencoding(); PYTHONIOENCODING
            # reproduces exactly that stream encoding on any OS.
            "PYTHONIOENCODING": "cp1252",
        }
    )
    # PYTHONUTF8=1 is the documented workaround and would mask the bug —
    # make sure an ambient setting doesn't leak into the subprocess.
    env.pop("PYTHONUTF8", None)

    result = subprocess.run(
        [sys.executable, "-m", "omnigent", "config", "list"],
        capture_output=True,
        env=env,
        timeout=120,
    )

    # stdout/stderr are cp1252-encoded bytes; decode leniently so the
    # assertion messages stay readable even when glyphs were replaced.
    stdout = result.stdout.decode("cp1252", errors="replace")
    stderr = result.stderr.decode("cp1252", errors="replace")
    combined = stdout + stderr

    assert "UnicodeEncodeError" not in combined, (
        "`omnigent config list` crashed with UnicodeEncodeError on a "
        f"cp1252 (legacy Windows codepage) stdio stream:\n{combined}"
    )
    assert result.returncode == 0, (
        "`omnigent config list` exited non-zero under a cp1252 stdio "
        f"encoding (exit {result.returncode}):\n{combined}"
    )
    # The listing must still show the configured provider — the command
    # may degrade the glyph, but not drop the row or truncate the output.
    assert "anthropic" in stdout, (
        f"configured provider missing from the cp1252 listing:\n{combined}"
    )
