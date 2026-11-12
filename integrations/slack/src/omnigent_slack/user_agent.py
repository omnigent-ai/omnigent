"""User-Agent the Slack bot sends to the Omnigent server."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import httpx

from omnigent_slack import __version__

try:
    _VERSION = version("omnigent-slack")
except PackageNotFoundError:  # running from a source tree
    _VERSION = __version__

# Lets the server attribute the bot's requests, e.g. ``omnigent-slack/0.22.0 python-httpx/0.28.1``.
SLACK_USER_AGENT = f"omnigent-slack/{_VERSION} python-httpx/{httpx.__version__}"
USER_AGENT_HEADERS = {"User-Agent": SLACK_USER_AGENT}
