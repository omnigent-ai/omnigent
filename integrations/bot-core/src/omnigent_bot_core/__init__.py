"""Shared Omnigent client for the chat-bot integrations.

Platform-agnostic by construction: nothing here knows what a Slack thread or a
Discord channel is. See the package README for why it is a separate
distribution from ``omnigent-client``.
"""

__all__ = ["__version__"]

__version__ = "0.12.0.dev0"
