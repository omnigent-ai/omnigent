# Cara — Support Triage

Cara is a support-triage orchestrator with two inline specialist sub-agents.
She classifies every ticket (category, severity, root-cause hypothesis) and
then drafts an empathetic customer reply — cross-vendor, sequentially, every
time.

```bash
omnigent run examples/cara/
```

## How it works

| Step | Agent | Harness | Output |
|---|---|---|---|
| 1 — Classify | `classifier` | `claude-sdk` | Category, severity, root cause, key facts |
| 2 — Respond | `responder` | `codex` | Draft customer reply |
| 3 — Present | Cara (orchestrator) | `claude-sdk` | Triage summary + next steps |

Both specialists are declared **inline** inside `config.yaml` — no separate
`agents/` directory needed. This makes the whole agent a single self-contained
file, which suits focused specialists like these.

## Prerequisites

- Omnigent ≥ 0.2.0: `uv tool install "omnigent>=0.2.0"`
- An Anthropic API key (Cara's brain + classifier):
  `export ANTHROPIC_API_KEY=sk-ant-...`
- An OpenAI API key (responder): `export OPENAI_API_KEY=sk-...`

Or run `omnigent setup` to configure both providers interactively.

## Run it

```bash
omnigent run examples/cara/
```

Paste or type a support ticket when Cara prompts you. She handles the rest.

### Single-provider mode

If you only have a Claude provider, swap the responder harness at run time:

```bash
omnigent run examples/cara/ --harness claude-sdk
```

This overrides the orchestrator's harness; inline agents pick it up too.

## Example session

```
Cara: Paste or describe the support ticket.

You:  Hi, I haven't been able to log in since yesterday. I keep getting
      "Invalid session" even after resetting my password. This is blocking
      our whole team — we have a demo in 2 hours.

Cara: [classifier] Category: Authentication | Severity: 🔴 critical
      Root cause: session cache not invalidated after password reset.

      [responder] Draft reply ready.

      ## 🏷️ Classification
      **Category:** Authentication
      **Severity:** 🔴 critical
      **Root cause:** Session cache not invalidated after password reset
      **Key facts:**
        - Error: "Invalid session" post-reset
        - Team-wide impact
        - 2-hour deadline

      ## ✉️ Draft Reply
      Hi,

      I'm sorry this is blocking your team before a demo — that's the
      worst timing.

      Here's what to try right now:
      1. Sign out on all devices and clear browser cookies/storage.
      2. Wait 2 minutes, then sign in fresh (this forces a new session token).
      3. If still failing, try an incognito window.

      If none of those work, reply here immediately and I'll escalate to
      our auth team with emergency priority.

      We're monitoring and will follow up within 30 minutes.

      ## Next Steps
      - Send the draft reply now.
      - If unresolved in 15 min, run `/escalate` to page the auth team.

      > ⚠️ High-severity ticket — consider `/escalate` to hand off to a human.
```

## The `escalate` skill

For critical or high-severity tickets, ask Cara to escalate:

```
You: /escalate
```

Cara produces a ready-to-paste escalation package with severity, impact
summary, timeline, suggested owner, and recommended next steps — formatted
for a Slack incident channel or PagerDuty.

## Inline sub-agents

Cara's two specialists are defined directly in `config.yaml` under `tools:`
rather than in separate `agents/<name>/config.yaml` files:

```yaml
tools:
  classifier:
    type: agent
    prompt: |
      You are a support ticket classifier…
    executor:
      harness: claude-sdk

  responder:
    type: agent
    prompt: |
      You are a support responder…
    executor:
      harness: codex
```

Use the inline pattern when your specialists are short and tightly coupled to
the orchestrator. Use separate files (as in Polly, Debby, Pippa) when agents
have their own skill directories or complex configurations worth maintaining
independently.

## Extending Cara

- **Add a `kb_lookup` tool** (`type: function`) to search your knowledge base
  before the responder drafts — so the reply references real docs.
- **Add a `notify` tool** that posts the escalation package to a Slack channel
  via webhook when `/escalate` runs.
- **Add a `logger` tool** to write triage summaries to a local CSV for
  tracking volume and severity trends over time.
