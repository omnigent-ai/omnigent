---
name: smart-router
description: Pick the right polly sub-agent and model override for a task based on task type and harness constraints.
---

# smart-router — task-to-agent/model routing

Polly invokes this skill when it needs to route a task to a sub-agent and an
explicit `args.model` is useful. The skill is documentation-only; the actual
routing decision lives in polly's prompt, but this file is the canonical
reference for the mapping.

## Routing rules

- Frontend / UI / design tasks → `antigravity` with `args.model: gemini-3.5-flash`
- Code review / architecture → `claude_code` with `args.model: claude-sonnet-4-6`
- Core implementation → `claude_code` with `args.model: claude-opus-4-6`
- Light exploration / search → `pi` with `args.model: MiniMax-M3`
- Mid-tier implementation → `opencode` (DeepSeek default; no override needed)
- Rapid prototype → `kiro` with `args.model: GLM-5`

## Important harness constraints

- Antigravity is **Gemini-native**. `model_override.py` rejects non-Gemini models
  for the `antigravity-native` harness, so never route Claude models to
  `antigravity`.
- Kiro is its own family and accepts `GLM-5`.
