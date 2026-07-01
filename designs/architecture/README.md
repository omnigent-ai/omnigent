# Omnigent architecture docs — index

Trace-backed master architecture (2026-06-30, branch `dhruvgupta/master-arch-docs`). Produced from
the code + a live trace corpus (real turns → Jaeger), verified in two passes (round-1 component
analysis, round-2 live-driving of the code-only CUJs). The four deliverable categories:

## 1. Overall architecture
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — system overview · process topology & channels ·
  end-to-end request lifecycle · empirical inter-component message/channel catalog · per-harness
  capability matrix · cross-cutting invariants & gaps · observability · trace-corpus index.
  **§10 = round-2 live-driving corrections** (timers, runner failover, +10 more).

## 2. Component architecture (this directory)
Each is also embedded as a `§7` subsection of the master doc; these are the standalone per-component views.
- [`server.md`](server.md) — FastAPI control plane + conversation store + DB (source of truth)
- [`runner.md`](runner.md) — per-conversation worker; WS reverse-tunnel
- [`host.md`](host.md) — host daemon; WS JSON control frames
- [`executor-harness.md`](executor-harness.md) — turn loop; SDK-vs-native split
- [`policy.md`](policy.md) — guardrails, ASK/elicitations
- [`tools-mcp-sandbox.md`](tools-mcp-sandbox.md) — tools, MCP routing, OmniBox sandbox
- [`agents-subagents-routing.md`](agents-subagents-routing.md) — subagents, routing, inbox
- [`web.md`](web.md) — React SPA
- [`tui-repl.md`](tui-repl.md) — `omni run` REPL + python client
- [`creds-auth-onboarding.md`](creds-auth-onboarding.md) — credentials, auth, onboarding
- [`observability.md`](observability.md) — distributed tracing

## 3. Diagrams ([`diagrams/`](diagrams))
Renderable Mermaid sources + rendered PNGs, cross-checked against the code.
- [`diagrams/topology.mmd`](diagrams/topology.mmd) ([png](diagrams/topology.png)) — process topology & channels.
- [`diagrams/executor-events.mmd`](diagrams/executor-events.mmd) ([png](diagrams/executor-events.png)) — the two harness couplings (SDK in-process loop vs native vendor-CLI + runner-resident forwarder), over the runner↔harness UDS.

## 4. Google-doc answers
- [`../STABILITY-CUJ-ANSWERS.md`](../STABILITY-CUJ-ANSWERS.md) — paste-ready answers to the
  "Stability & Reliability" tab ("How does claude-code / codex / polly behave when…").

## 5. CUJs
- [`../CUJ-MAP.md`](../CUJ-MAP.md) — the CUJ inventory (the list of journeys).
- [`../CUJ-ANALYSIS.md`](../CUJ-ANALYSIS.md) — per-CUJ mechanisms + `§7` (round-1) & `§8` (round-2)
  verification addenda.

> **Note on round-2 corrections:** where a component doc below still states a pre-round-2 claim
> (notably `sys_timer_*` "non-functional" and runner "no failover"), the authoritative correction is
> in [`../ARCHITECTURE.md` §10](../ARCHITECTURE.md). The two flagship corrections are also fixed
> inline in `tools-mcp-sandbox.md` and `runner.md`.
