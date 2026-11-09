"""Verify the mock's paced text reaches the same SDK used by the web harness."""

import time

import httpx
import pytest
from agents import Agent, OpenAIResponsesModel, RunConfig, Runner
from openai import AsyncOpenAI

from tests.server.integration.test_mock_tool_routing_http import mock_http_url  # noqa: F401


@pytest.mark.parametrize("chunk_delay", [0, 0.02])
async def test_responses_sdk_receives_text_before_completion(
    mock_http_url: str,  # noqa: F811
    chunk_delay: float,
) -> None:
    text = "  First line.\n\nSecond  line\twith spacing.\n"
    async with httpx.AsyncClient(trust_env=False, timeout=10) as http:
        configured = await http.post(
            f"{mock_http_url}/mock/configure",
            json={"responses": [{"text": text, "stream": True, "chunk_delay": chunk_delay}]},
        )
        configured.raise_for_status()
        async with AsyncOpenAI(
            base_url=f"{mock_http_url}/v1", api_key="mock", http_client=http
        ) as client:
            result = Runner.run_streamed(
                Agent(name="stream-test", model=OpenAIResponsesModel("mock-model", client)),
                input="Write an answer.",
                run_config=RunConfig(tracing_disabled=True),
            )
            deltas: list[str] = []
            arrivals: list[float] = []
            item_id = None
            completed = False
            sequence_numbers: list[int] = []
            async for event in result.stream_events():
                if event.type != "raw_response_event":
                    continue
                data = event.data
                assert data.type is not None, "Every event must identify its type to the SDK"
                sequence_numbers.append(data.sequence_number)
                if data.type == "response.output_item.added":
                    item_id = data.item.id
                    assert data.item.status == "in_progress"
                    assert data.item.content == []
                elif data.type == "response.output_text.delta":
                    assert not completed
                    assert item_id is not None and data.item_id == item_id
                    assert data.output_index == data.content_index == 0
                    deltas.append(data.delta)
                    arrivals.append(time.monotonic())
                elif data.type == "response.completed":
                    assert len(deltas) > 1
                    assert "".join(deltas) == text
                    completed = True

            assert completed
            assert result.final_output == text
            assert sequence_numbers == list(range(len(sequence_numbers)))
            if chunk_delay:
                assert arrivals[-1] - arrivals[0] >= chunk_delay * (len(arrivals) - 1) * 0.8
