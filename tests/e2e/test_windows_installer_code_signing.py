"""Regression test: the Windows desktop installer must be code-signed.

The Windows NSIS installer produced by ``electron-builder`` ships without an
Authenticode signature, so Windows machines whose policy only allows signed
installers refuse to install Omnigent. The macOS pipeline declares a signing
identity plus a DMG notarization hook, but ``build.win`` in
``web/electron/package.json`` declares no signing mechanism at all, and no CI
workflow supplies Windows signing material either — so every Windows installer
built from this tree is unsigned by construction.

This test fails while that defect is present and passes once a Windows
code-signing mechanism lands, either:

- in ``web/electron/package.json`` under ``build.win``: a custom ``sign``
  hook, ``signtoolOptions``, ``azureSignOptions`` (Azure Trusted Signing),
  certificate fields (``certificateFile`` / ``certificateSubjectName`` /
  ``certificateSha1``), or ``forceCodeSigning: true`` (which makes
  electron-builder fail loudly when signing material is missing); or
- in a CI workflow that supplies Windows signing material to electron-builder
  (``WIN_CSC_LINK``) or invokes signtool / a trusted-signing action.

Usage::

    pytest tests/e2e/test_windows_installer_code_signing.py -v
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ELECTRON_PACKAGE_JSON = REPO_ROOT / "web" / "electron" / "package.json"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# build.win keys that each declare a code-signing mechanism electron-builder
# honors for the NSIS target.
_WIN_SIGNING_KEYS = (
    "sign",
    "signtoolOptions",
    "azureSignOptions",
    "certificateFile",
    "certificateSubjectName",
    "certificateSha1",
    "cscLink",
)

# Workflow markers that supply Windows signing material out-of-band.
_WORKFLOW_SIGNING_PATTERN = re.compile(
    r"WIN_CSC_LINK|azureSignOptions|trusted-signing|signtool",
    re.IGNORECASE,
)


def _win_signing_configured(build: dict[str, Any]) -> bool:
    win = build.get("win") or {}
    if any(win.get(key) for key in _WIN_SIGNING_KEYS):
        return True
    # forceCodeSigning makes electron-builder abort an unsigned build, so a
    # build that succeeds with it set necessarily produced signed artifacts.
    return bool(win.get("forceCodeSigning") or build.get("forceCodeSigning"))


def _workflow_signing_configured() -> bool:
    if not WORKFLOWS_DIR.is_dir():
        return False
    workflow_paths = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    return any(
        _WORKFLOW_SIGNING_PATTERN.search(path.read_text(encoding="utf-8", errors="replace"))
        for path in workflow_paths
    )


def test_windows_desktop_build_declares_code_signing() -> None:
    package = json.loads(ELECTRON_PACKAGE_JSON.read_text(encoding="utf-8"))
    build = package.get("build") or {}
    assert "win" in build, "web/electron/package.json no longer declares a Windows build target"

    assert _win_signing_configured(build) or _workflow_signing_configured(), (
        "The Windows desktop installer is built without Authenticode signing: "
        "build.win in web/electron/package.json declares no signing mechanism "
        "(sign hook, signtoolOptions, azureSignOptions, certificate fields, or "
        "forceCodeSigning) and no CI workflow supplies Windows signing "
        "material (WIN_CSC_LINK / signtool / trusted signing). Unsigned "
        "installers are rejected on managed Windows devices that require a "
        "valid signature, blocking installation entirely."
    )
