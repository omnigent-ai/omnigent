"""Databricks Apps entry point for the Omnigent Slack bot.

Runs the Socket-Mode bot and, in Databricks web-auth mode, the enrollment web
server. The ``omnigent_slack`` package is installed from the wheel ``deploy.py``
copies next to this file; the app's ``uv run`` command resolves it (and the
inlined runtime deps) from the generated ``pyproject.toml`` in-container at boot.
Startup failures are logged and the process is held open briefly so the platform
captures them in ``/logz``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import traceback

logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)
logger = logging.getLogger("omnigent-slack-app")

try:
    from omnigent_slack.app import run

    if __name__ == "__main__":
        logger.info("Starting Omnigent Slack bot (Databricks App)")
        asyncio.run(run())
except Exception:  # startup catch-all; we want every failure logged to /logz
    logger.error("FATAL: Omnigent Slack bot failed to start:\n%s", traceback.format_exc())
    # Keep the process alive briefly so the platform captures the traceback.
    time.sleep(30)
    sys.exit(1)
