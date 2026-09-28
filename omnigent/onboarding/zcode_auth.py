"""Read-only ZCode credential probe.

ZCode stores browser OAuth and Coding Plan API-key credentials at
``$ZCODE_DATA_BASE_DIR/.zcode/v2/credentials.json`` (``$HOME`` when the env
var is unset). Omnigent does not read, decrypt, or copy that file. ZCode has
no credential-status command, so a non-empty file proves only that supported
credential storage is present, not that a credential is currently valid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from omnigent.onboarding.harness_install import ZCODE_KEY, harness_cli_installed


def zcode_data_base_dir() -> Path:
    """Directory whose ``.zcode/v2`` tree holds ZCode credentials."""
    raw = os.environ.get("ZCODE_DATA_BASE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home()


def zcode_credentials_path() -> Path:
    """Path of the encrypted credential file. The file is not opened."""
    return zcode_data_base_dir() / ".zcode" / "v2" / "credentials.json"


def zcode_login_configured() -> bool:
    """Return whether ZCode's shared credential store is present and non-empty."""
    path = zcode_credentials_path()
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


@dataclass(frozen=True)
class ZCodeAuthSummary:
    """Setup-overview facts. No credential material."""

    installed: bool
    signed_in: bool

    @property
    def ready(self) -> bool:
        """True when the CLI is on PATH and credential storage is present."""
        return self.installed and self.signed_in

    def describe(self) -> str:
        """Short status for the setup row."""
        if self.ready:
            return "Credentials found"
        if self.installed:
            return "No credentials found"
        return "Not installed"


def zcode_auth_summary() -> ZCodeAuthSummary:
    """Report install and credential presence without reading the credential file."""
    return ZCodeAuthSummary(
        installed=harness_cli_installed(ZCODE_KEY),
        signed_in=zcode_login_configured(),
    )
