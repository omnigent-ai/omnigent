"""Background-install missing harness CLIs when ``omnigent host`` boots on a
managed sandbox (``IS_SANDBOX=1``), so a fresh arclet offers Pi without manual
setup. Laptops never set that flag; a silent global npm install is unwanted there."""

from __future__ import annotations

import logging
import os
import threading

from omnigent.onboarding.harness_install import (
    PI_KEY,
    harness_cli_binary,
    harness_install_spec,
    try_install_harness_cli,
)

_logger = logging.getLogger(__name__)

# Only Pi is auto-installed; one CLI keeps the boot footprint bounded.
_MANAGED_HARNESS_KEYS: tuple[str, ...] = (PI_KEY,)


def running_in_managed_sandbox() -> bool:
    """Whether this host is a managed sandbox (image sets ``IS_SANDBOX=1``)."""
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


def _install_missing_harness(key: str) -> None:
    """Install one managed harness CLI when no binary is present and it is installable."""
    try:
        # Presence only: an existing (even outdated) CLI is never upgraded here;
        # readiness reports its version separately.
        if harness_cli_binary(key) is not None:
            return
        result = try_install_harness_cli(key)
    except Exception:
        _logger.exception("Harness preparation: install of %r failed", key)
        return
    if result.installed:
        _logger.info("Harness preparation: installed %r CLI", key)
    else:
        _logger.info("Harness preparation: skipped %r CLI (%s)", key, result.reason)


def prepare_host_harnesses() -> bool:
    """Start background installs of missing managed-sandbox harness CLIs; ``True``
    when a thread was started, ``False`` off a managed sandbox. Non-blocking so
    host registration isn't delayed by a multi-second install."""
    if not running_in_managed_sandbox():
        return False
    started = False
    for key in _MANAGED_HARNESS_KEYS:
        try:
            if harness_install_spec(key) is None:
                continue
        except Exception:  # a broken plugin spec must not abort host boot
            _logger.exception("Harness preparation: spec lookup for %r failed", key)
            continue
        worker = threading.Thread(
            target=_install_missing_harness,
            args=(key,),
            name=f"host-harness-prepare-{key}",
            daemon=True,
        )
        try:
            worker.start()
        except RuntimeError:  # thread exhaustion must not abort host boot
            _logger.exception("Harness preparation: could not start the %r installer", key)
            continue
        started = True
    return started
