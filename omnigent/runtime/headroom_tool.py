"""Headroom retrieval tool for recovering compressed content.

Provides the headroom_retrieve tool that allows agents to recover
original uncompressed content from compressed messages when CCR
(Content-Cache-Retrieval) is enabled.
"""

from __future__ import annotations

import logging
from typing import Any

from omnigent.runtime.headroom_compression import CCRCache

_logger = logging.getLogger(__name__)


def headroom_retrieve(
    key: str,
    *,
    cache_dir: str | None = None,
) -> dict[str, Any]:
    """Retrieve original uncompressed content from Headroom cache.

    When CCR (Content-Cache-Retrieval) is enabled, compressed messages include
    a retrieval key. Use this tool to recover the full original content.

    :param key: The retrieval key from a compressed message (_headroom_key field).
    :param cache_dir: Optional cache directory override (for testing).
    :returns: Dict with 'content' (original text) or 'error' (if key not found).

    Example usage by an agent:
        If you see a compressed message like:
          "API response truncated [retrieve: abc123]"

        Call headroom_retrieve(key="abc123") to get the full response.
    """
    if not key:
        return {"error": "No retrieval key provided"}

    try:
        cache = CCRCache(cache_dir=cache_dir)
        content = cache.retrieve(key)

        if content is None:
            _logger.warning("CCR key not found: %s", key)
            return {
                "error": f"Content not found for key '{key}'. "
                "It may have been deleted or never cached."
            }

        _logger.info("Retrieved CCR content for key %s (%d bytes)", key, len(content))
        return {"content": content}

    except Exception as e:
        _logger.error("Failed to retrieve CCR content for key %s: %s", key, e)
        return {"error": f"Retrieval failed: {e}"}


# Tool definition for runtime registration
HEADROOM_RETRIEVE_TOOL = {
    "name": "headroom_retrieve",
    "description": (
        "Retrieve original uncompressed content from Headroom cache. "
        "Use this when you encounter compressed messages with a retrieval key "
        "and need to see the full details. The key is stored in the message's "
        "_headroom_key field."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "The retrieval key from the compressed message",
            }
        },
        "required": ["key"],
    },
}
