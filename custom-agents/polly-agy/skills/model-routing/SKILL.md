---
name: model-routing
description: How polly picks per-task models for sub-agent dispatches, especially opencode's many provider/model options. Use when deciding args.model for a sys_session_send, or when the user asks about model/provider choices.
---

# model-routing — picking models per task

polly decides the model per dispatch rather than always using a worker's
default. This applies to any worker, but matters most for `opencode`, which
has direct access to many providers/models on this machine.

## Defaults
- **Cheap / free / throwaway work** (quick explores, one-off checks, wide
  fan-outs where individual task quality matters less): prefer a cheap or
  free model, e.g. an `opencode/*-free` model for `opencode` dispatches, or a
  lightweight model for other workers.
- **Real implementation or review work** (anything that needs to be correct,
  produce a mergeable PR, or serve as an independent cross-vendor review):
  prefer a strong model, e.g. `github-copilot/claude-sonnet-5` or
  `google/gemini-3.1-pro-preview` for `opencode` dispatches, or the equivalent
  strong option for whichever worker is used.
- **If the right tradeoff isn't clear-cut** (task difficulty, cost, or
  quality bar is ambiguous): ask the user which model to use rather than
  guessing.

## How to check available models
- `sys_list_models` gives a static view, but it may under-report what a
  worker can actually run (e.g. it has reported "no usable model provider"
  for `opencode` even when the CLI itself is fully authenticated).
- For `opencode` specifically, trust `opencode models` (via `sys_os_shell`)
  over `sys_list_models` — it lists every provider/model actually
  authenticated on this machine (as of last check: `github-copilot/*`,
  `google/*`, and free `opencode/*` models).
- Pass the chosen model as `args.model: "<provider>/<model>"` on the
  `sys_session_send` call, e.g. `"github-copilot/claude-sonnet-5"` or
  `"opencode/big-pickle"`.

## Notes
- This is a per-task decision, not a fixed global setting — re-evaluate for
  each dispatch based on what the task actually needs.
- Other workers (`claude_code`, `codex`, `cursor`, `hermes`, `pi`) may have
  narrower model choices; apply the same cheap-vs-strong judgment call within
  whatever options `sys_list_models` or the worker's own CLI reports for it.
