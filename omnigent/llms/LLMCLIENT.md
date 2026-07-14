# LLM Client Design

## Overview

The `llms/` module is a client-side multi-provider LLM SDK that replaces litellm. It presents the **OpenAI Responses API** as its public interface and routes requests to any supported LLM provider. Internally, it uses **Chat Completions as a lingua franca** — each provider adapter translates between Chat Completions format and the provider's native API.

Translation logic is ported from MLflow AI Gateway adapters (master + TomeHirata's provider PRs #21990–#21999).

## Public API

```python
from llms import Client

client = Client()

# Non-streaming
resp = client.responses.create(
    input=[{"role": "user", "content": "Hello"}],
    instructions="You are a helpful assistant.",
    model="anthropic/claude-sonnet-4-20250514",
    tools=[{"type": "function", "function": {...}}],
    reasoning={"effort": "high", "summary": "concise"},
)
# resp.output -> list of MessageOutput / FunctionCallOutput items
# resp.model -> str
# resp.usage -> Usage

# Streaming
for event in client.responses.create(..., stream=True):
    if event.type == "response.output_text.delta":
        print(event.delta, end="")
    elif event.type == "response.completed":
        final = event.response
```

Model strings use `"provider/model-name"` format. If no provider prefix is given, defaults to `"openai"`.

---

## Architecture

```
llms/
  __init__.py              # exports Client
  client.py                # Client class with .responses.create()
  types.py                 # Response/streaming dataclasses
  routing.py               # "anthropic/claude-..." -> provider + model
  _responses_to_chat.py    # Responses API <-> Chat Completions translation
  adapters/
    __init__.py            # get_adapter() registry
    base.py                # BaseAdapter ABC
    openai.py              # OpenAI + OpenAI-compatible (Groq, DeepSeek, xAI, OpenRouter, Ollama)
    anthropic.py           # Anthropic Messages API
    gemini.py              # Gemini generateContent API
    bedrock.py             # AWS Bedrock Converse API
    vertex.py              # Vertex AI (Gemini format + GCP auth)
    databricks.py          # Databricks (OpenAI-compat + OAuth)
```

### Request Flow

```
client.responses.create(input, instructions, model, tools, stream)
  │
  ├─ routing.parse_model_string(model) -> (provider, model_name)
  │
  ├─ _responses_to_chat.responses_input_to_chat_messages(input, instructions)
  │     Responses API items -> Chat Completions messages
  │
  ├─ adapter = get_adapter(provider)
  │
  ├─ adapter.chat_completions(messages, model_name, tools, stream, extra)
  │     Chat Completions -> Provider Native -> HTTP -> Provider Response -> Chat Completions
  │
  └─ _responses_to_chat.chat_response_to_response(chat_dict)
        Chat Completions response -> Response dataclass
        (or chat_stream_to_response_events for streaming)
```

---

## Types — `types.py`

Dataclasses matching the attribute access patterns in `workflow.py`'s `_response_to_dict()` and `_accumulate_stream()`.

### Response Types

```python
@dataclass
class OutputText:
    type: str = "output_text"  # always "output_text"
    text: str

@dataclass
class MessageOutput:
    type: str = "message"  # always "message"
    content: list[OutputText]

@dataclass
class FunctionCallOutput:
    type: str = "function_call"  # always "function_call"
    call_id: str
    name: str
    arguments: str

@dataclass
class Usage:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None

@dataclass
class Response:
    output: list[MessageOutput | FunctionCallOutput]
    model: str
    usage: Usage | None
```

### Streaming Event Types

```python
@dataclass
class ResponseTextDeltaEvent:
    type: str = "response.output_text.delta"
    delta: str

@dataclass
class ResponseCompletedEvent:
    type: str = "response.completed"
    response: Response
```

Reasoning events (`response.reasoning_text.delta`, `response.reasoning_summary_text.delta`) are only emitted by OpenAI. Non-OpenAI providers simply don't emit them — `_accumulate_stream()` handles this gracefully.

---

## Routing — `routing.py`

```python
@dataclass
class RoutedModel:
    provider: str   # e.g. "anthropic"
    model: str      # e.g. "claude-sonnet-4-20250514"

def parse_model_string(model: str) -> RoutedModel:
    """
    Parse "provider/model-name" -> RoutedModel.
    No "/" defaults provider to "openai".
    """
```

---

## Translation Layer — `_responses_to_chat.py`

### Input: Responses API -> Chat Completions

| Responses API input | Chat Completions message |
|---|---|
| `instructions` string | `{"role": "system", "content": instructions}` |
| `{"role": "user", "content": "..."}` | `{"role": "user", "content": "..."}` |
| `{"role": "assistant", "content": "..."}` | `{"role": "assistant", "content": "..."}` |
| `{"type": "function_call", "call_id": "...", "name": "...", "arguments": "..."}` | Grouped into assistant message with `tool_calls` array |
| `{"type": "function_call_output", "call_id": "...", "output": "..."}` | `{"role": "tool", "tool_call_id": "...", "content": "..."}` |

### Output: Chat Completions -> Responses API

| Chat Completions response | Responses API output |
|---|---|
| `choices[0].message.content` | `MessageOutput` with `OutputText` |
| `choices[0].message.tool_calls` | List of `FunctionCallOutput` items |
| `usage.prompt_tokens / completion_tokens` | `Usage(input_tokens, output_tokens, total_tokens)` |

### Streaming: Chat Completions chunks -> Responses API events

- Text content deltas -> `ResponseTextDeltaEvent`
- Tool call deltas accumulated across chunks
- `finish_reason` set -> `ResponseCompletedEvent` with assembled `Response`

---

## Adapters

### Base Adapter — `adapters/base.py`

```python
class BaseAdapter(ABC):
    @abstractmethod
    def chat_completions(
        self,
        messages: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        extra: dict[str, Any],
    ) -> dict[str, Any] | Iterator[dict[str, Any]]:
        """
        Send a chat completions request.
        stream=False: returns Chat Completions response dict.
        stream=True: returns iterator of Chat Completions chunk dicts.
        """
        ...
```

### OpenAI + Compatible — `adapters/openai.py`

Chat Completions IS the native format. Minimal translation (add model to payload). Streaming parses SSE lines.

`OpenAICompatibleAdapter` subclass accepts configurable `base_url` and `api_key_env` for all OpenAI-compatible providers:

| Provider | `base_url` | `api_key_env` |
|----------|-----------|---------------|
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `deepseek` | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` |
| `xai` | `https://api.x.ai/v1` | `XAI_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | (none) |
| `minimax` | `https://api.minimax.io/v1` | (none) |

HTTP via sync `httpx` (already a project dependency).

#### MiniMax

The `minimax` provider supports `MiniMax-M3` with a 1,000,000-token context
window and `MiniMax-M2.7` with a 204,800-token context window. The OpenAI
compatible global endpoint is the default. Use `connection_params["base_url"]`
for another region or use the `anthropic` model prefix with an Anthropic base
URL to select the Messages protocol:

`MiniMax-M3` accepts text, image, and video input. OpenAI-compatible requests
enable thinking by default, while Anthropic-compatible requests disable it by
default. Use `thinking={"type": "adaptive"}` or
`thinking={"type": "disabled"}` to select the behavior explicitly.
`MiniMax-M2.7` accepts text input and always uses thinking.

Both protocols also accept `service_tier="priority"` for `MiniMax-M3`;
omitting it uses the standard admission tier. Published prices are in USD per
million tokens:

| Model | Service tier | Input length | Input | Output | Cache read | Cache write |
|-------|--------------|--------------|-------|--------|------------|-------------|
| `MiniMax-M3` | Standard | Up to 512,000 | $0.30 | $1.20 | $0.06 | N/A |
| `MiniMax-M3` | Standard | Above 512,000 | $0.60 | $2.40 | $0.12 | N/A |
| `MiniMax-M3` | Priority | Up to 512,000 | $0.45 | $1.80 | $0.09 | N/A |
| `MiniMax-M3` | Priority | Above 512,000 | $0.90 | $3.60 | $0.18 | N/A |
| `MiniMax-M2.7` | Standard | Any | $0.30 | $1.20 | $0.06 | $0.375 |

| Region | Protocol | Model prefix | Base URL |
|--------|----------|--------------|----------|
| Global | OpenAI compatible | `minimax/` | `https://api.minimax.io/v1` |
| China | OpenAI compatible | `minimax/` | `https://api.minimaxi.com/v1` |
| Global | Anthropic compatible | `anthropic/` | `https://api.minimax.io/anthropic` |
| China | Anthropic compatible | `anthropic/` | `https://api.minimaxi.com/anthropic` |

```python
from omnigent.llms import Client

client = Client()

response = await client.responses.create(
    input=[{"role": "user", "content": "Hello"}],
    model="minimax/MiniMax-M3",
    connection_params={"api_key": "..."},
    thinking={"type": "disabled"},
)

response = await client.responses.create(
    input=[{"role": "user", "content": "Hello"}],
    model="anthropic/MiniMax-M3",
    connection_params={
        "api_key": "...",
        "base_url": "https://api.minimax.io/anthropic",
    },
    thinking={"type": "adaptive"},
)
```

The Anthropic base URLs intentionally end in `/anthropic`. The adapter derives
the `/v1/messages` request path internally.

### Anthropic — `adapters/anthropic.py`

Ported from `mlflow/gateway/providers/anthropic.py`. Key translations:

**Request (Chat Completions -> Anthropic Messages API):**
- System messages extracted -> top-level `system` field
- Assistant `tool_calls` -> `tool_use` content blocks
- Tool messages -> `tool_result` blocks in user role
- OpenAI tools -> `name/description/input_schema` format
- `tool_choice` mapping: `"none"/"auto"/"required"` -> Anthropic equivalents
- Temperature halved (OpenAI 0-2 range -> Anthropic 0-1 range)

**Response (Anthropic -> Chat Completions):**
- `content` blocks of type `text` -> assistant message content
- `content` blocks of type `tool_use` -> `tool_calls` array
- `stop_reason` mapping: `max_tokens` -> `length`, else `stop`
- `usage.input_tokens/output_tokens` -> `prompt_tokens/completion_tokens`

**Streaming:** Parse SSE events (`message_start`, `content_block_start`, `content_block_delta`, `message_delta`). Assemble into Chat Completions streaming chunks.

Auth: `ANTHROPIC_API_KEY` env var. Headers: `x-api-key`, `anthropic-version: 2023-06-01`.
Endpoint: `https://api.anthropic.com/v1/messages`

### Gemini — `adapters/gemini.py`

Ported from `mlflow/gateway/providers/gemini.py`. Key translations:

**Request (Chat Completions -> Gemini):**
- Messages -> `contents` with role remapping (`assistant` -> `model`)
- System messages -> `system_instruction`
- Tool calls -> `functionCall` parts
- Tool results -> `functionResponse` parts in user role
- Tools -> `functionDeclarations` with `parametersJsonSchema`
- Generation config key mapping (`stop` -> `stopSequences`, `max_tokens` -> `maxOutputTokens`, etc.)

**Response (Gemini -> Chat Completions):**
- `candidates[0].content.parts[0].text` -> assistant content
- `functionCall` parts -> `tool_calls` (MD5 `call_id` fallback for Gemini's missing IDs)
- `usageMetadata` -> usage

Auth: `GOOGLE_API_KEY` via `x-goog-api-key` header.
Endpoint: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
Streaming: `:streamGenerateContent?alt=sse`

### Bedrock — `adapters/bedrock.py`

Ported from `mlflow/gateway/providers/bedrock.py` (Converse API).

**Request (Chat Completions -> Bedrock Converse):**
- System messages -> `system` prompts
- User/assistant messages -> content blocks with `text`
- Tool results -> `toolResult` blocks in user role
- Tools -> `toolConfig` with `toolSpec` entries
- Generation params -> `inferenceConfig` (temperature, topP, maxTokens, stopSequences)

**Response (Bedrock Converse -> Chat Completions):**
- `output.message.content` blocks -> text content and/or `tool_calls`
- `stopReason` mapping: `tool_use` -> `tool_calls`, else `stop`
- `usage.inputTokens/outputTokens/totalTokens` -> Chat Completions usage

Uses `boto3` (lazy import, sync). Auth from env: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION`.

### Vertex AI — `adapters/vertex.py`

Inherits Gemini translation logic. Different auth and endpoint:
- Auth: `google.auth` Application Default Credentials or service account (lazy import)
- Endpoint: `https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent`
- Config from env: `VERTEX_PROJECT`, `VERTEX_LOCATION`

### Databricks — `adapters/databricks.py`

Extends OpenAI-compatible adapter with Databricks auth:
- Auth: `DATABRICKS_HOST` + `DATABRICKS_TOKEN` from env. Bearer token.
- Base URL: `{DATABRICKS_HOST}/serving-endpoints`

---

## Integration with workflow.py

The swap is minimal:

```python
# Before
import openai
_openai_client = openai.OpenAI()
resp = _openai_client.responses.create(
    input=input_items, instructions=instructions,
    model="gpt-5.4", tools=tools, stream=True,
)

# After
from llms import Client
_llm_client = Client()
resp = _llm_client.responses.create(
    input=input_items, instructions=instructions,
    model="openai/gpt-5.4", tools=tools, stream=True,
)
```

`_response_to_dict()` and `_accumulate_stream()` work unchanged — they access `.type`, `.output`, `.delta`, `.response`, `.content`, `.text`, `.call_id`, `.name`, `.arguments` attributes which the `llms.types` dataclasses provide.

Model strings without a provider prefix default to `"openai"` for backward compatibility.

---

## Dependencies

- `httpx` (already in pyproject.toml) — HTTP client for all providers
- `boto3` — lazy import, only for Bedrock
- `google-auth` — lazy import, only for Vertex AI
- No litellm, no aiohttp, no fastapi
- `openai` SDK is NOT used by the llms module itself (it makes raw HTTP calls)

## Implementation Phases

1. **Phase 1 — Core plumbing**: types, routing, _responses_to_chat, base adapter, client, __init__
2. **Phase 2 — OpenAI adapter + workflow swap**: adapters/openai.py, update workflow.py
3. **Phase 3 — Anthropic adapter**: adapters/anthropic.py
4. **Phase 4 — Gemini adapter**: adapters/gemini.py
5. **Phase 5 — Remaining**: adapters/bedrock.py, adapters/vertex.py, adapters/databricks.py
