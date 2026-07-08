# Omnigent LiteLLM Gateway Configuration

This directory contains a [LiteLLM Proxy](https://litellm.vercel.app/) configuration
that exposes a unified OpenAI-compatible API endpoint to all Omnigent agents.
Agents reference models through `gateway/<name>` aliases; the Gateway handles
provider routing, key management, fallbacks, and rate-limit handling.

## Quick Start

### 1. Install LiteLLM

```bash
pip install 'litellm[proxy]'
```

Or, if you use the project's package manager:

```bash
uv pip install 'litellm[proxy]'
```

### 2. Set environment variables

All API keys are read from the environment — never hardcoded in the config.
Create a `.env` file (or export directly in your shell):

```bash
# Google AI Studio (Gemini)
export GEMINI_API_KEY="your-gemini-api-key"

# OpenRouter (free tier + Claude)
export OPENROUTER_API_KEY="your-openrouter-api-key"

# DeepSeek
export DEEPSEEK_API_KEY="your-deepseek-api-key"

# MiniMax
export MINIMAX_API_KEY="your-minimax-api-key"

# Zhipu AI (GLM-5)
export ZHIPU_API_KEY="your-zhipu-api-key"
```

A template is available at `examples/polly/.env.example`.

### 3. Start the proxy

```bash
litellm --config litellm-config.yaml --port 4000
```

The proxy listens on `http://0.0.0.0:4000` by default (configurable with `--port`).

### 4. Verify it works

```bash
curl http://localhost:4000/v1/models
```

You should see the six `gateway/<name>` aliases listed.

Test a completion:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gateway/gemini-35-flash",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Integration with Omnigent / Polly

### Option A: Global `llm:` block in `~/.hermes/config.yaml`

```yaml
llm:
  provider: openai  # LiteLLM exposes an OpenAI-compatible API
  model: gateway/deepseek-v4-flash   # default brain model
  api_base: http://localhost:4000/v1
  api_key: "not-needed"              # LiteLLM uses its own env-var keys
```

Now every Omnigent agent uses the Gateway by default.

### Option B: Agent-scoped override

In a Polly agent config (`examples/polly/agents/<name>/config.yaml`), pass
`args.model: gateway/<alias>` when dispatching, or set a default model for
that agent.

### Option C: Credential registration

If Omnigent's credential system is enabled (`omnigent credential add`),
register the Gateway as an OpenAI-compatible provider:

```bash
omnigent credential add \
  --provider openai \
  --api-base http://localhost:4000/v1 \
  --api-key placeholder \
  --label gateway
```

## Enabling Intelligent Routing

Set the environment variable that tells Omnigent to use the `sys_advise_models`
tool for smart model selection:

```bash
export OMNIGENT_SMART_ROUTING=1
```

When this is set, Polly and other orchestrators will call `sys_advise_models`
before every fan-out to get the optimal model per task. The LiteLLM Gateway
handles the actual fallback and retry logic at the proxy layer.

## Model Aliases

| Gateway Alias                   | Backend Provider | Model                         | Cost Tier |
|---------------------------------|------------------|-------------------------------|-----------|
| `gateway/openrouter-free`       | OpenRouter       | `openrouter/free`             | Free      |
| `gateway/gemini-35-flash`       | Google AI Studio | Gemini 3.5 Flash              | Low       |
| `gateway/deepseek-v4-flash`     | DeepSeek         | DeepSeek V4 Flash             | Low       |
| `gateway/minimax-m3`            | MiniMax          | MiniMax-M3 (Hailuo)           | Mid       |
| `gateway/claude-sonnet-4-6`     | OpenRouter       | Claude Sonnet 4-6             | High      |
| `gateway/glm-5`                 | Zhipu AI         | GLM-5                         | Mid       |

## Fallback Chain

The LiteLLM Gateway is configured with automatic fallbacks:

- **Free exhaustion**: `gateway/openrouter-free` → `gateway/gemini-35-flash` → `gateway/deepseek-v4-flash`
- **Gemini rate-limit**: `gateway/gemini-35-flash` → `gateway/deepseek-v4-flash` → `gateway/minimax-m3`
- **DeepSeek overload**: `gateway/deepseek-v4-flash` → `gateway/minimax-m3` → `gateway/claude-sonnet-4-6`
- **Sonnet unavailable**: `gateway/claude-sonnet-4-6` → `gateway/minimax-m3` → `gateway/deepseek-v4-flash`
- **GLM-5 unavailable**: `gateway/glm-5` → `gateway/deepseek-v4-flash` → `gateway/gemini-35-flash`

All fallback is transparent to the calling agent — no 429 handling needed in Polly.
