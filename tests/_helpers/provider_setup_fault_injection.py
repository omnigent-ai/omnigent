"""Fault injection for one disposable Providers host process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def install() -> None:
    root = Path(os.environ["PROVIDER_FIXTURE_ROOT"]).resolve()
    config_home = Path(os.environ["OMNIGENT_CONFIG_HOME"]).resolve()
    if config_home != root / "host-a/config":
        return

    from omnigent.onboarding import setup_operations as operations

    armed = root / "host-a/fail-save-and-cleanup"
    save = operations.save_setup_settings
    cleanup = operations.cleanup_unreferenced_secret

    def fail_save(*args: Any, **kwargs: Any) -> None:
        if armed.exists():
            (root / "host-a/save-fault-hit").write_text("fixture only")
            raise OSError("fixture-private-save-detail")
        save(*args, **kwargs)

    def fail_cleanup(ref: object, name: str) -> None:
        if armed.exists():
            (root / "host-a/cleanup-fault-hit").write_text("fixture only")
            raise OSError("fixture-private-cleanup-detail")
        cleanup(ref, name)

    operations.save_setup_settings = fail_save
    operations.cleanup_unreferenced_secret = fail_cleanup
