---
name: smart-router
description: >-
  Static decision table mapping task types to the best-fit sub-agent and
  LiteLLM Gateway model alias. Model selection deferred to sys_advise_models
  at runtime; Gateway handles fallback/retry.
---

# Smart Router — 任务到最优 agent+model 的静态决策手册

本 skill **不决定最终 model**，只决定"任务类型 → 候选 agent + 候选 model alias"。
具体 model 选择由 `sys_advise_models`（当 `OMNIGENT_SMART_ROUTING=1` 时可用）在每次
fan-out 前给出建议；Gateway 层自动处理 429、超时、quota 耗尽等降级。

## 路由表

| 任务类型 | 候选 Agent | 首选模型 alias | 成本优先级 |
|---|---|---|---|
| 前端/UI/设计 | `antigravity` | `gateway/gemini-35-flash` | 免费 > 低价 |
| 多文件长文本实现 | `pi` / `opencode` | `gateway/minimax-m3` / `gateway/deepseek-v4-flash` | 低价 > 中价 |
| 代码审查/架构 | `claude_code` / `pi` | `gateway/claude-sonnet-4-6` / `gateway/deepseek-v4-flash` | 中价 > 高价 |
| 轻量修复/单测 | `codex` / `opencode` | `gateway/deepseek-v4-flash` / `gateway/gemini-35-flash` | 免费 > 低价 |
| 快速原型 | `kiro` | `gateway/glm-5` | 中价 |
| 探索/搜索 | `pi` | `gateway/openrouter-free` / `gateway/gemini-35-flash` | 免费 > 低价 |

## 核心原则

### 1. 每次 fan-out 前必须先调用 `sys_advise_models`

当 `OMNIGENT_SMART_ROUTING=1` 时，`sys_advise_models` 工具会在你的工具列表中。
在调用 `sys_session_send` 之前，传入你即将 dispatch 的全部任务，用每个返回的
`model` 作为 `args.model`。例如：

```
sys_advise_models(tasks=[
  {
    title: "fix-header",
    agents: [{agent: "opencode", models: ["gateway/deepseek-v4-flash"]}],
    task: "Fix the header rendering bug in the login page."
  },
  {
    title: "review-header",
    agents: [{agent: "pi", models: ["gateway/claude-sonnet-4-6"]}],
    task: "Review the header fix for correctness and style."
  }
])
```

### 2. 成本优先级

```
免费 → 低价 → 中价 → 高价
```

- **免费**: `gateway/openrouter-free`（OpenRouter free tier）
- **低价**: `gateway/gemini-35-flash`、`gateway/deepseek-v4-flash`
- **中价**: `gateway/minimax-m3`、`gateway/glm-5`
- **高价**: `gateway/claude-sonnet-4-6`

尽可能从左侧选择，等免费/低价配额耗尽后自动升至右侧。

### 3. Fallback 由 Gateway 层自动处理

Polly **不自己处理 429/rate-limit/timeout**。LiteLLM Gateway 配置了完整的
fallback 链：

- 免费耗尽 → Gemini → DeepSeek
- Gemini 限速 → DeepSeek → MiniMax
- DeepSeek 过载 → MiniMax → Claude Sonnet
- 高价不可用 → 降到中价/低价
- Gateway 还有 cooldown 机制（连续 5 次失败后暂停 30 秒）

如果 dispatch 返回 model 错误，**不要重试**——检查 `sys_list_models` 确认
Gateway 上该 alias 是否在线，然后重新调用 `sys_advise_models` 获取建议。

### 4. Agent 与模型兼容性

- `antigravity` 使用 Gemini-native harness，只能运行 Gemini 系列模型
  （`gateway/gemini-35-flash`）。
- `kiro` 使用 Kiro-native harness，只能运行 GLM 系列（`gateway/glm-5`）。
- `pi` 是通用 agent，可以运行任意 Gateway model alias。
- `claude_code`、`codex`、`opencode` 等 CLI harness 有自身的内置模型，
  `args.model` 仅在他们支持 model 覆盖时可生效——通过 `sys_list_models`
  查看每个 agent 实际支持的模型列表。

## 工作流示意

```
                         ┌──────────────────┐
                         │  Polly 分解任务    │
                         └────────┬─────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │ sys_advise_models(tasks)  │ ← OMNIGENT_SMART_ROUTING=1
                    └──────────┬───────────────┘
                               │ returns [{agent, model, cost_tier}]
                               ▼
                    ┌──────────────────────────┐
                    │ sys_session_send(agent,  │
                    │   args.model=...)        │ ← 使用 Gateway alias
                    └──────────┬───────────────┘
                               │
                    ┌──────────▼──────────┐
                    │ LiteLLM Gateway      │ ← 自动 fallback / retry / cooldown
                    │ (http://localhost    │
                    │   :4000/v1)          │
                    └──────────────────────┘
```
