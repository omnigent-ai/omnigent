"""Client for the lab's mock model server (``tests/server/integration/mock_llm_server.py``)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx


class MockModel:
    """Script replies on the mock model server, bypassing the model proxy.

    :param base_url: Direct mock server URL, e.g. ``"http://127.0.0.1:5123"``.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._client = httpx.Client(base_url=base_url, timeout=10.0, trust_env=False)

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def reply(
        self,
        responses: Sequence[dict[str, Any]],
        *,
        match: str | None = None,
        required_tools: Sequence[str] | None = None,
    ) -> str:
        """Queue replies, optionally claimed by a token in the user's message.

        Entries use the mock's schema, e.g. ``{"text": "done"}``,
        ``{"tool_calls": [...]}``, ``{"block": True}`` or ``{"error": ..., "status_code": 503}``.

        :param responses: Replies served in order.
        :param match: Token that routes a request to this queue when it appears in
            the user input, isolating the scenario from background requests.
        :param required_tools: Tool names a request must advertise to draw from this queue.
        :returns: The queue key.
        """
        body: dict[str, Any] = {"responses": list(responses)}
        if match is not None:
            body["match"] = match
        if required_tools is not None:
            body["required_tools"] = list(required_tools)
        response = self._client.post("/mock/configure", json=body)
        response.raise_for_status()
        return str(response.json()["key"])

    def set_fallback(self, text: str, *, key: str = "default") -> None:
        """Set the reply served once a queue is exhausted; survives :meth:`reset`.

        :param text: Fallback text, e.g. ``"ok"``.
        :param key: Queue key, e.g. ``"_policy_llm_"``.
        """
        response = self._client.post("/mock/set_fallback", json={"key": key, "text": text})
        response.raise_for_status()

    def reset(self) -> None:
        """Clear queues, captured requests and gates (fallbacks are kept)."""
        self._client.post("/mock/reset").raise_for_status()

    def requests(self) -> list[dict[str, Any]]:
        """Return every captured request body."""
        response = self._client.get("/mock/requests")
        response.raise_for_status()
        return list(response.json()["requests"])

    def gate_pending(self) -> bool:
        """Whether a ``{"block": True}`` reply is holding a request open."""
        response = self._client.get("/gate/pending")
        response.raise_for_status()
        return bool(response.json()["pending"])

    def release_gate(self) -> bool:
        """Release the oldest held reply.

        :returns: Whether a held reply was released.
        """
        response = self._client.post("/gate/release")
        response.raise_for_status()
        return bool(response.json()["released"])
