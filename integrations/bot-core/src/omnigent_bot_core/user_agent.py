"""User-Agent the chat bots send to the Omnigent server.

Each bot records its identity once at startup with :func:`set_bot_user_agent`
so the server can attribute its requests (e.g. ``omnigent-slack/0.22.0
python-httpx/0.28.1``). Until a bot does, requests carry the bot-core identity.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

import httpx

from omnigent_bot_core import __version__


def _user_agent_headers(distribution: str, fallback_version: str) -> dict[str, str]:
    try:
        distribution_version = version(distribution)
    except PackageNotFoundError:  # running from a source tree
        distribution_version = fallback_version
    return {
        "User-Agent": f"{distribution}/{distribution_version} python-httpx/{httpx.__version__}"
    }


_headers = _user_agent_headers("omnigent-bot-core", __version__)


def set_bot_user_agent(distribution: str, fallback_version: str) -> None:
    """
    Identify this process's requests as coming from the given bot.

    :param distribution: The bot's distribution name, e.g. ``"omnigent-slack"``.
    :param fallback_version: Version to report when the distribution is not
        installed, i.e. when running from a source tree.
    """
    global _headers
    _headers = _user_agent_headers(distribution, fallback_version)


def user_agent_headers() -> dict[str, str]:
    """Return a fresh copy of the headers identifying this bot."""
    return dict(_headers)
