"""Rich content an agent can attach to an Inbox item, as typed blocks.

An item's ``media`` is a list of blocks shaped like OpenAI content parts:
``{"type": "chart", "chart": {...}}``. The server accepts only block types it
can validate, and the web app skips types it cannot draw, so a new type (an
SVG, a reference to a stored file) adds a block model here and a renderer in
the web app, with no schema change.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from omnigent.server.inbox_chart import InboxChartV1

MAX_MEDIA_BLOCKS = 3
# Serialized size limit for all blocks, below the column's 16,383 stored-byte cap.
MAX_MEDIA_JSON_BYTES = 16_000


class ChartBlock(BaseModel):
    """A chart drawn on the card; see :class:`InboxChartV1`."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["chart"]
    chart: InboxChartV1

    def stored(self) -> dict[str, Any]:
        """The block as stored and returned, using the wire field names."""
        return {"type": self.type, "chart": self.chart.stored()}


# The block types the server accepts. Add a type as a union member with a
# ``type`` literal and annotate the union with ``Field(discriminator="type")``.
InboxMediaBlock = ChartBlock


def stored_media(blocks: list[InboxMediaBlock]) -> list[dict[str, Any]]:
    """Return *blocks* as stored.

    :raises ValueError: If they serialize to more than
        :data:`MAX_MEDIA_JSON_BYTES` bytes of JSON.
    """
    stored = [block.stored() for block in blocks]
    if len(json.dumps(stored).encode("utf-8")) > MAX_MEDIA_JSON_BYTES:
        raise ValueError(f"media is larger than {MAX_MEDIA_JSON_BYTES} bytes of JSON")
    return stored
